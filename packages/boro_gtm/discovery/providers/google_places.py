"""The Google Places API as an M2 discovery provider.

One production provider for BoRo's own market, built on the official Places API
(New) `places:searchText` endpoint. Not a scraper, not a browser, not a provider
marketplace (M2-ADR-040).

It does exactly what the `ProviderAdapter` contract asks and nothing more:
`search` is the only method that touches the network, and `parse`, `normalize`,
`canonicalize` and `derive_key` stay pure. Canonical companies are M2's to
write; this file never writes one.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from boro_gtm.core.errors import GtmError
from boro_gtm.discovery.enums import ProviderIdentityCapability
from boro_gtm.discovery.providers.base import (
    CandidateCompany,
    ProviderAdapter,
    ProviderCapabilities,
    RawRecord,
)

PROVIDER_KEY = "google_places"
ADAPTER_VERSION = "1.0.0"
NORMALIZER_VERSION = "1"
SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"

#: The environment variable the key is read from. Never persisted, never logged,
#: never committed (M2-ADR-043).
API_KEY_ENV = "GTM_GOOGLE_PLACES_API_KEY"

#: Exactly the fields M2 consumes, and nothing else. The Places API bills by
#: field mask, so every field requested is a cost, and a field M2 does not read
#: is a cost with no purpose.
FIELD_MASK = ",".join((
    "places.id",
    "places.displayName",
    "places.websiteUri",
    "places.formattedAddress",
    "places.addressComponents",
    "places.nationalPhoneNumber",
    "places.types",
    "places.primaryType",
    "places.businessStatus",
    "places.location",
    "nextPageToken",
))

#: Places returns at most 20 results per page and at most three pages per query.
MAX_PAGE_SIZE = 20
MAX_PAGES_PER_QUERY = 3


class MissingCredentialError(GtmError):
    """No API key. The run stops before any network call (M2-ADR-043)."""

    code = "PROVIDER_CREDENTIAL_MISSING"
    http_status = 503


class ProviderQuotaError(GtmError):
    """The provider refused for quota or authorization reasons.

    Recorded truthfully. There is no fallback to scraping and no attempt to work
    around a limit the provider set.
    """

    code = "PROVIDER_REFUSED"
    http_status = 502


@dataclass(frozen=True, slots=True)
class PlacesPage:
    """One provider page, kept whole so query provenance survives."""

    records: list[RawRecord]
    next_page_token: str | None


def api_key(explicit: str | None = None) -> str:
    key = explicit or os.environ.get(API_KEY_ENV, "").strip()
    if not key:
        raise MissingCredentialError(
            f"no Google Places credential: set {API_KEY_ENV}. Live discovery "
            "makes no network call without it, and there is no scraping "
            "fallback.",
            {"env_var": API_KEY_ENV},
        )
    return key


class GooglePlacesAdapter(ProviderAdapter):
    """`searchText` against the Places API, one page at a time.

    The HTTP client is injected so the whole adapter is testable against
    captured responses. CI never reaches Google.
    """

    def __init__(self, *, client=None, key: str | None = None,
                 region_code: str = "US") -> None:
        self._client = client
        self._key = key
        self._region_code = region_code

    # -- static declaration ------------------------------------------------

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider_key=PROVIDER_KEY,
            name="Google Places API",
            # Places issues a stable place id and documents it as durable, so
            # the adapter never derives a key of its own.
            identity_capability=ProviderIdentityCapability.NATIVE_EXTERNAL_ID.value,
            canonicalization_strategy="JSON_CANONICAL_V1",
            canonicalization_version="1",
            media_type="application/json",
            normalizer_version=NORMALIZER_VERSION,
            # A listing is a business the provider observed, not a registry
            # record. Mid trust: better than a scraped directory, well short of
            # a company's own statement or a registry filing.
            trust_tier=0.55,
            supported_filters=("text_query", "included_type", "location_bias",
                               "region_code"),
            is_fixture=False,
        )

    # -- the only method that may do I/O ----------------------------------

    def search(self, query: dict[str, Any]) -> Iterator[RawRecord]:
        """Walk the provider's pages for one query, yielding every place.

        Pagination is the provider's own `nextPageToken`. A caller that needs
        per-page provenance uses `search_page` and keeps the pages apart; this
        method exists because the `ProviderAdapter` contract requires it.
        """
        token: str | None = query.get("page_token")
        for _ in range(MAX_PAGES_PER_QUERY):
            page = self.search_page({**query, "page_token": token})
            yield from page.records
            token = page.next_page_token
            if not token:
                return

    def search_page(self, query: dict[str, Any]) -> PlacesPage:
        """Exactly one provider page. The unit of query provenance."""
        if self._client is None:
            raise MissingCredentialError(
                "no HTTP client configured for the Places adapter", {}
            )
        text_query = (query.get("text_query") or "").strip()
        if not text_query:
            raise GtmError("a Places search needs a text_query", {})

        payload: dict[str, Any] = {
            "textQuery": text_query,
            "pageSize": min(int(query.get("limit") or MAX_PAGE_SIZE), MAX_PAGE_SIZE),
            "regionCode": query.get("region_code") or self._region_code,
        }
        if query.get("page_token"):
            payload["pageToken"] = query["page_token"]
        if query.get("included_type"):
            payload["includedType"] = query["included_type"]
        if query.get("location_bias"):
            payload["locationBias"] = query["location_bias"]

        response = self._client.post(
            SEARCH_URL,
            json=payload,
            headers={
                "X-Goog-Api-Key": api_key(self._key),
                "X-Goog-FieldMask": FIELD_MASK,
                "Content-Type": "application/json",
            },
        )
        status = response.status_code
        if status in (401, 403):
            raise ProviderQuotaError(
                f"Places refused the request ({status}): the credential is "
                "missing, invalid, or not authorised for this API",
                {"status": status},
            )
        if status == 429:
            raise ProviderQuotaError(
                "Places rate limit or quota exhausted (429). Recorded as a "
                "provider failure; the run does not retry around the limit.",
                {"status": status},
            )
        if status >= 400:
            raise ProviderQuotaError(
                f"Places returned {status}", {"status": status},
            )

        body = response.json()
        places = body.get("places") or []
        records = [
            RawRecord(
                # One place per record, so semantic identity is the place and
                # not the page it arrived on. Two queries finding the same place
                # converge on one version (M2-ADR-041).
                body=json.dumps(place, sort_keys=True, separators=(",", ":"),
                                ensure_ascii=False).encode("utf-8"),
                content_type="application/json",
                native_external_id=place.get("id"),
                source_url=SEARCH_URL,
            )
            for place in places
            if place.get("id")
        ]
        return PlacesPage(records=records,
                          next_page_token=body.get("nextPageToken") or None)

    # -- pure stages -------------------------------------------------------

    def parse(self, raw: RawRecord) -> dict[str, Any]:
        return json.loads(raw.body.decode("utf-8"))

    def normalize(self, parsed: dict[str, Any]) -> CandidateCompany:
        """Map a place onto the candidate shape. Nothing is invented.

        `legal_name` is deliberately left unset: a Places display name is the
        name a business trades under, which its owner sets, and M2 writes
        `legal_name` as a `FACT` about legal registration. Asserting one from
        the other would be a false fact, so the display name becomes a trading
        name (M2-ADR-042).
        """
        display = ((parsed.get("displayName") or {}).get("text") or "").strip()
        components = parsed.get("addressComponents") or []

        def component(*types: str) -> str | None:
            for entry in components:
                if set(entry.get("types") or ()) & set(types):
                    return (entry.get("shortText") or entry.get("longText") or "").strip() or None
            return None

        website = (parsed.get("websiteUri") or "").strip()
        host = urlsplit(website).hostname if website else None

        # Provider taxonomy, carried as hints. M2's claim writer records these
        # as PROXY: a Places category is what Google filed the business under,
        # not a fact that the company satisfies anyone's ICP.
        hints = [t for t in (parsed.get("types") or []) if isinstance(t, str)]
        primary = parsed.get("primaryType")
        if isinstance(primary, str) and primary not in hints:
            hints.insert(0, primary)

        return CandidateCompany(
            legal_name=None,
            trading_names=[display] if display else [],
            registrable_domain=host,
            country=component("country"),
            city=component("locality", "postal_town", "administrative_area_level_3"),
            postal_code=component("postal_code"),
            address=(parsed.get("formattedAddress") or "").strip() or None,
            phone=(parsed.get("nationalPhoneNumber") or "").strip() or None,
            vertical_hints=hints,
            raw_extras={
                "place_id": parsed.get("id"),
                "business_status": parsed.get("businessStatus"),
                "location": parsed.get("location"),
            },
        )
