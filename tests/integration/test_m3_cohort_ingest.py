"""Loading BoRo's own target list, under M2's identity rules.

The rule this file exists to enforce: a company name is never turned into a
website. A row with no verified domain is loaded and flagged, never researched —
because wrong-account evidence arrives with a complete, internally consistent
provenance chain and is indistinguishable from correct evidence afterwards.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from boro_gtm.discovery.domain.models import CompanyDomain, CompanyName
from boro_gtm.research.live.cohort import (
    COHORT_COLUMNS,
    load_cohort,
    read_cohort_csv,
)
from boro_gtm.research.seeds import seed_all

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)


def _csv(tmp_path, body: str):
    path = tmp_path / "cohort.csv"
    path.write_text(body, encoding="utf-8")
    return path


@pytest.fixture
def m3(session):
    seed_all(session)
    session.flush()
    return session


def test_the_documented_header_is_what_the_reader_accepts(tmp_path, m3):
    path = _csv(tmp_path, ",".join(COHORT_COLUMNS) + "\n"
                "Big Mechanical Inc,bigmechanical.com,,CRM-100\n")
    rows, problems = read_cohort_csv(path)
    assert problems == []
    assert rows[0].company_name == "Big Mechanical Inc"
    assert rows[0].canonical_domain == "bigmechanical.com"
    assert rows[0].source_id == "CRM-100"


def test_a_row_with_a_domain_becomes_a_canonical_company(tmp_path, m3):
    path = _csv(tmp_path, "company_name,canonical_domain,website_url,source_id\n"
                "Big Mechanical Inc,bigmechanical.com,,CRM-100\n")
    rows, _ = read_cohort_csv(path)
    report = load_cohort(m3, rows, now=NOW)
    m3.flush()

    assert len(report.loaded) == 1
    name, company_id, domain = report.loaded[0]
    assert domain == "bigmechanical.com"
    assert m3.scalar(select(CompanyDomain.domain_role).where(
        CompanyDomain.company_id == company_id)) == "IDENTITY"
    assert m3.scalar(select(CompanyName.name_raw).where(
        CompanyName.company_id == company_id)) == "Big Mechanical Inc"


def test_a_row_with_only_a_website_url_uses_its_host(tmp_path, m3):
    path = _csv(tmp_path, "company_name,canonical_domain,website_url,source_id\n"
                "Allied Mechanical,,https://www.alliedmechanical.com/about,\n")
    rows, _ = read_cohort_csv(path)
    report = load_cohort(m3, rows, now=NOW)
    assert report.loaded[0][2] == "alliedmechanical.com", (
        "the operator asserted the URL, so its host is an asserted domain"
    )


def test_a_row_with_no_domain_is_flagged_and_never_researched(tmp_path, m3):
    """The rule. A name is not an address.

    Two contractors called "Allied Mechanical" are two companies, and choosing
    whichever ranks better attaches one's evidence to the other's account.
    """
    path = _csv(tmp_path, "company_name,canonical_domain,website_url,source_id\n"
                "Allied Mechanical,,,CRM-7\n")
    rows, _ = read_cohort_csv(path)
    report = load_cohort(m3, rows, now=NOW)
    m3.flush()

    assert report.loaded == []
    assert report.needs_identity_review == [
        ("Allied Mechanical", "no canonical_domain or website_url supplied")
    ]
    # Nothing was invented: no company, and therefore nothing researchable.
    assert m3.scalars(select(CompanyName.name_raw).where(
        CompanyName.name_raw == "Allied Mechanical")).all() == []


def test_a_shared_platform_host_is_flagged_not_loaded_as_identity(tmp_path, m3):
    """M2 records these as GROUP precisely because they identify no company."""
    path = _csv(tmp_path, "company_name,canonical_domain,website_url,source_id\n"
                "Tiny HVAC,tinyhvac.wixsite.com,,\n")
    rows, _ = read_cohort_csv(path)
    report = load_cohort(m3, rows, now=NOW)
    assert report.loaded == []
    assert "shared host" in report.needs_identity_review[0][1]


def test_loading_the_same_cohort_twice_creates_one_company(tmp_path, m3):
    path = _csv(tmp_path, "company_name,canonical_domain,website_url,source_id\n"
                "Big Mechanical Inc,bigmechanical.com,,\n")
    rows, _ = read_cohort_csv(path)
    first = load_cohort(m3, rows, now=NOW)
    m3.flush()
    second = load_cohort(m3, rows, now=NOW)
    m3.flush()

    assert len(first.loaded) == 1
    assert second.loaded == []
    assert second.already_present[0][1] == first.loaded[0][1]


def test_a_www_prefix_and_casing_resolve_to_one_company(tmp_path, m3):
    path = _csv(tmp_path, "company_name,canonical_domain,website_url,source_id\n"
                "Big Mechanical Inc,bigmechanical.com,,\n"
                "Big Mechanical,WWW.BigMechanical.com,,\n")
    rows, _ = read_cohort_csv(path)
    report = load_cohort(m3, rows, now=NOW)
    m3.flush()
    assert len(report.loaded) == 1
    assert len(report.already_present) == 1


def test_a_missing_company_name_column_is_refused_with_the_expected_header(tmp_path):
    path = _csv(tmp_path, "name,domain\nBig Mechanical,bigmechanical.com\n")
    with pytest.raises(ValueError, match="company_name"):
        read_cohort_csv(path)


def test_a_blank_name_is_a_parse_problem_not_a_silent_skip(tmp_path):
    path = _csv(tmp_path, "company_name,canonical_domain,website_url,source_id\n"
                ",bigmechanical.com,,\n"
                "Big Mechanical Inc,bigmechanical.com,,\n")
    rows, problems = read_cohort_csv(path)
    assert len(rows) == 1
    assert problems == [("line 2", "no company_name")]


def test_a_loaded_cohort_row_is_immediately_researchable(tmp_path, m3):
    """The handover: what `load-cohort` writes is what live research reads."""
    from boro_gtm.research.live.discovery import domain_scope

    path = _csv(tmp_path, "company_name,canonical_domain,website_url,source_id\n"
                "Big Mechanical Inc,bigmechanical.com,,\n")
    rows, _ = read_cohort_csv(path)
    report = load_cohort(m3, rows, now=NOW)
    m3.flush()

    scope = domain_scope(m3, report.loaded[0][1])
    assert scope.primary == "https://bigmechanical.com/"
    assert scope.registrable == {"bigmechanical.com": "IDENTITY"}
