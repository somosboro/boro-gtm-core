"""Live design text must not contradict the frozen decisions.

Documents drift in a specific way: a superseded model survives in one section
while its replacement is written in another, and the stale copy reads as
authoritative because nothing marks it. That is how the artifact-only lineage
key came back (M3-ADR-049), and how "consumed by M2's existing human-review
path" outlived the revision-4 finding that verified it false.

These tests are deliberately narrow. Historical discussion is the useful part
of an ADR and must stay readable, so an obsolete phrase is allowed wherever it
is labelled — and only there.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

DOCS = Path(__file__).resolve().parents[2] / "docs"
DESIGN = DOCS / "M3_OPERATIONAL_RESEARCH_DESIGN.md"
SCHEMA_GRAPH = DOCS / "M3_SCHEMA_GRAPH.md"
ADRS = DOCS / "M3_ADRS.md"

#: Words that mark a sentence as describing a past model rather than the
#: current one. A paragraph carrying any of these may say anything.
HISTORY_MARKERS = (
    "earlier revision", "superseded", "supersedes", "amended", "amendment",
    "withdrawn", "no longer exists", "removed in", "corrected",
    "verified false", "docs claimed", "stale", "was wrong", "historical",
)

#: "revision 2", "revisions 1-3" — the en-dash form is why this is a regex.
HISTORY_PATTERN = re.compile(r"revisions?\s+\d", re.I)


def _paragraphs(path: Path) -> list[str]:
    return re.split(r"\n\s*\n", path.read_text())


def _is_historical(text: str) -> bool:
    lowered = text.lower()
    return (
        any(marker in lowered for marker in HISTORY_MARKERS)
        or HISTORY_PATTERN.search(lowered) is not None
    )


def _adr_blocks(text: str) -> list[str]:
    """ADRs are labelled at the block level, by their status line.

    Paragraph granularity is wrong here: an amended ADR keeps its original
    Decision paragraph verbatim on purpose, and that paragraph carries no
    marker of its own — the status above it does.
    """
    return [
        block for block in re.split(r"^(?=## M3-ADR-)", text, flags=re.M)
        if block.startswith("## M3-ADR-")
    ]


def _unlabelled(path: Path, pattern: str) -> list[str]:
    """Sections matching `pattern` that nothing marks as historical.

    ADRs are scoped by decision record; everything else by paragraph.
    """
    text = path.read_text()
    sections = _adr_blocks(text) if path is ADRS else re.split(r"\n\s*\n", text)
    return [
        section.strip().splitlines()[0]
        for section in sections
        if re.search(pattern, section, re.I) and not _is_historical(section)
    ]


# --- the identity-review queue ---------------------------------------------

M2_CONSUMPTION = r"M2-consumed|consumed by M2|M2's existing human-review path"


@pytest.mark.parametrize("path", [DESIGN, SCHEMA_GRAPH, ADRS], ids=lambda p: p.name)
def test_no_live_text_claims_the_identity_queue_is_wired_into_m2(path):
    """M2's review needs a ProviderEntity; a company-level signal has none.

    Saying otherwise is not a wording problem. It implies an integration whose
    only possible implementation is fabricating a `ProviderEntity` — an
    identity write by the component forbidden from writing identity.
    """
    offenders = _unlabelled(path, M2_CONSUMPTION)
    assert offenders == [], (
        f"{path.name} presents the M2 integration as current:\n\n"
        + "\n\n".join(offenders)
    )


def test_the_design_states_the_queue_is_m3_owned():
    """The positive claim, so deleting the wrong sentence is not enough."""
    text = DESIGN.read_text()
    assert "M3-written and M3-owned end to\nend" in text
    assert "This queue is not wired into M2" in text.replace("**", "")
    assert "fabricating" in text.lower()


def test_adr_009_is_marked_amended():
    """The original reasoning stays; its status says it was overtaken."""
    block = re.search(
        r"^## M3-ADR-009 .*?(?=^## M3-ADR-010)", ADRS.read_text(), re.M | re.S
    )
    assert block, "M3-ADR-009 is missing"
    body = block.group(0)
    assert "amended in revision 4" in body.lower()
    assert "### Amendment" in body
    assert "provider_entity_id" in body
    assert "never create a `ProviderEntity`" in body


# --- other superseded vocabulary -------------------------------------------

@pytest.mark.parametrize("term", [
    r"research_artifact_versions",
    r"corroborating_lineage_count",
    r"target_set_hash",
])
@pytest.mark.parametrize("path", [DESIGN, SCHEMA_GRAPH], ids=lambda p: p.name)
def test_superseded_object_names_appear_only_in_labelled_context(path, term):
    offenders = _unlabelled(path, term)
    assert offenders == [], (
        f"{path.name} uses the superseded name {term} as current:\n\n"
        + "\n\n".join(offenders)
    )


def test_every_adr_using_a_superseded_name_says_so_in_its_status():
    """An ADR may describe an old model; its status must admit it."""
    text = ADRS.read_text()
    blocks = re.split(r"^(?=## M3-ADR-)", text, flags=re.M)
    stale = {"research_artifact_versions", "corroborating_lineage_count",
             "target_set_hash"}
    offenders = []
    for block in blocks:
        if not block.startswith("## M3-ADR-"):
            continue
        if not any(name in block for name in stale):
            continue
        status = re.search(r"^\*\*Status:\*\*(.*?)(?=^###)", block, re.M | re.S)
        if status is None or not _is_historical(status.group(1)):
            offenders.append(block.splitlines()[0])
    assert offenders == [], (
        "these ADRs use a superseded object name without saying so in their "
        f"status: {offenders}"
    )


# --- the lineage key, stated in two documents ------------------------------

def test_both_documents_define_the_lineage_key_the_same_way():
    """The contradiction that let the artifact-only lineage regress."""
    for path in (DESIGN, SCHEMA_GRAPH):
        offenders = _unlabelled(path, r"lineage_key\s*=\s*sorted distinct artifact")
        assert offenders == [], f"{path.name} still defines lineage by artifact alone"
    assert "sorted [(source_id, artifact_id)" in SCHEMA_GRAPH.read_text()
    assert "sorted [(source_id, artifact_id)" in DESIGN.read_text()


# --- publication metadata ownership ----------------------------------------

#: Every spelling that puts the publication date on the globally deduplicated
#: artifact. It belongs to the derivation that observed it; the other placement
#: would fold it into the canonical hash and split mirrors into two documents.
_ARTIFACT_OWNED_PUBLICATION = (
    r"research_artifacts\.source_published_at",
    r"source_published_at`?\s*\|\s*artifact",
    r"artifact is where\s*\n?`?source_published_at",
)


@pytest.mark.parametrize("pattern", _ARTIFACT_OWNED_PUBLICATION)
@pytest.mark.parametrize("path", [DESIGN, SCHEMA_GRAPH], ids=lambda p: p.name)
def test_publication_metadata_is_never_placed_on_the_artifact(path, pattern):
    offenders = _unlabelled(path, pattern)
    assert offenders == [], (
        f"{path.name} places publication metadata on the artifact:\n\n"
        + "\n\n".join(offenders)
    )


def test_both_documents_place_publication_metadata_on_the_derivation():
    """The positive claim, so deleting the wrong sentence is not enough."""
    assert "research_artifact_derivations.source_published_at" in DESIGN.read_text()
    graph = SCHEMA_GRAPH.read_text()
    assert "`source_published_at`, `source_published_granularity`" in graph
