"""Small durable facts the engine and its operators record (`ce_controls`)."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from boro_gtm.engine.models import CeControl
from boro_gtm.engine.pipeline import now_utc

BACKUP_KEY = "last_backup"            # written by deploy/vps/backup.sh
RESTORE_KEY = "last_restore_test"     # written by deploy/vps/restore-test.sh
WORKER_STARTS_KEY = "worker_starts"
VERIFY_KEY = "verification"
READY_AUDIT_KEY = "ready_audit"
PARITY_KEY = "shadow_parity"


def get(session: Session, key: str) -> dict[str, Any] | None:
    row = session.get(CeControl, key)
    return dict(row.value) if row is not None else None


def put(session: Session, key: str, value: dict[str, Any]) -> None:
    row = session.get(CeControl, key)
    if row is None:
        session.add(CeControl(key=key, value=value, updated_at=now_utc()))
    else:
        row.value, row.updated_at = value, now_utc()
    session.flush()


def record_worker_start(session: Session, worker: str, interrupted: int) -> None:
    starts = get(session, WORKER_STARTS_KEY) or {}
    history = list(starts.get(worker, []))[-9:]
    history.append({"at": now_utc().isoformat(), "interrupted_runs": interrupted})
    put(session, WORKER_STARTS_KEY, {**starts, worker: history})
