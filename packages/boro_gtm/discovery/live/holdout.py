"""Offline evaluation of a blind discovery run against known accounts.

The holdout is **evaluation data**. It is never a provider, never loaded into M2
before a run, and never used to build queries, pick metros or seed domains. A
discovery run that had seen it would be scoring itself (M2-ADR-045).

The only thing that counts as recovery is an exact normalized **domain** match.
Name similarity is reported as a diagnostic and never as a hit: two contractors
called "Allied Mechanical" are two companies, and counting a name match would
turn the one thing M2 refuses to do into a success metric.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from boro_gtm.discovery.domain.models import CompanyDomain, CompanyName
from boro_gtm.discovery.services.resolution import normalize_domain, normalize_name

HOLDOUT_COLUMNS = ("company_name", "canonical_domain")

#: Reported, never counted. High enough to be interesting, and it still proves
#: nothing.
NAME_SIMILARITY_FLOOR = 0.88


@dataclass(frozen=True, slots=True)
class HoldoutRow:
    company_name: str
    canonical_domain: str | None
    line: int


@dataclass
class HoldoutEvaluation:
    holdout_total: int = 0
    holdout_without_domain: int = 0
    recovered_by_domain: list[tuple[str, str]] = field(default_factory=list)
    not_recovered: list[tuple[str, str | None]] = field(default_factory=list)
    duplicate_recovery: list[tuple[str, int]] = field(default_factory=list)
    wrong_domain_conflicts: list[tuple[str, str, str]] = field(default_factory=list)
    #: Diagnostic only. Never counted as recovery.
    name_only_matches: list[tuple[str, str, float]] = field(default_factory=list)
    discovered_companies: int = 0
    discovered_with_domain: int = 0
    new_outside_holdout: int = 0

    @property
    def recall(self) -> float:
        if not self.holdout_total:
            return 0.0
        return round(len(self.recovered_by_domain) / self.holdout_total, 4)

    def as_dict(self) -> dict:
        return {
            "holdout_total": self.holdout_total,
            "holdout_without_domain": self.holdout_without_domain,
            "recovered_by_domain": len(self.recovered_by_domain),
            "not_recovered": len(self.not_recovered),
            "duplicate_recovery": len(self.duplicate_recovery),
            "wrong_domain_conflicts": len(self.wrong_domain_conflicts),
            "recall": self.recall,
            "discovered_companies": self.discovered_companies,
            "discovered_with_domain": self.discovered_with_domain,
            "new_outside_holdout": self.new_outside_holdout,
            "missing_accounts": [
                {"company_name": n, "canonical_domain": d}
                for n, d in self.not_recovered
            ],
            "recovered_accounts": [
                {"company_name": n, "canonical_domain": d}
                for n, d in self.recovered_by_domain
            ],
            "duplicates": [
                {"canonical_domain": d, "companies": n}
                for d, n in self.duplicate_recovery
            ],
            "name_only_diagnostic": [
                {"holdout": h, "discovered": d, "similarity": s}
                for h, d, s in self.name_only_matches
            ],
        }


def read_holdout_csv(path: Path | str) -> tuple[list[HoldoutRow], list[tuple[str, str]]]:
    rows: list[HoldoutRow] = []
    problems: list[tuple[str, str]] = []
    with open(path, newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None or "company_name" not in reader.fieldnames:
            raise ValueError(
                f"the holdout CSV must have a `company_name` column; got "
                f"{reader.fieldnames}. Expected header: {','.join(HOLDOUT_COLUMNS)}"
            )
        for number, raw in enumerate(reader, start=2):
            name = (raw.get("company_name") or "").strip()
            if not name:
                problems.append((f"line {number}", "no company_name"))
                continue
            rows.append(HoldoutRow(
                company_name=name,
                canonical_domain=normalize_domain(
                    (raw.get("canonical_domain") or "").strip() or None
                ),
                line=number,
            ))
    return rows, problems


def evaluate(session: Session, holdout: list[HoldoutRow]) -> HoldoutEvaluation:
    """Compare the discovered canonical set against the holdout. Read-only."""
    result = HoldoutEvaluation(holdout_total=len(holdout))

    # Discovered identity domains, and which company each belongs to.
    by_domain: dict[str, list] = {}
    for company_id, domain in session.execute(
        select(CompanyDomain.company_id, CompanyDomain.domain_normalized)
        .where(CompanyDomain.domain_role == "IDENTITY")
    ).all():
        by_domain.setdefault(domain, []).append(company_id)

    names_by_company: dict[object, str] = {}
    for company_id, raw_name in session.execute(
        select(CompanyName.company_id, CompanyName.name_raw)
        .order_by(CompanyName.is_primary.desc(), CompanyName.name_normalized)
    ).all():
        names_by_company.setdefault(company_id, raw_name)

    result.discovered_companies = len({
        cid for ids in by_domain.values() for cid in ids
    } | set(names_by_company))
    result.discovered_with_domain = len({
        cid for ids in by_domain.values() for cid in ids
    })

    holdout_domains = {r.canonical_domain for r in holdout if r.canonical_domain}
    for row in holdout:
        if not row.canonical_domain:
            # No domain in the evaluation data: it cannot be scored either way,
            # and counting it as a miss would blame discovery for a gap in the
            # holdout itself.
            result.holdout_without_domain += 1
            continue
        owners = by_domain.get(row.canonical_domain, [])
        if owners:
            result.recovered_by_domain.append((row.company_name, row.canonical_domain))
            if len(set(owners)) > 1:
                # One domain held as IDENTITY by two companies would be an M2
                # invariant violation; reported loudly rather than averaged in.
                result.duplicate_recovery.append(
                    (row.canonical_domain, len(set(owners)))
                )
            continue

        result.not_recovered.append((row.company_name, row.canonical_domain))
        target = normalize_name(row.company_name)
        for company_id, raw_name in names_by_company.items():
            similarity = SequenceMatcher(
                None, target, normalize_name(raw_name) or ""
            ).ratio()
            if similarity >= NAME_SIMILARITY_FLOOR:
                result.name_only_matches.append(
                    (row.company_name, raw_name, round(similarity, 3))
                )
                owned = [d for d, ids in by_domain.items() if company_id in ids]
                for domain in owned:
                    if domain != row.canonical_domain:
                        # Same-looking name on a different domain. Either the
                        # holdout's domain is stale or these are two companies —
                        # a human decides, and nothing is merged.
                        result.wrong_domain_conflicts.append(
                            (row.company_name, row.canonical_domain, domain)
                        )

    result.new_outside_holdout = len({
        cid for domain, ids in by_domain.items() if domain not in holdout_domains
        for cid in ids
    })
    return result
