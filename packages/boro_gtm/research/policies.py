"""Every versioned deterministic contract M3 runs under, in one module.

These are *policies*, not calibration. Each carries a version, each version is
folded into a hash that is stored beside the thing it produced, and none may be
reinterpreted retroactively: a claim written under trust policy 1 keeps the
trust inputs of policy 1 forever, because a recalibration that silently
re-explains an existing number is indistinguishable from a bug.

Nothing here is tuned against real data, and nothing here should be presented
as though it were.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from boro_gtm.research.enums import (
    CanonicalizationStrategy,
    SourceClass,
)

# --- versions ---------------------------------------------------------------

LOCATOR_POLICY_VERSION = "1.0"
CLASSIFIER_POLICY_VERSION = "1"
TEXT_EXTRACTION_POLICY_VERSION = "1"
REDACTION_POLICY_VERSION = "1"
PUBLISHER_POLICY_VERSION = "1"
TRUST_POLICY_VERSION = "1"
CONFIDENCE_FORMULA_VERSION = "1"
FACT_TYPE_MAPPING_VERSION = "1"
ASSERTION_POLICY_VERSION = "1"
SIGNAL_POLICY_VERSION = "1"


def canonical_json(value: object) -> str:
    """One spelling per value, so a hash of it means something."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sha256_json(value: object) -> str:
    return sha256_text(canonical_json(value))


# --- locator normalization --------------------------------------------------

#: Parameters that identify a campaign, not a document. Stripping them is what
#: stops one page becoming five sources.
TRACKING_PARAMS = frozenset({
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "utm_id", "gclid", "fbclid", "msclkid", "mc_cid", "mc_eid", "ref", "ref_src",
})

_DEFAULT_PORTS = {"http": "80", "https": "443"}


def normalize_locator(url: str) -> str:
    """Collapse the spellings of one address into a single identity.

    Case, default ports, tracking parameters, parameter order, a trailing
    slash and the fragment are all presentation. What remains is the document.
    """
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower() or "https"
    host = (parts.hostname or "").lower()
    port = parts.port
    netloc = host
    if port is not None and str(port) != _DEFAULT_PORTS.get(scheme):
        netloc = f"{host}:{port}"

    path = parts.path or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")

    kept = sorted(
        (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k.lower() not in TRACKING_PARAMS
    )
    return urlunsplit((scheme, netloc, path, urlencode(kept), ""))


def host_of(normalized: str) -> str:
    return urlsplit(normalized).hostname or ""


def registrable_domain(host: str) -> str:
    """Last two labels. Adequate for fixtures; a public-suffix list is not.

    Named honestly rather than pretending: `co.uk` would be wrong here, and no
    fixture uses one.
    """
    labels = host.split(".")
    return ".".join(labels[-2:]) if len(labels) >= 2 else host


# --- body classification ----------------------------------------------------

def sniff_media_type(raw: bytes) -> tuple[str, str | None]:
    """Media type from the bytes themselves, never from what a server claimed.

    A declared content type belongs to one retrieval; the bytes are global, and
    body identity may not carry anything contextual.
    """
    head = raw[:512].lstrip()
    if raw[:5] == b"%PDF-":
        return "application/pdf", None
    lowered = head.lower()
    if lowered.startswith(b"<!doctype html") or lowered.startswith(b"<html"):
        return "text/html", "utf-8"
    if head[:1] in (b"{", b"["):
        try:
            json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            pass
        else:
            return "application/json", "utf-8"
    return "text/plain", "utf-8"


STRATEGY_FOR_MEDIA_TYPE = {
    "text/html": CanonicalizationStrategy.HTML_TEXT_V1.value,
    "application/pdf": CanonicalizationStrategy.PDF_TEXT_V1.value,
    "application/json": CanonicalizationStrategy.JSON_CANONICAL_V1.value,
    "text/plain": CanonicalizationStrategy.PLAINTEXT_V1.value,
}


# --- canonicalization -------------------------------------------------------

_SCRIPT_OR_STYLE = re.compile(rb"<(script|style)\b.*?</\1>", re.I | re.S)
_COMMENT = re.compile(rb"<!--.*?-->", re.S)
_TAG = re.compile(rb"<[^>]+>")
_WS = re.compile(r"\s+")

_META_PUBLISHED = re.compile(
    rb"""<meta[^>]+property=["']article:published_time["'][^>]+content=["']([^"']+)["']""",
    re.I,
)
_META_LANG = re.compile(rb"""<html[^>]*\blang=["']([^"']+)["']""", re.I)
_TITLE = re.compile(rb"<title[^>]*>(.*?)</title>", re.I | re.S)


@dataclass(frozen=True, slots=True)
class Canonicalized:
    """A body under one canonicalization contract."""

    text: str
    content_hash: str
    title: str | None = None
    language: str | None = None
    published_at: str | None = None
    page_offsets: dict[str, int] | None = None


def _html_text(raw: bytes) -> str:
    stripped = _TAG.sub(b" ", _COMMENT.sub(b" ", _SCRIPT_OR_STYLE.sub(b" ", raw)))
    text = stripped.decode("utf-8", errors="replace")
    for entity, char in (("&amp;", "&"), ("&nbsp;", " "), ("&#39;", "'"), ("&quot;", '"')):
        text = text.replace(entity, char)
    return _WS.sub(" ", text).strip()


def canonicalize(raw: bytes, strategy: str) -> Canonicalized:
    """Deterministic per (bytes, strategy). Never reads the network or a clock."""
    if strategy == CanonicalizationStrategy.HTML_TEXT_V1.value:
        text = _html_text(raw)
        title_m = _TITLE.search(raw)
        lang_m = _META_LANG.search(raw)
        pub_m = _META_PUBLISHED.search(raw)
        return Canonicalized(
            text=text,
            content_hash=sha256_text(text),
            title=_WS.sub(" ", title_m.group(1).decode("utf-8", "replace")).strip()
            if title_m else None,
            language=lang_m.group(1).decode() if lang_m else None,
            published_at=pub_m.group(1).decode() if pub_m else None,
        )

    if strategy == CanonicalizationStrategy.PDF_TEXT_V1.value:
        return _canonicalize_pdf(raw)

    if strategy == CanonicalizationStrategy.JSON_CANONICAL_V1.value:
        parsed = json.loads(raw.decode("utf-8"))
        text = canonical_json(parsed)
        return Canonicalized(text=text, content_hash=sha256_text(text))

    text = _WS.sub(" ", raw.decode("utf-8", errors="replace")).strip()
    return Canonicalized(text=text, content_hash=sha256_text(text))


#: The fixture PDF format: a tiny, honest, page-delimited container. It is a
#: stand-in for a real PDF text layer, not a PDF parser, and it is named so
#: nobody mistakes it for one. No OCR, here or anywhere in M3.
PDF_PAGE_BREAK = "\f"


def _canonicalize_pdf(raw: bytes) -> Canonicalized:
    body = raw.decode("utf-8", errors="replace")
    if body.startswith("%PDF-"):
        body = body.split("\n", 1)[1] if "\n" in body else ""
    pages = body.split(PDF_PAGE_BREAK)
    offsets: dict[str, int] = {}
    cursor = 0
    cleaned: list[str] = []
    for number, page in enumerate(pages, start=1):
        text = _WS.sub(" ", page).strip()
        offsets[str(number)] = cursor
        cleaned.append(text)
        cursor += len(text) + 1
    joined = "\n".join(cleaned)
    return Canonicalized(
        text=joined, content_hash=sha256_text(joined), page_offsets=offsets
    )


# --- text derivation --------------------------------------------------------

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
_PHONE = re.compile(r"\b(?:\+?1[ .-]?)?\(?\d{3}\)?[ .-]?\d{3}[ .-]?\d{4}\b")


def redact(text: str, redaction_policy_version: str) -> str:
    """Policy 1 removes e-mail addresses; policy 2 also removes phone numbers.

    Redaction is versioned because a false negative is a privacy incident and a
    false positive destroys evidence, so the day the rule changes, the old text
    must remain readable exactly as it was when a claim cited it.
    """
    out = _EMAIL.sub("[REDACTED_EMAIL]", text)
    if redaction_policy_version >= "2":
        out = _PHONE.sub("[REDACTED_PHONE]", out)
    return out


def text_derivation_contract_hash(
    text_extraction_policy_version: str, redaction_policy_version: str
) -> str:
    return sha256_json({
        "text_extraction_policy_version": text_extraction_policy_version,
        "redaction_policy_version": redaction_policy_version,
    })


# --- publisher policy -------------------------------------------------------

@dataclass(frozen=True, slots=True)
class Publisher:
    key: str
    source_class: str


#: Registrable domain -> who published it. Independence is computed from this,
#: so a mirror on the same publisher must never read as a second voice.
PUBLISHER_BY_DOMAIN: dict[str, Publisher] = {
    "meridianmechanical.com": Publisher("meridianmechanical", SourceClass.COMPANY_OWN_SITE.value),
    "meridian-mechanical.net": Publisher(
        # The marketing mirror is the *same* publisher on a second domain.
        "meridianmechanical", SourceClass.COMPANY_OWN_SITE.value
    ),
    "hvacjobsboard.example": Publisher("hvacjobsboard", SourceClass.JOB_BOARD.value),
    "sos.ohio.example": Publisher("ohio-sos", SourceClass.GOVERNMENT_REGISTRY.value),
    "contractordirectory.example": Publisher(
        "contractordirectory", SourceClass.THIRD_PARTY_DIRECTORY.value
    ),
}

UNKNOWN_PUBLISHER = Publisher("unknown", SourceClass.UNKNOWN.value)


def publisher_for(host: str) -> Publisher:
    return PUBLISHER_BY_DOMAIN.get(registrable_domain(host), UNKNOWN_PUBLISHER)


# --- trust policy -----------------------------------------------------------

#: Reproducibility inputs, not commercial judgement. A registry filing is more
#: reliable about a legal name than a directory listing is; that is all this
#: table says.
TRUST_TIER_BY_SOURCE_CLASS: dict[str, float] = {
    SourceClass.GOVERNMENT_REGISTRY.value: 1.00,
    SourceClass.COMPANY_OWN_SITE.value: 0.90,
    SourceClass.JOB_BOARD.value: 0.75,
    SourceClass.NEWS_MEDIA.value: 0.70,
    SourceClass.VENDOR_SITE.value: 0.60,
    SourceClass.THIRD_PARTY_DIRECTORY.value: 0.55,
    SourceClass.UNKNOWN.value: 0.40,
}


def trust_tier(source_class: str) -> float:
    return TRUST_TIER_BY_SOURCE_CLASS[source_class]


# --- confidence -------------------------------------------------------------

BASE_BY_FACT_TYPE: dict[str, float] = {
    "FACT": 0.95,
    "ESTIMATE": 0.80,
    "PROXY": 0.65,
    "INFERENCE": 0.50,
    "HYPOTHESIS": 0.35,
}

#: Saturating and monotone non-decreasing, 1.0 at one publisher.
CORROBORATION_FACTOR: tuple[float, ...] = (1.0, 1.0, 1.10, 1.16, 1.20)

INFERENCE_PENALTY = 0.85


def corroboration_factor(independent_publishers: int) -> float:
    if independent_publishers <= 0:
        return 1.0
    index = min(independent_publishers, len(CORROBORATION_FACTOR) - 1)
    return CORROBORATION_FACTOR[index]


def compute_confidence(
    fact_type: str,
    trust_tiers: list[float],
    independent_publishers: int,
    inferred: bool = False,
) -> float:
    """``base × trust × corroboration × penalty``, clamped to [0, 1].

    ``trust`` is the **maximum** tier across links, never the mean: adding a
    weak corroborating source must not lower a claim's confidence, or the
    system would punish looking for more evidence.
    """
    base = BASE_BY_FACT_TYPE[fact_type]
    trust = max(trust_tiers) if trust_tiers else 0.0
    value = (
        base
        * trust
        * corroboration_factor(independent_publishers)
        * (INFERENCE_PENALTY if inferred else 1.0)
    )
    return round(max(0.0, min(1.0, value)), 4)


def assertion_contract_hash(inference_rule_version: str | None = None) -> str:
    """Every policy version that explains a stored confidence, in one hash."""
    return sha256_json({
        "assertion_policy_version": ASSERTION_POLICY_VERSION,
        "trust_policy_version": TRUST_POLICY_VERSION,
        "confidence_formula_version": CONFIDENCE_FORMULA_VERSION,
        "fact_type_mapping_version": FACT_TYPE_MAPPING_VERSION,
        "publisher_policy_version": PUBLISHER_POLICY_VERSION,
        "inference_rule_version": inference_rule_version,
    })


def assertion_fingerprint(
    *,
    subject_company_id: object,
    attribute_key: str,
    attribute_registry_version: str,
    value: object,
    unit: str | None,
    fact_type: str | None,
    availability: str,
    period_granularity: str,
    observed_at: object,
    lineage_artifact_ids: list[str],
    contract_hash: str,
) -> str:
    """Identity of an assertion, deliberately **excluding the extractor**.

    A newer extractor agreeing with an older one over the same lineage appends
    an evidence link rather than a twin claim. Without that, re-extracting one
    page three times would read as threefold corroboration.
    """
    return sha256_json({
        "subject_company_id": str(subject_company_id),
        "attribute_key": attribute_key,
        "attribute_registry_version": attribute_registry_version,
        "value": value,
        "unit": unit,
        "fact_type": fact_type,
        "availability": availability,
        "period_granularity": period_granularity,
        "observed_at": observed_at,
        "lineage_key": sorted(lineage_artifact_ids),
        "assertion_contract_hash": contract_hash,
    })
