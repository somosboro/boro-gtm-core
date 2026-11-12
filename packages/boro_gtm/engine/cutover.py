"""The cutover report: what must be true before SHADOW_MODE goes off (CE-ADR-015, CE-ADR-018).

Each check is a query over durable rows or a recorded verification. Nothing
here can be satisfied by configuration alone: Gmail must actually have been
read, shadow must actually have run on business days, the READY audit and the
parity comparison must actually have been recorded and passed. `ready_for_live`
is true only when every BLOCKING check passes; warnings never make it true and
never make it false. The report never switches anything itself.
"""

from __future__ import annotations

import os
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from boro_gtm.engine import controls
from boro_gtm.engine import states as S
from boro_gtm.engine.business_days import BusinessCalendar
from boro_gtm.engine.history import RECONCILIATION_KEY
from boro_gtm.engine.models import (
    CeAccount,
    CeContact,
    CeMessage,
    CeSourceRun,
    CeSuppression,
    CeTarget,
    CeTaskRun,
    CeWorkerHeartbeat,
)
from boro_gtm.engine.pipeline import (
    email_domain,
    is_role_mailbox,
    now_utc,
    prior_contact,
    suppression_for,
)
from boro_gtm.engine.replies import BACKFILL_KEY
from boro_gtm.engine.settings import engine_settings
from boro_gtm.engine.strategy import Strategy, active_strategy

#: Gmail history must reach back at least to here (the start of BoRo outreach).
HISTORY_SINCE = date(2026, 6, 1)
SHADOW_BUSINESS_DAYS = 2
#: READY audit thresholds, as fractions of the sample (minimum 30 targets).
AUDIT_MIN_SAMPLE = 30
AUDIT_THRESHOLDS = {"account_identity": 0.95, "domain": 0.95, "signal": 0.95, "lane": 0.95,
                    "source": 1.0, "score_defensible": 0.9, "contact_belongs": 0.95,
                    "channel_appropriate": 0.9, "no_support_mailbox": 1.0,
                    "no_gmail_duplicate": 1.0, "no_suppression": 1.0,
                    "message_reflects_evidence": 0.95, "no_invented_pain": 1.0}
#: Real sending needs an off-host copy (CE-ADR-018): once live, the database
#: holds what Gmail cannot rebuild (manual suppressions, lanes, scores, human
#: pipeline moves). It does not block shadow operation.
OFF_HOST_BACKUP_BLOCKS_LIVE = True
#: A verified copy on the operator's PC counts when its dump is younger than this.
PC_COPY_MAX_AGE_HOURS = 50


def _at(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(value) if value else None
    except (TypeError, ValueError):
        return None


def _task_result(run: CeTaskRun) -> dict[str, Any]:
    """What a task returned. The worker stores it wrapped as {"value": ...}
    (worker._jsonable); older or hand-written rows may hold it bare."""
    result = run.result or {}
    inner = result.get("value") if isinstance(result, dict) else None
    return inner if isinstance(inner, dict) else (result if isinstance(result, dict) else {})


def _strategy_problems(strategy: Strategy) -> list[str]:
    raw = strategy.raw
    problems = []
    if raw.get("status") != "ACTIVE":
        problems.append("strategy status is not ACTIVE")
    sender = strategy.sender.get("email", "")
    if not sender.endswith("@somosboro.com"):
        problems.append("sender is not the corporate somosboro.com mailbox")
    if not 0 < strategy.daily_cap <= 20:
        problems.append(f"daily cap {strategy.daily_cap} is outside 1..20")
    if abs(sum(strategy.lane_mix.values()) - 1) > 0.01:
        problems.append("lane mix does not sum to 1")
    for lane in ("PARTNER_DELIVERY", "EXPLICIT_DEMAND", "TRIGGER_OVERFLOW"):
        try:
            strategy.cadence(lane)
        except KeyError:
            problems.append(f"no cadence for {lane}")
    return problems


def report(session: Session) -> dict[str, Any]:
    settings = engine_settings()
    strategy = active_strategy()
    cal = BusinessCalendar(strategy)
    moment = now_utc()
    checks: dict[str, dict[str, Any]] = {}
    warnings: dict[str, dict[str, Any]] = {}

    # --- Gmail -----------------------------------------------------------
    creds = settings.gmail_credentials
    exists = bool(creds) and Path(creds).is_file()
    private = exists and (os.name != "posix" or not Path(creds).stat().st_mode & 0o077)
    last_ingest = session.scalar(select(CeTaskRun).where(
        CeTaskRun.task == "ingest", CeTaskRun.status == "OK").order_by(
        CeTaskRun.started_at.desc()).limit(1))
    read_gmail = (last_ingest is not None and "seen" in _task_result(last_ingest)
                  and moment - last_ingest.started_at < timedelta(hours=1))
    checks["gmail_oauth_connected"] = {
        "ok": exists and private and read_gmail, "credentials_file": exists,
        "file_private": private,
        "last_successful_read": last_ingest.started_at if read_gmail else None}

    backfill = controls.get(session, BACKFILL_KEY) or {}
    since = date.fromisoformat(backfill["since"]) if backfill.get("since") else None
    checks["gmail_history_backfill_complete"] = {
        "ok": bool(backfill.get("complete")) and since is not None and since <= HISTORY_SINCE,
        "since": backfill.get("since"), "messages": backfill.get("seen"),
        "completed_at": backfill.get("at")}

    reconciled = controls.get(session, RECONCILIATION_KEY) or {}
    stuck = session.scalar(select(func.count()).select_from(CeMessage).where(
        CeMessage.status == "SENDING")) or 0
    failed = session.scalar(select(func.count()).select_from(CeMessage).where(
        CeMessage.status == "FAILED")) or 0
    after_backfill = (_at(reconciled.get("at")) or datetime.min.replace(tzinfo=moment.tzinfo)
                      ) >= (_at(backfill.get("at")) or moment)
    checks["gmail_reconciliation_clean"] = {
        "ok": bool(reconciled) and after_backfill and stuck == 0 and failed == 0,
        "reconciled_at": reconciled.get("at"), "adopted_sequences": reconciled.get("adopted"),
        "stuck_sending": stuck, "failed_messages": failed}
    if reconciled.get("recent_without_target") or reconciled.get("terminal_but_contacted"):
        warnings["external_sequences_engine_cannot_follow_up"] = {
            "recent_without_target": reconciled.get("recent_without_target"),
            "terminal_but_contacted": reconciled.get("terminal_but_contacted"),
            "meaning": "Recent ChatGPT sequences to companies the engine has no open "
                       "pursuit for. The engine will never re-contact them; it will not "
                       "send their follow-ups either. Import them or accept that."}

    from boro_gtm.engine.actions import adopted_review_list, sister_review_list

    adopted = adopted_review_list(session)
    if adopted:
        warnings["adopted_threads_awaiting_review"] = {
            "targets": adopted,
            "meaning": "Gmail threads the engine took over; their follow-ups are parked until "
                       "you confirm them: gtm engine adopted-review <target_id> "
                       "--decision continue|stop"}
    held = sister_review_list(session)
    if held:
        warnings["held_for_sister_domain_review"] = {
            "targets": held,
            "meaning": "Gmail shows a send only to a same-name domain under another TLD. "
                       "Decide with: gtm engine sister-review <target_id> "
                       "--decision same|different"}

    # --- duplicate-send risk -----------------------------------------------
    risky = []
    planned = session.execute(select(CeTarget, CeContact, CeAccount).join(
        CeContact, CeContact.id == CeTarget.contact_id).join(
        CeAccount, CeAccount.id == CeTarget.account_id).where(CeTarget.state == S.READY))
    for target, contact, account in planned:
        reason = prior_contact(session, contact.email, email_domain(contact.email) or "",
                               account.domain)
        if reason or suppression_for(session, contact.email) or is_role_mailbox(
                contact.email, contact_role=contact.role):
            risky.append({"target_id": str(target.id), "domain": account.domain,
                          "reason": reason or "suppressed_or_role_mailbox"})
    duplicates = session.scalar(select(func.count()).select_from(
        select(CeMessage.target_id).where(CeMessage.step == "INITIAL")
        .group_by(CeMessage.target_id).having(func.count() > 1).subquery())) or 0
    checks["no_duplicate_send_risk"] = {"ok": not risky and duplicates == 0,
                                        "ready_targets_already_contacted_or_unsafe": risky[:20],
                                        "targets_with_two_initials": duplicates}

    # --- shadow ------------------------------------------------------------
    start = _at(backfill.get("at")) if backfill.get("complete") else None
    days: set[date] = set()
    if start is not None:
        for due in session.scalars(select(CeMessage.due_at).where(
                CeMessage.status == "SHADOW", CeMessage.step == "INITIAL",
                CeMessage.due_at >= start)):
            day = cal.business_day_of(due)
            if cal.is_business_day(day):
                days.add(day)
    checks["shadow_ran_two_business_days_after_backfill"] = {
        "ok": len(days) >= SHADOW_BUSINESS_DAYS, "business_days": sorted(days),
        "counted_from": backfill.get("at")}

    audit = controls.get(session, controls.READY_AUDIT_KEY) or {}
    sample = int(audit.get("sample", 0))
    failing = {k: audit.get("metrics", {}).get(k) for k, floor in AUDIT_THRESHOLDS.items()
               if sample == 0 or audit.get("metrics", {}).get(k, 0) / sample < floor}
    checks["ready_audit_precision"] = {
        "ok": sample >= AUDIT_MIN_SAMPLE and not failing, "sample": sample,
        "below_threshold": failing, "audited_at": audit.get("at")}

    parity = (controls.get(session, controls.PARITY_KEY) or {}).get("days", {})
    parity_days = [d for d, v in parity.items() if v.get("business_day")
                   and start is not None and date.fromisoformat(d) >= start.date()]
    criticals = sum(v.get("critical_mismatches", 0) for v in parity.values())
    checks["shadow_parity_no_critical_mismatch"] = {
        "ok": len(parity_days) >= SHADOW_BUSINESS_DAYS and criticals == 0,
        "days_compared": sorted(parity_days), "critical_mismatches": criticals}

    # --- data and handling -------------------------------------------------
    suppressions = session.scalar(select(func.count()).select_from(CeSuppression)) or 0
    checks["suppressions_loaded"] = {"ok": bool(backfill.get("complete")),
                                     "suppressions": suppressions,
                                     "note": "bounces and opt-outs in the Gmail backfill"}
    suppressed_ready = session.scalar(
        select(func.count()).select_from(CeTarget).join(CeContact,
                                                        CeContact.id == CeTarget.contact_id)
        .where(CeTarget.state == S.READY, CeContact.email.in_(
            select(CeSuppression.value).where(CeSuppression.kind == "EMAIL")))) or 0
    checks["no_suppressed_contact_is_ready"] = {"ok": suppressed_ready == 0,
                                                "found": suppressed_ready}

    verified = controls.get(session, controls.VERIFY_KEY) or {}
    fresh = (_at(verified.get("at")) or datetime.min.replace(tzinfo=moment.tzinfo)
             ) > moment - timedelta(days=7)
    checks["bounce_and_opt_out_handling_tested"] = {
        "ok": fresh and bool((verified.get("reply_handling") or {}).get("ok")),
        "verified_at": verified.get("at")}
    checks["followup_scheduler_tested_against_real_dates"] = {
        "ok": fresh and bool((verified.get("followup_dates") or {}).get("ok")),
        "problems": (verified.get("followup_dates") or {}).get("problems")}
    outside = [m.id for m in session.scalars(select(CeMessage).where(
        CeMessage.status == "SCHEDULED")) if not cal.within_hours(m.due_at)]
    checks["every_scheduled_message_is_in_business_hours"] = {"ok": not outside,
                                                              "outside": len(outside)}

    # --- operations --------------------------------------------------------
    beats = {b.worker: b.last_seen_at for b in session.scalars(select(CeWorkerHeartbeat))}
    outreach = beats.get("outreach")
    sourcing = beats.get("sourcing")
    recent_sources = list(session.scalars(select(CeSourceRun).where(
        CeSourceRun.started_at > moment - timedelta(days=1))))
    checks["workers_healthy"] = {
        "ok": (outreach is not None and moment - outreach < timedelta(minutes=5)
               and sourcing is not None and moment - sourcing < timedelta(minutes=45)
               and any(r.status in ("COMPLETED", "EXHAUSTED") for r in recent_sources)),
        "outreach_heartbeat": outreach, "sourcing_heartbeat": sourcing,
        "source_runs_last_24h": len(recent_sources)}
    starts = controls.get(session, controls.WORKER_STARTS_KEY) or {}
    restarted = {}
    for worker in ("outreach", "sourcing"):
        history = starts.get(worker, [])
        last = _at(history[-1]["at"]) if history else None
        ok_after = last is not None and session.scalar(select(CeTaskRun.id).where(
            CeTaskRun.worker == worker, CeTaskRun.status == "OK",
            CeTaskRun.started_at > last).limit(1)) is not None
        restarted[worker] = {"starts_recorded": len(history), "ok_after_last_start": ok_after}
    checks["worker_restart_tested"] = {
        "ok": all(v["starts_recorded"] >= 2 and v["ok_after_last_start"]
                  for v in restarted.values()), **restarted}

    backup = controls.get(session, controls.BACKUP_KEY) or {}
    backup_at = _at(backup.get("at"))
    checks["database_backup_exists"] = {
        "ok": backup_at is not None and moment - backup_at < timedelta(hours=26),
        "last_backup": backup.get("at"), "bytes": backup.get("bytes")}
    restore = controls.get(session, controls.RESTORE_KEY) or {}
    restore_at = _at(restore.get("at"))
    checks["restore_test_passed"] = {
        "ok": bool(restore.get("ok")) and restore_at is not None
        and moment - restore_at < timedelta(days=8), "last_restore_test": restore.get("at")}
    remote_copy = bool(backup.get("remote_ok")) and backup_at is not None and (
        moment - backup_at < timedelta(hours=26))
    # Or the operator's PC pulled a dump, verified it by SHA-256 and recorded it. The PC
    # pulls when it is on, so the copied dump may be up to two nightly runs old.
    pc = controls.get(session, controls.OFF_HOST_COPY_KEY) or {}
    pc_backup_at = _at(pc.get("backup_at"))
    pc_copy = bool(pc.get("verified")) and pc_backup_at is not None and (
        moment - pc_backup_at < timedelta(hours=PC_COPY_MAX_AGE_HOURS))
    off_host = remote_copy or pc_copy
    off_host_check = {"ok": off_host, "remote": backup.get("remote"),
                      "pc_copy": {"ok": pc_copy, "backup_at": pc.get("backup_at"),
                                  "copied_at": pc.get("at")},
                      "decision": "blocks real sending, not shadow (CE-ADR-018)"}
    if OFF_HOST_BACKUP_BLOCKS_LIVE:
        checks["off_host_backup"] = off_host_check
    elif not off_host:
        warnings["off_host_backup"] = {**off_host_check, "severity": "HIGH"}

    from boro_gtm.core.db import get_engine
    from boro_gtm.core.schema_check import check_schema

    schema = check_schema(get_engine())
    checks["schema_matches_migrations"] = {"ok": schema.ok}
    problems = _strategy_problems(strategy)
    checks["active_strategy_valid"] = {"ok": not problems, "version": strategy.version,
                                       "problems": problems}

    ready_for_live = all(c["ok"] for c in checks.values())
    failing_checks = sorted(k for k, c in checks.items() if not c["ok"])
    return {"shadow_mode": settings.shadow_mode, "ready_for_live": ready_for_live,
            "failing": failing_checks, "checks": checks, "warnings": warnings,
            "next_step": ("Disable the ChatGPT commercial automations (Demand Radar, Lead "
                          "Enrichment, Initial Outreach, Follow-ups & Replies), wait for their "
                          "last run to finish, run `gtm engine run ingest`, re-run this report, "
                          "then set GTM_ENGINE_SHADOW_MODE=false and restart the worker."
                          if ready_for_live else
                          "Keep SHADOW_MODE on. Fix the failing checks first.")}
