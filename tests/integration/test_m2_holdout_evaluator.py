"""Scoring a blind run against known accounts, offline.

The holdout is evaluation data. These tests prove it stays that way: it is never
loaded into M2, never reaches the provider, and a name that merely looks similar
is never counted as a recovered account.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from boro_gtm.discovery.domain.models import Company, CompanyDomain, CompanyName
from boro_gtm.discovery.live.holdout import (
    HOLDOUT_COLUMNS,
    evaluate,
    read_holdout_csv,
)

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)


def _csv(tmp_path, body: str):
    path = tmp_path / "holdout.csv"
    path.write_text(body, encoding="utf-8")
    return path


def _discovered(session, name: str, domain: str | None,
                role: str = "IDENTITY") -> uuid.UUID:
    """A canonical company as a discovery run would have left it."""
    company = Company(id=uuid.uuid4(), created_at=NOW,
                      identity_policy_version="1.0", lifecycle_status="ACTIVE")
    session.add(company)
    session.flush()
    session.add(CompanyName(
        company_id=company.id, name_normalized=name.casefold(),
        name_type="TRADING", name_raw=name, is_primary=True,
        derived_from_claim_ids=[],
    ))
    if domain:
        session.add(CompanyDomain(
            company_id=company.id, domain_normalized=domain,
            domain_role=role, derived_from_claim_ids=[],
        ))
    session.flush()
    return company.id


def test_the_documented_header_is_what_the_reader_accepts(tmp_path):
    path = _csv(tmp_path, ",".join(HOLDOUT_COLUMNS) + "\n"
                "Big Mechanical Inc,bigmechanical.com\n")
    rows, problems = read_holdout_csv(path)
    assert problems == []
    assert rows[0].company_name == "Big Mechanical Inc"
    assert rows[0].canonical_domain == "bigmechanical.com"


def test_domains_are_normalized_on_the_way_in(tmp_path):
    path = _csv(tmp_path, "company_name,canonical_domain\n"
                "A,HTTPS://WWW.BigMechanical.com/contact\n")
    rows, _ = read_holdout_csv(path)
    assert rows[0].canonical_domain == "bigmechanical.com"


def test_an_exact_domain_match_is_a_recovered_account(session, tmp_path):
    _discovered(session, "Big Mechanical Inc", "bigmechanical.com")
    path = _csv(tmp_path, "company_name,canonical_domain\n"
                "Big Mechanical Incorporated,www.bigmechanical.com\n")
    rows, _ = read_holdout_csv(path)

    result = evaluate(session, rows)
    assert result.holdout_total == 1
    assert len(result.recovered_by_domain) == 1
    assert result.not_recovered == []
    assert result.recall == 1.0


def test_a_similar_name_on_a_different_domain_is_never_a_recovery(session, tmp_path):
    """The rule. Counting this would make M2's one refusal a success metric."""
    _discovered(session, "Allied Mechanical LLC", "alliedmechanical-ohio.com")
    path = _csv(tmp_path, "company_name,canonical_domain\n"
                "Allied Mechanical LLC,alliedmechanical.com\n")
    rows, _ = read_holdout_csv(path)

    result = evaluate(session, rows)
    assert result.recovered_by_domain == []
    assert len(result.not_recovered) == 1
    assert result.recall == 0.0
    # Reported as a diagnostic, and as a conflict for a human to look at.
    assert result.name_only_matches, "the similarity is worth surfacing"
    assert result.wrong_domain_conflicts
    holdout_name, expected, found = result.wrong_domain_conflicts[0]
    assert expected == "alliedmechanical.com"
    assert found == "alliedmechanical-ohio.com"


def test_a_missing_account_is_named_so_it_can_be_investigated(session, tmp_path):
    _discovered(session, "Other Mechanical", "othermech.com")
    path = _csv(tmp_path, "company_name,canonical_domain\n"
                "Never Found HVAC,neverfound.com\n")
    rows, _ = read_holdout_csv(path)

    result = evaluate(session, rows)
    assert result.not_recovered == [("Never Found HVAC", "neverfound.com")]
    assert result.as_dict()["missing_accounts"] == [
        {"company_name": "Never Found HVAC", "canonical_domain": "neverfound.com"}
    ]


def test_a_holdout_row_without_a_domain_is_not_scored_either_way(session, tmp_path):
    """Blaming discovery for a gap in the evaluation data would be dishonest."""
    _discovered(session, "Big Mechanical Inc", "bigmechanical.com")
    path = _csv(tmp_path, "company_name,canonical_domain\n"
                "Big Mechanical Inc,bigmechanical.com\n"
                "No Domain Known Co,\n")
    rows, _ = read_holdout_csv(path)

    result = evaluate(session, rows)
    assert result.holdout_total == 2
    assert result.holdout_without_domain == 1
    assert len(result.recovered_by_domain) == 1
    assert result.not_recovered == []
    # Recall is over the whole holdout, and the unscored row is stated, not hidden.
    assert result.recall == 0.5


def test_a_group_domain_is_not_a_recovery(session, tmp_path):
    """Only an identity domain counts. A shared host identifies no company."""
    _discovered(session, "Tiny HVAC", "wixsite.com", role="GROUP")
    path = _csv(tmp_path, "company_name,canonical_domain\n"
                "Tiny HVAC,wixsite.com\n")
    rows, _ = read_holdout_csv(path)
    result = evaluate(session, rows)
    assert result.recovered_by_domain == []


def test_companies_discovered_outside_the_holdout_are_counted(session, tmp_path):
    """The engine should find accounts BoRo did not already know."""
    _discovered(session, "Known Co", "known.com")
    _discovered(session, "New Find One", "newfind1.com")
    _discovered(session, "New Find Two", "newfind2.com")
    path = _csv(tmp_path, "company_name,canonical_domain\nKnown Co,known.com\n")
    rows, _ = read_holdout_csv(path)

    result = evaluate(session, rows)
    assert len(result.recovered_by_domain) == 1
    assert result.new_outside_holdout == 2
    assert result.discovered_with_domain == 3


def test_the_database_forbids_two_companies_holding_one_identity_domain(
    session, tmp_path
):
    """The duplicate the evaluator reports on cannot actually be reached.

    `uq_identity_domain` is a partial unique index, so at most one company can
    hold a domain as `IDENTITY` — which is why a discovery run that finds one
    contractor twice demotes the second's domain to `GROUP` rather than creating
    an ambiguous identity (M2-ADR-047).

    The evaluator's `duplicate_recovery` field stays as defence-in-depth: it
    cannot fire while this index exists, and if the index ever went away it is
    the number that would say so.
    """
    from sqlalchemy.exc import IntegrityError

    _discovered(session, "Dup One", "dup.com")
    second = _discovered(session, "Dup Two", None)

    with pytest.raises(IntegrityError):
        with session.begin_nested():
            session.add(CompanyDomain(
                company_id=second, domain_normalized="dup.com",
                domain_role="IDENTITY", derived_from_claim_ids=[]))
            session.flush()

    rows, _ = read_holdout_csv(_csv(tmp_path,
        "company_name,canonical_domain\nDup One,dup.com\n"))
    result = evaluate(session, rows)
    assert len(result.recovered_by_domain) == 1
    assert result.duplicate_recovery == [], (
        "unreachable while the index holds, which is the point"
    )


def test_the_evaluator_writes_nothing(session, tmp_path):
    """It is evaluation, not ingestion: the holdout never enters M2 (§15)."""
    before = session.scalar(select(Company.id).limit(1))
    rows, _ = read_holdout_csv(_csv(tmp_path,
        "company_name,canonical_domain\nNot In M2 Co,notinm2.com\n"))
    evaluate(session, rows)
    session.flush()

    names = set(session.scalars(select(CompanyName.name_raw)).all())
    assert "Not In M2 Co" not in names
    assert session.scalar(select(Company.id).limit(1)) == before


def test_a_missing_company_name_column_is_refused(tmp_path):
    path = _csv(tmp_path, "name,domain\nA,a.com\n")
    with pytest.raises(ValueError, match="company_name"):
        read_holdout_csv(path)


def test_recall_over_an_empty_holdout_is_zero_not_an_error(session):
    result = evaluate(session, [])
    assert result.holdout_total == 0
    assert result.recall == 0.0
