"""Engine runtime switches, read from the environment.

`SHADOW_MODE` defaults to **on**. The engine must be told, explicitly, to send
real email; a missing variable never means "live".
"""

from __future__ import annotations

import os
import socket
from dataclasses import dataclass


def _flag(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class EngineSettings:
    shadow_mode: bool
    sourcing_enabled: bool
    gmail_credentials: str | None
    api_token: str | None
    worker_name: str
    #: A mailbox on a domain separate from client mail (CE-ADR-022). Configured,
    #: never used until docs/SENDER_ACCOUNT.md is complete and Amaru enables it.
    sender_account: str | None = None
    sender_account_enabled: bool = False


def engine_settings() -> EngineSettings:
    return EngineSettings(
        shadow_mode=_flag("GTM_ENGINE_SHADOW_MODE", True),
        sourcing_enabled=_flag("GTM_ENGINE_SOURCING_ENABLED", True),
        gmail_credentials=os.environ.get("GTM_ENGINE_GMAIL_CREDENTIALS") or None,
        api_token=os.environ.get("GTM_ENGINE_API_TOKEN") or None,
        worker_name=os.environ.get("GTM_ENGINE_WORKER_NAME") or socket.gethostname()[:64],
        sender_account=os.environ.get("GTM_ENGINE_SENDER_ACCOUNT") or None,
        sender_account_enabled=_flag("GTM_ENGINE_SENDER_ACCOUNT_ENABLED", False),
    )
