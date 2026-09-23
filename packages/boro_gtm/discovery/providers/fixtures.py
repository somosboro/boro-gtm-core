"""TEST/FIXTURE provider adapters.

**These are not production discovery providers.** They exist to prove the
abstraction against genuinely different payload shapes and media types, and to
exercise all three identity capabilities, without any network access. Every one
is registered with ``is_fixture = True`` so it can never be mistaken for a real
source, and no production integration (Apollo, Google Maps, Clay, …) is
invented here — none has been selected.

Three adapters, deliberately different:

* :class:`FixtureJsonDirectoryAdapter` — JSON, ``NATIVE_EXTERNAL_ID``
* :class:`FixtureCsvExportAdapter` — CSV, ``DERIVED_STABLE_KEY``
* :class:`FixtureScrapeAdapter` — JSON, ``CONTENT_ONLY``
"""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Iterator
from typing import Any

from boro_gtm.discovery.enums import CanonicalizationStrategy, ProviderIdentityCapability
from boro_gtm.discovery.providers.base import (
    CandidateCompany,
    ProviderAdapter,
    ProviderCapabilities,
    RawRecord,
)


class _FixtureBase(ProviderAdapter):
    """Serves records from an in-memory list. Never touches the network."""

    def __init__(self, records: list[RawRecord] | None = None) -> None:
        self._records: list[RawRecord] = records or []

    def load(self, records: list[RawRecord]) -> None:
        self._records = list(records)

    def search(self, query: dict[str, Any]) -> Iterator[RawRecord]:
        limit = query.get("limit")
        for index, record in enumerate(self._records):
            if limit is not None and index >= limit:
                return
            yield record


class FixtureJsonDirectoryAdapter(_FixtureBase):
    """A JSON directory that issues durable ids."""

    PROVIDER_KEY = "fixture_json_directory"

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider_key=self.PROVIDER_KEY,
            name="Fixture JSON Directory (TEST ONLY)",
            identity_capability=ProviderIdentityCapability.NATIVE_EXTERNAL_ID.value,
            canonicalization_strategy=CanonicalizationStrategy.JSON_CANONICAL_V1.value,
            canonicalization_version="1",
            media_type="application/json",
            normalizer_version="1",
            trust_tier=0.8,
            supported_filters=("country", "vertical"),
            is_fixture=True,
        )

    def parse(self, raw: RawRecord) -> dict[str, Any]:
        return json.loads(raw.body.decode("utf-8"))

    def normalize(self, parsed: dict[str, Any]) -> CandidateCompany:
        loc = parsed.get("location") or {}
        return CandidateCompany(
            legal_name=parsed.get("legal_name"),
            trading_names=list(parsed.get("trading_names") or []),
            registrable_domain=parsed.get("website_domain"),
            country=loc.get("country"),
            city=loc.get("city"),
            postal_code=loc.get("postal_code"),
            address=loc.get("address"),
            phone=parsed.get("phone"),
            employee_count_min=(parsed.get("employees") or {}).get("min"),
            employee_count_max=(parsed.get("employees") or {}).get("max"),
            founded_year=parsed.get("founded"),
            legal_form=parsed.get("legal_form"),
            registry_ids=dict(parsed.get("registry_ids") or {}),
            vertical_hints=list(parsed.get("categories") or []),
        )


class FixtureCsvExportAdapter(_FixtureBase):
    """A CSV export with no identifier column — the key must be derived."""

    PROVIDER_KEY = "fixture_csv_export"

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider_key=self.PROVIDER_KEY,
            name="Fixture CSV Export (TEST ONLY)",
            identity_capability=ProviderIdentityCapability.DERIVED_STABLE_KEY.value,
            canonicalization_strategy=CanonicalizationStrategy.CSV_ROW_V1.value,
            canonicalization_version="1",
            media_type="text/csv",
            normalizer_version="1",
            key_fields=("registrable_domain",),
            key_algorithm_version="1",
            trust_tier=0.5,
            is_fixture=True,
        )

    def parse(self, raw: RawRecord) -> dict[str, Any]:
        reader = csv.DictReader(io.StringIO(raw.body.decode("utf-8")))
        rows = list(reader)
        if len(rows) != 1:
            raise ValueError(f"CSV fixture record must hold exactly one row, got {len(rows)}")
        return rows[0]

    def normalize(self, parsed: dict[str, Any]) -> CandidateCompany:
        def _int(key: str) -> int | None:
            value = (parsed.get(key) or "").strip()
            return int(value) if value else None

        return CandidateCompany(
            legal_name=(parsed.get("company") or "").strip() or None,
            registrable_domain=(parsed.get("domain") or "").strip().lower() or None,
            country=(parsed.get("country") or "").strip() or None,
            city=(parsed.get("city") or "").strip() or None,
            postal_code=(parsed.get("zip") or "").strip() or None,
            employee_count_min=_int("staff_min"),
            employee_count_max=_int("staff_max"),
        )


class FixtureScrapeAdapter(_FixtureBase):
    """A source with no stable object identity at all.

    Keyed by content hash, so any payload change creates a new entity. Such a
    provider cannot express "the same object changed", and its trust tier
    reflects that.
    """

    PROVIDER_KEY = "fixture_scrape"

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider_key=self.PROVIDER_KEY,
            name="Fixture Scrape (TEST ONLY)",
            identity_capability=ProviderIdentityCapability.CONTENT_ONLY.value,
            canonicalization_strategy=CanonicalizationStrategy.JSON_CANONICAL_V1.value,
            canonicalization_version="1",
            media_type="application/json",
            normalizer_version="1",
            trust_tier=0.3,
            is_fixture=True,
        )

    def parse(self, raw: RawRecord) -> dict[str, Any]:
        return json.loads(raw.body.decode("utf-8"))

    def normalize(self, parsed: dict[str, Any]) -> CandidateCompany:
        return CandidateCompany(
            legal_name=parsed.get("title"),
            registrable_domain=parsed.get("domain"),
            city=parsed.get("city"),
            country=parsed.get("country"),
            vertical_hints=list(parsed.get("tags") or []),
        )


FIXTURE_ADAPTERS: dict[str, type[_FixtureBase]] = {
    FixtureJsonDirectoryAdapter.PROVIDER_KEY: FixtureJsonDirectoryAdapter,
    FixtureCsvExportAdapter.PROVIDER_KEY: FixtureCsvExportAdapter,
    FixtureScrapeAdapter.PROVIDER_KEY: FixtureScrapeAdapter,
}


def json_record(payload: dict[str, Any], external_id: str | None = None,
                indent: int | None = None) -> RawRecord:
    """Build a JSON fixture record. ``indent`` lets a test produce
    byte-different but semantically identical payloads."""
    body = json.dumps(payload, indent=indent).encode("utf-8")
    return RawRecord(body=body, content_type="application/json",
                     native_external_id=external_id)


def csv_record(row: dict[str, str]) -> RawRecord:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(row))
    writer.writeheader()
    writer.writerow(row)
    return RawRecord(body=buffer.getvalue().encode("utf-8"), content_type="text/csv")
