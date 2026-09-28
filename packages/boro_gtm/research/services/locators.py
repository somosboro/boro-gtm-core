"""Locators: where a quote sits, and how to find it again later.

A locator has two jobs and they fail differently.

**Pointing precisely.** A character span into derived text is exact but says
nothing a human can read. A structural path — the element and the heading it sat
under — is what makes a citation checkable by someone looking at the page.

**Surviving.** Text is re-derived under new policies, pages are re-fetched and
reflowed, and offsets rot. So resolution is **quote-hash first**: the stored
`quote_sha256` identifies the text regardless of where it moved to. Offsets are
a fast path, not the contract.

When neither resolves, the locator is reported as **rotted**. It is never
deleted and the evidence is never deleted: "we cannot currently point at this"
and "this was never observed" are different statements, and a system that
conflates them quietly loses history.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from html.parser import HTMLParser

from boro_gtm.research.policies import sha256_text

#: Elements whose text is not document content.
_SKIP = frozenset({"script", "style", "head"})
_HEADINGS = ("h1", "h2", "h3", "h4", "h5", "h6")
_WS = re.compile(r"\s+")


@dataclass(frozen=True, slots=True)
class Structure:
    """Where a piece of text sat in the document."""

    css_path: str
    heading_path: tuple[str, ...]


class _StructureIndex(HTMLParser):
    """Collects each text run with the element stack and heading trail above it.

    Written as an index over the *raw* document rather than over the canonical
    text, so adding it cannot change `canonical_content_hash` — the artifact
    identity — for any document already captured.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._stack: list[str] = []
        self._counts: list[dict[str, int]] = [{}]
        self._headings: dict[str, str] = {}
        self._capturing_heading: str | None = None
        #: (normalized text, structure)
        self.runs: list[tuple[str, Structure]] = []

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in ("br", "img", "meta", "link", "input", "hr"):
            return
        counts = self._counts[-1]
        counts[tag] = counts.get(tag, 0) + 1
        self._stack.append(f"{tag}:nth-of-type({counts[tag]})")
        self._counts.append({})
        if tag in _HEADINGS:
            self._capturing_heading = tag
            self._headings[tag] = ""

    def handle_endtag(self, tag: str) -> None:
        if tag in ("br", "img", "meta", "link", "input", "hr"):
            return
        if self._capturing_heading == tag:
            self._capturing_heading = None
        for index in range(len(self._stack) - 1, -1, -1):
            if self._stack[index].split(":")[0] == tag:
                del self._stack[index:]
                del self._counts[index + 1:]
                break

    def handle_data(self, data: str) -> None:
        if any(part.split(":")[0] in _SKIP for part in self._stack):
            return
        text = _WS.sub(" ", data).strip()
        if not text:
            return
        if self._capturing_heading:
            self._headings[self._capturing_heading] += text
        trail = tuple(
            self._headings[level] for level in _HEADINGS
            if self._headings.get(level)
        )
        self.runs.append((text, Structure(
            css_path=" > ".join(part.split(":")[0] for part in self._stack)
            + "".join(
                f":nth-of-type({part.split('(')[1][:-1]})"
                for part in self._stack[-1:] if "(" in part
            ),
            heading_path=trail,
        )))


def html_structure(raw: bytes, quote: str) -> Structure | None:
    """The element and heading trail the quote sat under, or None if unfound."""
    index = _StructureIndex()
    try:
        index.feed(raw.decode("utf-8", errors="replace"))
    except Exception:            # pragma: no cover - a malformed fixture
        return None
    needle = _WS.sub(" ", quote).strip()
    for text, structure in index.runs:
        if needle and needle in text:
            return structure
    return None


#: A fixture PDF's sections are named headings: "SECTION 2. SERVICE DELIVERY".
#: The title is the run of capitals that follows — canonicalization collapses a
#: page to one line, so anchoring on the line end would swallow the whole page.
_PDF_SECTION = re.compile(r"SECTION\s+([\w.]+)\.?\s+([A-Z][A-Z ]{2,}?)(?=\s+\d|\s+[a-z]|$)")


def pdf_section(text: str, offset: int | None) -> str | None:
    """The last section heading at or before the offset."""
    if offset is None:
        return None
    found = None
    for match in _PDF_SECTION.finditer(text):
        if match.start() <= offset:
            found = f"{match.group(1)} {match.group(2).strip()}"
        else:
            break
    return found


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


class Resolution(StrEnum):
    """How a locator was resolved, which is part of the answer."""

    #: The stored offsets still point at the quoted text.
    EXACT = "EXACT"
    #: The offsets moved; the quote hash found it elsewhere.
    REHOMED = "REHOMED"
    #: Neither resolved. The evidence stands; the pointer does not.
    ROTTED = "ROTTED"


@dataclass(frozen=True, slots=True)
class ResolvedLocator:
    resolution: str
    start: int | None
    end: int | None
    quote: str | None
    #: A multiplier a reader may apply. Never zero: a rotted pointer weakens a
    #: citation, it does not retract the observation behind it.
    weight: float


#: What a rotted pointer costs. A number, stated, rather than an implicit
#: "ignore it" — and deliberately not zero.
ROTTED_WEIGHT = 0.5


def resolve(
    text: str | None, locator: dict, quote_sha256: str | None
) -> ResolvedLocator:
    """Quote-hash first. Offsets are the fast path, not the contract."""
    start, end = locator.get("start"), locator.get("end")

    if text is None:
        # The text derivation was pruned. The pointer is unresolvable *now*,
        # which is not the same as rotted, but weakens the citation identically.
        return ResolvedLocator(Resolution.ROTTED.value, None, None, None,
                               ROTTED_WEIGHT)

    if start is not None and end is not None and 0 <= start < end <= len(text):
        candidate = text[start:end]
        if quote_sha256 is None or sha256_text(candidate) == quote_sha256:
            return ResolvedLocator(Resolution.EXACT.value, start, end, candidate, 1.0)

    if quote_sha256 is not None:
        # Search by hash. Length is unknown, so try every span of the stored
        # length first, then fall back to a line-wise scan.
        span = (end - start) if (start is not None and end is not None) else None
        if span:
            for index in range(len(text) - span + 1):
                window = text[index:index + span]
                if sha256_text(window) == quote_sha256:
                    return ResolvedLocator(
                        Resolution.REHOMED.value, index, index + span, window, 1.0
                    )

    return ResolvedLocator(Resolution.ROTTED.value, None, None, None, ROTTED_WEIGHT)
