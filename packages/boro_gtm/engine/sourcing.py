"""Source runs, dedupe and the source ladder (CE-ADR-009).

A run that finds fewer candidates than asked for says why: which sources it
tried, which queries, how many results it inspected, how many were duplicates,
expired or rejected and for what reason, and whether each source's cursor is
exhausted. "No results" is a record, not an excuse, and the ladder keeps going
until it has the target number or every source on it is exhausted.
"""

from __future__ import annotations

import hashlib
from collections import Counter
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from boro_gtm.engine import states as S
from boro_gtm.engine.models import CeSignal, CeSourceCursor, CeSourceRun, CeTarget
from boro_gtm.engine.pipeline import account_key, now_utc, target_from_signal
from boro_gtm.engine.sources.base import Fetcher, SourceAdapter
from boro_gtm.engine.sources.getonboard import GetOnBoardSource
from boro_gtm.engine.sources.getonboard_more import (
    GetOnBoardCompaniesSource,
    GetOnBoardHiringSource,
)
from boro_gtm.engine.sources.hn_hiring import HnHiringSource
from boro_gtm.engine.sources.manual import MANUAL_SOURCES
from boro_gtm.engine.sources.odoo_partners import OdooPartnerSource
from boro_gtm.engine.strategy import Strategy

ODOO_COUNTRIES = ("CL", "PE", "CO", "MX", "AR", "UY", "EC", "BO", "PY", "CR", "PA", "GT",
                  "DO", "ES")


def default_ladder() -> list[SourceAdapter]:
    """Every automated source, grouped by lane by `run_ladder`. Order inside a lane
    is expected yield of contactable, allowed-jurisdiction targets."""
    return [GetOnBoardSource(), GetOnBoardHiringSource(), HnHiringSource(),
            GetOnBoardCompaniesSource()] + [OdooPartnerSource(c) for c in ODOO_COUNTRIES]


def _cursor(session: Session, key: str) -> CeSourceCursor:
    row = session.get(CeSourceCursor, key)
    if row is None:
        row = CeSourceCursor(source_key=key, cursor={}, updated_at=now_utc())
        session.add(row)
        session.flush()
    return row


def run_source(session: Session, strategy: Strategy, adapter: SourceAdapter, fetcher: Fetcher,
               *, wanted: int, max_pages: int = 5) -> CeSourceRun:
    cursor = _cursor(session, adapter.key)
    run = CeSourceRun(source_key=adapter.key, lane=adapter.lane, status="RUNNING",
                      requested=wanted, queries=[], cursor_before=dict(cursor.cursor),
                      started_at=now_utc())
    session.add(run)
    session.flush()
    reasons: Counter[str] = Counter()
    try:
        for _ in range(max_pages):
            if cursor.cursor.get("exhausted_at"):
                run.cursor_exhausted = True
                break
            if cursor.cursor.get("blocked_until", "") > now_utc().isoformat():
                run.queries = [*run.queries, {"skipped": "blocked_until",
                                              "until": cursor.cursor["blocked_until"],
                                              "error": cursor.cursor.get("blocked_reason")}]
                break
            page = adapter.next_page(fetcher, cursor.cursor)
            run.queries = [*run.queries, {**page.query, "items": len(page.items),
                                          "error": page.error}]
            run.pages_fetched += page.pages_fetched
            for draft in page.items:
                run.results_inspected += 1
                exists = session.scalar(select(CeSignal.id).where(
                    CeSignal.source_key == adapter.key, CeSignal.external_id == draft.external_id))
                if exists:
                    run.duplicates += 1
                    continue
                signal = CeSignal(
                    source_key=adapter.key, external_id=draft.external_id, kind=draft.kind,
                    lane=draft.lane, url=draft.url, title=draft.title, excerpt=draft.excerpt,
                    company_name=draft.company_name,
                    company_domain=account_key(draft.company_domain),
                    country=draft.country, posted_at=draft.posted_at,
                    expires_at=draft.expires_at, observed_at=now_utc(), source_run_id=run.id,
                    attributes={**draft.attributes, "raw_domain": draft.company_domain},
                )
                session.add(signal)
                session.flush()
                if draft.reject_reason:
                    run.rejected += 1
                    reasons[draft.reject_reason] += 1
                    continue
                target, outcome = target_from_signal(session, strategy, signal)
                if outcome in ("CREATED", "REOPENED"):
                    run.accepted += 1
                elif outcome == "EXPIRED":
                    run.expired += 1
                elif outcome == "ATTACHED":
                    run.duplicates += 1
                else:
                    run.rejected += 1
                    reasons[outcome.lower()] += 1
            cursor.cursor = (dict(page.cursor_after) | {"exhausted_at": now_utc().isoformat()}
                             if page.exhausted else dict(page.cursor_after))
            cursor.updated_at = now_utc()
            # Each page's signals, targets and the cursor that follows them are
            # durable together, so an interrupted pass resumes, not restarts.
            session.commit()
            if page.exhausted:
                run.cursor_exhausted = True
                break
            if run.accepted >= wanted:
                break
        attempts = [q for q in run.queries if not q.get("skipped")]
        errors = [q for q in attempts if q.get("error")]
        if run.queries and not attempts:
            run.status = "BLOCKED"  # still resting from an earlier block
        elif attempts and len(errors) == len(attempts) and run.results_inspected == 0:
            # Every request failed. That is the site's answer, not ours to work
            # around: the source rests for a week and the run says so.
            run.status = "BLOCKED"
            cursor.cursor = {**cursor.cursor, "blocked_until": (now_utc() + timedelta(days=7))
                             .isoformat(), "blocked_reason": errors[-1]["error"]}
        else:
            run.status = "EXHAUSTED" if run.cursor_exhausted else "COMPLETED"
    except Exception as exc:  # noqa: BLE001 - a broken source is a record, not a crash
        run.status = "FAILED"
        run.error = f"{type(exc).__name__}: {exc}"[:2000]
    run.rejection_reasons = dict(reasons)
    run.cursor_after = dict(cursor.cursor)
    run.finished_at = now_utc()
    session.flush()
    return run


def lane_targets(strategy: Strategy, wanted: int) -> dict[str, int]:
    mix = strategy.lane_mix
    quotas = {lane: int(wanted * share) for lane, share in mix.items()}
    for lane in sorted(mix, key=lambda k: -mix[k]):
        if sum(quotas.values()) >= wanted:
            break
        quotas[lane] += 1
    return quotas


def run_ladder(session: Session, strategy: Strategy, fetcher: Fetcher, *,
               target_new: int | None = None, ladder: list[SourceAdapter] | None = None,
               max_pages_per_source: int = 5) -> dict[str, Any]:
    """Source each lane toward its share of the day's target, and say what fell short.

    A lane short of its share is never filled from another lane here, and
    every source on the lane is tried before the shortage is reported, with
    what each one inspected, rejected and why, and where its cursor stands.
    """
    wanted = target_new or strategy.raw["volume"]["new_qualified_candidates_per_day"]
    adapters = ladder or default_ladder()
    quotas = lane_targets(strategy, wanted)
    runs: list[dict[str, Any]] = []
    not_attempted: list[dict[str, str]] = []
    lanes: dict[str, dict[str, Any]] = {}
    for lane, quota in quotas.items():
        accepted = 0
        for adapter in [a for a in adapters if a.lane == lane]:
            if accepted >= quota:
                not_attempted.append({"source": adapter.key, "lane": lane,
                                      "reason": "lane_share_already_met"})
                continue
            run = run_source(session, strategy, adapter, fetcher, wanted=quota - accepted,
                             max_pages=max_pages_per_source)
            accepted += run.accepted
            reasons = run.rejection_reasons or {}
            runs.append({"source": run.source_key, "lane": lane,
                         "family": getattr(adapter, "family", None), "status": run.status,
                         "pages": run.pages_fetched, "inspected": run.results_inspected,
                         "accepted": run.accepted, "duplicates": run.duplicates,
                         "expired": run.expired, "rejected": run.rejected,
                         "rejected_employment_only": reasons.get(
                             "full_time_employment_not_vendor_demand", 0),
                         "rejection_reasons": reasons,
                         "cursor_exhausted": run.cursor_exhausted,
                         "next_cursor": run.cursor_after, "error": run.error})
        lane_runs = [r for r in runs if r["lane"] == lane]
        reason = None
        if accepted < quota:
            if not lane_runs:
                reason = "no_automated_source_for_lane"
            elif all(r["cursor_exhausted"] for r in lane_runs):
                reason = "every_source_on_lane_exhausted"
            elif all(r["cursor_exhausted"] or r["status"] == "BLOCKED" for r in lane_runs):
                reason = "remaining_sources_blocked_by_site"
            else:
                reason = "page_budget_per_source_reached"
        lanes[lane] = {"target": quota, "accepted": accepted,
                       "shortage": max(0, quota - accepted), "reason": reason}
    accepted_total = sum(v["accepted"] for v in lanes.values())
    return {"wanted": wanted, "accepted": accepted_total, "lanes": lanes, "runs": runs,
            "not_attempted": not_attempted,
            "manual_sources": [{"source": m["key"], "lane": m["lane"], "reason": m["reason"]}
                               for m in MANUAL_SOURCES],
            # A lane short of its share is reported even when other lanes made
            # up the total: the mix is what is being measured.
            "below_target_reason": "; ".join(f"{k}: {v['reason']}" for k, v in lanes.items()
                                             if v["reason"]) or None}


def reset_cursor(session: Session, key: str) -> None:
    row = session.get(CeSourceCursor, key)
    if row is not None:
        row.cursor, row.updated_at = {}, now_utc()


def rotate_exhausted(session: Session, *, older_than: timedelta = timedelta(days=7)) -> list[str]:
    """Exhausted sources start over after a week: directories and boards change."""
    cutoff = now_utc() - older_than
    reset = []
    for row in session.scalars(select(CeSourceCursor)):
        at = row.cursor.get("exhausted_at")
        if at and at < cutoff.isoformat():
            row.cursor, row.updated_at = {}, now_utc()
            reset.append(row.source_key)
    return reset


def pool_status(session: Session) -> dict[str, int]:
    rows = session.execute(select(CeTarget.state, func.count()).group_by(CeTarget.state)).all()
    return {state: count for state, count in rows if state in S.STATES}


def signal_fingerprint(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:40]
