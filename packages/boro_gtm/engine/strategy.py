"""The active commercial strategy, versioned and hashed (CE-ADR-010).

Runtime reads exactly one file: `commercial/strategies/active-*.json`, named by
`GTM_ENGINE_STRATEGY`. The 2026-09 doctrine (HVAC/Energy ICP, Architecture
Sprint entry offer) stays in the repository as history and is never loaded here.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from boro_gtm.core.config import REPO_ROOT

DEFAULT_STRATEGY = REPO_ROOT / "commercial" / "strategies" / "active-2026-11.2.json"


@dataclass(frozen=True)
class Strategy:
    raw: dict[str, Any]
    sha256: str
    path: Path

    @property
    def version(self) -> str:
        return self.raw["strategy_version"]

    @property
    def timezone(self) -> str:
        return self.raw["timezone"]

    @property
    def holidays(self) -> list[str]:
        return self.raw["holidays"]

    @property
    def business_hours(self) -> tuple[str, str]:
        hours = self.raw["business_hours"]
        return hours["start"], hours["end"]

    @property
    def sender(self) -> dict[str, Any]:
        return self.raw["sender"]

    @property
    def daily_cap(self) -> int:
        volume = self.raw["volume"]
        return min(volume["initial_contacts_per_business_day"],
                   volume["max_initial_contacts_per_business_day"])

    @property
    def min_minutes_between_sends(self) -> int:
        return self.raw["volume"]["min_minutes_between_sends"]

    @property
    def lane_mix(self) -> dict[str, float]:
        return self.raw["volume"]["lane_mix"]

    @property
    def threshold(self) -> int:
        return self.raw["qualification"]["threshold"]

    @property
    def high_priority(self) -> int:
        return self.raw["qualification"]["high_priority"]

    def cadence(self, lane: str) -> dict[str, int]:
        return self.raw["cadence"][lane]

    def template(self, lane: str) -> dict[str, Any]:
        return self.raw["templates"][lane]

    def terms(self, key: str) -> list[str]:
        return self.raw[key]

    @property
    def jurisdictions(self) -> dict[str, Any]:
        return self.raw["jurisdictions"]

    @property
    def deliverability(self) -> dict[str, Any]:
        return self.raw["deliverability"]


def load_strategy(path: Path | str | None = None) -> Strategy:
    target = Path(path) if path else DEFAULT_STRATEGY
    data = target.read_bytes()
    raw = json.loads(data)
    if raw.get("status") != "ACTIVE":
        raise ValueError(f"{target.name} is not an ACTIVE strategy")
    return Strategy(raw=raw, sha256=hashlib.sha256(data).hexdigest(), path=target)


@lru_cache(maxsize=1)
def active_strategy() -> Strategy:
    import os

    return load_strategy(os.environ.get("GTM_ENGINE_STRATEGY") or None)
