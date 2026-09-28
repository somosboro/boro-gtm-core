"""The traceability document's own numbers, checked against its own tables.

Every count this project stated by hand has eventually drifted: the attribute
count, the scenario count, the ADR count and the signal coverage summary all
did. This parses the documents instead.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

DOCS = Path(__file__).resolve().parents[2] / "docs"
ACCEPTANCE = DOCS / "M3_ACCEPTANCE_CRITERIA.md"
TRACEABILITY = DOCS / "M3_IMPLEMENTATION_TRACEABILITY.md"
ADRS = DOCS / "M3_ADRS.md"
DESIGN = DOCS / "M3_OPERATIONAL_RESEARCH_DESIGN.md"

SCENARIO = re.compile(r"^### ([A-Z]+\d+[a-c]?) \[MUST\]", re.M)
ADR = re.compile(r"^## M3-ADR-(\d+)", re.M)


@pytest.fixture(scope="module")
def contract_ids() -> set[str]:
    return set(SCENARIO.findall(ACCEPTANCE.read_text()))


@pytest.fixture(scope="module")
def cited_ids(contract_ids) -> set[str]:
    """Scenario ids in the first cell of any traceability table row."""
    found = set()
    for line in TRACEABILITY.read_text().splitlines():
        if not line.startswith("|"):
            continue
        first = line.strip().strip("|").split("|")[0].strip()
        for token in re.split(r"[,/ ]+", first):
            token = token.strip()
            if re.fullmatch(r"[A-Z]+\d+[a-c]?", token) and token in contract_ids:
                found.add(token)
    return found


def _stated(label: str) -> int:
    match = re.search(rf"\| {re.escape(label)} \| (\d+) \|", TRACEABILITY.read_text())
    assert match, f"{label!r} is not stated in the traceability document"
    return int(match.group(1))


def test_the_contract_count_matches_the_acceptance_document(contract_ids):
    """Live MUST scenarios only. A withdrawn scenario is not coverage to chase."""
    text = ACCEPTANCE.read_text()
    assert len(contract_ids) == _stated("Scenarios in the contract")

    withdrawn = re.findall(r"^### ([A-Z]+\d+) \[WITHDRAWN", text, re.M)
    stated = re.search(
        r"\*\*Scenario count: (\d+) MUST \+ (\d+) withdrawn = (\d+)\*\*", text
    )
    assert stated, "the acceptance document must state its own counts"
    live, retired, total = (int(g) for g in stated.groups())
    assert live == len(contract_ids)
    assert retired == len(withdrawn)
    assert live + retired == total == 135


def test_a_withdrawn_scenario_is_never_claimed_as_covered(contract_ids):
    withdrawn = set(re.findall(
        r"^### ([A-Z]+\d+) \[WITHDRAWN", ACCEPTANCE.read_text(), re.M))
    trace = TRACEABILITY.read_text()
    for scenario in withdrawn:
        for line in trace.splitlines():
            if line.startswith("|"):
                first = line.strip().strip("|").split("|")[0].strip()
                assert first != scenario, f"{scenario} is withdrawn but still cited"


def test_the_executable_count_matches_the_scenarios_actually_cited(cited_ids):
    assert len(cited_ids) == _stated("Executable, and passing")


def test_the_not_executable_count_is_the_remainder(contract_ids, cited_ids):
    assert len(contract_ids) - len(cited_ids) == _stated("Not yet executable")


def test_nothing_is_recorded_as_failing():
    assert _stated("Failing") == 0


def test_no_traceability_row_cites_a_scenario_that_does_not_exist(contract_ids):
    """A citation to a scenario nobody wrote is drift wearing a reference."""
    bogus = set()
    for line in TRACEABILITY.read_text().splitlines():
        if not line.startswith("|"):
            continue
        first = line.strip().strip("|").split("|")[0].strip()
        for token in re.split(r"[,/ ]+", first):
            token = token.strip()
            # Section letters used by the acceptance contract, plus a digit.
            if re.fullmatch(r"[ABCDEFGHIJLMNO]\d+[a-c]?", token):
                if token not in contract_ids:
                    bogus.add(token)
    assert bogus == set(), f"traceability cites scenarios that do not exist: {bogus}"


def test_the_traceability_table_uses_no_ranges():
    """A range hides what is covered; the table lists ids individually."""
    ranges = set()
    for line in TRACEABILITY.read_text().splitlines():
        if not line.startswith("|"):
            continue
        first = line.strip().strip("|").split("|")[0].strip()
        ranges |= set(re.findall(r"[A-Z]+\d+\s*[–-]\s*[A-Z]*\d+", first))
    assert ranges == set(), f"ranges in the coverage table: {ranges}"


def test_the_adr_count_the_design_states_matches_the_adr_document():
    numbers = [int(n) for n in ADR.findall(ADRS.read_text())]
    assert numbers == sorted(numbers), "ADRs are out of order"
    assert len(numbers) == len(set(numbers)), "duplicate ADR number"
    assert f"{len(numbers)} ADRs" in DESIGN.read_text()


def test_every_test_the_traceability_cites_actually_exists():
    """A citation to a test nobody wrote is the same drift as a wrong count.

    Collected from the suite rather than grepped, so a renamed or deleted test
    fails here instead of leaving a row that looks like coverage.
    """
    import subprocess
    import sys

    cited = set(re.findall(r"`(test_[a-z0-9_]+)`", TRACEABILITY.read_text()))
    assert len(cited) > 100, "the table should cite well over a hundred tests"

    root = TRACEABILITY.resolve().parents[1]
    collected = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/", "--collect-only", "-q"],
        capture_output=True, text=True, cwd=root, check=False,
    ).stdout
    existing = set(re.findall(r"::(test_[A-Za-z0-9_]+)", collected))
    assert existing, "collection produced nothing; the guard would pass vacuously"

    missing = sorted(cited - existing)
    assert missing == [], f"traceability cites tests that do not exist: {missing}"
