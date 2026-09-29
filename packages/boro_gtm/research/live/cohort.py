"""Load BoRo's own target cohort as canonical M2 companies.

A cohort is a commercial asset: it is the list of accounts BoRo has decided to
pursue. This module's only job is to get that list into M2 without inventing any
part of it — above all, without inventing a domain.

A row with no verified domain is **not researched**. It is loaded and flagged for
identity review, because a company name is not an address: two contractors
called "Allied Mechanical" are two companies, and guessing which website belongs
to which produces wrong-account evidence with a complete, internally consistent
provenance chain and no way to tell (M3-ADR-074).
"""

from __future__ import annotations

import csv
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from boro_gtm.discovery.domain.models import Company, CompanyDomain, CompanyName
from boro_gtm.discovery.services.resolution import is_identity_domain, normalize_domain

#: The columns an operator supplies. `company_name` and one of
#: `canonical_domain` / `website_url` are what make a row researchable; the rest
#: are optional provenance.
COHORT_COLUMNS = ("company_name", "canonical_domain", "website_url", "source_id")


@dataclass(frozen=True, slots=True)
class CohortRow:
    """One line of the operator's list, before anything is decided about it."""

    company_name: str
    canonical_domain: str | None = None
    website_url: str | None = None
    source_id: str | None = None
    line: int = 0


@dataclass
class CohortLoadReport:
    """What was loaded, what was refused, and what a human has to look at."""

    loaded: list[tuple[str, uuid.UUID, str]] = field(default_factory=list)
    needs_identity_review: list[tuple[str, str]] = field(default_factory=list)
    rejected: list[tuple[str, str]] = field(default_factory=list)
    already_present: list[tuple[str, uuid.UUID]] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "loaded": [
                {"company_name": n, "company_id": str(i), "domain": d}
                for n, i, d in self.loaded
            ],
            "needs_identity_review": [
                {"company_name": n, "reason": r} for n, r in self.needs_identity_review
            ],
            "rejected": [{"row": n, "reason": r} for n, r in self.rejected],
            "already_present": [
                {"company_name": n, "company_id": str(i)} for n, i in self.already_present
            ],
            "counts": {
                "loaded": len(self.loaded),
                "needs_identity_review": len(self.needs_identity_review),
                "rejected": len(self.rejected),
                "already_present": len(self.already_present),
            },
        }


def read_cohort_csv(path: Path | str) -> tuple[list[CohortRow], list[tuple[str, str]]]:
    """Parse the operator's CSV. Unknown columns are ignored, not guessed."""
    rows: list[CohortRow] = []
    problems: list[tuple[str, str]] = []
    with open(path, newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None or "company_name" not in reader.fieldnames:
            raise ValueError(
                f"the cohort CSV must have a `company_name` column; got "
                f"{reader.fieldnames}. Expected header: {','.join(COHORT_COLUMNS)}"
            )
        for number, raw in enumerate(reader, start=2):
            name = (raw.get("company_name") or "").strip()
            if not name:
                problems.append((f"line {number}", "no company_name"))
                continue
            rows.append(CohortRow(
                company_name=name,
                canonical_domain=(raw.get("canonical_domain") or "").strip() or None,
                website_url=(raw.get("website_url") or "").strip() or None,
                source_id=(raw.get("source_id") or "").strip() or None,
                line=number,
            ))
    return rows, problems


def _domain_of(row: CohortRow) -> str | None:
    """The domain the operator supplied, normalized. Never derived from a name."""
    candidate = row.canonical_domain
    if not candidate and row.website_url:
        # A URL is an address the operator asserted, so its host is theirs too.
        from urllib.parse import urlsplit

        parsed = urlsplit(row.website_url if "//" in row.website_url
                          else f"https://{row.website_url}")
        candidate = parsed.hostname
    return normalize_domain(candidate) if candidate else None


def load_cohort(
    session: Session, rows: list[CohortRow], *, now: datetime,
    identity_policy_version: str = "1.0",
) -> CohortLoadReport:
    """Create or find one M2 company per row, under M2's own identity rules.

    Nothing here researches anything. Loading a cohort and researching it are
    separate operator actions, so a bad list is discovered before it reaches the
    internet.
    """
    report = CohortLoadReport()
    for row in rows:
        domain = _domain_of(row)
        if not domain:
            # The one case that matters most: no address, so no research. The
            # company is still recorded, because BoRo targeting it is a fact.
            report.needs_identity_review.append(
                (row.company_name, "no canonical_domain or website_url supplied")
            )
            continue
        if not is_identity_domain(domain):
            # A shared platform host (`*.wixsite.com`, a franchise portal) does
            # not identify a company, and M2 would record it as GROUP.
            report.needs_identity_review.append(
                (row.company_name,
                 f"{domain} is a shared host and cannot carry identity")
            )
            continue

        owner = session.scalar(
            select(CompanyDomain.company_id).where(
                CompanyDomain.domain_normalized == domain,
                CompanyDomain.domain_role == "IDENTITY",
            )
        )
        if owner is not None:
            report.already_present.append((row.company_name, owner))
            continue

        company = Company(
            id=uuid.uuid4(), created_at=now,
            identity_policy_version=identity_policy_version,
            lifecycle_status="ACTIVE",
        )
        session.add(company)
        session.flush()
        session.add(CompanyName(
            company_id=company.id, name_normalized=row.company_name.casefold(),
            name_type="LEGAL", name_raw=row.company_name, is_primary=True,
            derived_from_claim_ids=[],
        ))
        session.add(CompanyDomain(
            company_id=company.id, domain_normalized=domain,
            domain_role="IDENTITY", derived_from_claim_ids=[],
        ))
        session.flush()
        report.loaded.append((row.company_name, company.id, domain))
    return report
