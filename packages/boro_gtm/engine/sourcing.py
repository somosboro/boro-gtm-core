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
from boro_gtm.engine.sources.odoo_partners import OdooPartnerSource
from boro_gtm.engine.strategy import Strategy


def default_ladder() -> list[SourceAdapter]:
    """Ordered by expected yield of contactable, allowed-jurisdiction targets."""
    return [GetOnBoardSource()] + [OdooPartnerSource(c) for c in
                                   ("CL", "PE", "CO", "MX", "AR", "ES")]


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
            if page.exhausted:
                run.cursor_exhausted = True
                break
            if run.accepted >= wanted:
                break
        run.status = "EXHAUSTED" if run.cursor_exhausted else "COMPLETED"
    except Exception as exc:  # noqa: BLE001 - a broken source is a record, not a crash
        run.status = "FAILED"
        run.error = f"{type(exc).__name__}: {exc}"[:2000]
    run.rejection_reasons = dict(reasons)
    run.cursor_after = dict(cursor.cursor)
    run.finished_at = now_utc()
    session.flush()
    return run


def run_ladder(session: Session, strategy: Strategy, fetcher: Fetcher, *,
               target_new: int | None = None, ladder: list[SourceAdapter] | None = None,
               max_pages_per_source: int = 5) -> dict[str, Any]:
    wanted = target_new or strategy.raw["volume"]["new_qualified_candidates_per_day"]
    accepted = 0
    runs = []
    for adapter in ladder or default_ladder():
        if accepted >= wanted:
            break
        run = run_source(session, strategy, adapter, fetcher, wanted=wanted - accepted,
                         max_pages=max_pages_per_source)
        accepted += run.accepted
        runs.append({"source": run.source_key, "status": run.status, "accepted": run.accepted,
                     "inspected": run.results_inspected, "duplicates": run.duplicates,
                     "expired": run.expired, "rejected": run.rejected,
                     "rejection_reasons": run.rejection_reasons,
                     "cursor_exhausted": run.cursor_exhausted, "queries": len(run.queries),
                     "error": run.error})
    remaining = [a.key for a in (ladder or default_ladder())
                 if a.key not in {r["source"] for r in runs}]
    return {"wanted": wanted, "accepted": accepted, "runs": runs,
            "remaining_sources": remaining,
            "below_target_reason": None if accepted >= wanted else (
                "ladder_exhausted" if not remaining else "page_budget_per_source_reached")}


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
