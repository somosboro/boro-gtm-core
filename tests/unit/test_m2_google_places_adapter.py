"""The Places adapter against captured responses. CI never reaches Google.

Every provider condition that matters is driven here: field omission, website
normalization, provider taxonomy, pagination, rate limiting, credential refusal
and server failure. A provider adapter whose error paths have never run is one
that will meet them for the first time against a paid API.
"""

from __future__ import annotations

import json

import httpx
import pytest

from boro_gtm.core.errors import GtmError
from boro_gtm.discovery.providers.base import RawRecord
from boro_gtm.discovery.providers.google_places import (
    API_KEY_ENV,
    FIELD_MASK,
    MAX_PAGE_SIZE,
    PROVIDER_KEY,
    SEARCH_URL,
    GooglePlacesAdapter,
    MissingCredentialError,
    ProviderQuotaError,
    api_key,
)

KEY = "test-key-not-a-real-credential"


def _place(**over) -> dict:
    place = {
        "id": "ChIJplace1",
        "displayName": {"text": "Big Mechanical Inc", "languageCode": "en"},
        "websiteUri": "https://www.bigmechanical.com/",
        "formattedAddress": "100 Trade St, Dallas, TX 75201, USA",
        "addressComponents": [
            {"longText": "Dallas", "shortText": "Dallas", "types": ["locality"]},
            {"longText": "Texas", "shortText": "TX",
             "types": ["administrative_area_level_1"]},
            {"longText": "75201", "shortText": "75201", "types": ["postal_code"]},
            {"longText": "United States", "shortText": "US", "types": ["country"]},
        ],
        "nationalPhoneNumber": "(214) 555-0100",
        "types": ["hvac_contractor", "general_contractor", "point_of_interest"],
        "primaryType": "hvac_contractor",
        "businessStatus": "OPERATIONAL",
        "location": {"latitude": 32.7767, "longitude": -96.797},
    }
    place.update(over)
    return place


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _adapter(handler, **kw) -> GooglePlacesAdapter:
    return GooglePlacesAdapter(client=_client(handler), key=KEY, **kw)


def _ok(places, token=None):
    body = {"places": places}
    if token:
        body["nextPageToken"] = token

    def handler(request):
        return httpx.Response(200, json=body)
    return handler


# --- capabilities -----------------------------------------------------------


def test_capabilities_declare_a_production_json_provider():
    caps = GooglePlacesAdapter().capabilities()
    assert caps.provider_key == PROVIDER_KEY
    assert caps.identity_capability == "NATIVE_EXTERNAL_ID"
    assert caps.canonicalization_strategy == "JSON_CANONICAL_V1"
    assert caps.media_type == "application/json"
    assert caps.is_fixture is False
    assert 0.0 < caps.trust_tier < 1.0


def test_the_place_id_is_the_native_external_id():
    """Places documents the place id as durable, so no key is derived."""
    adapter = _adapter(_ok([_place()]))
    records = list(adapter.search({"text_query": "commercial HVAC contractor"}))
    assert [r.native_external_id for r in records] == ["ChIJplace1"]
    assert GooglePlacesAdapter().derive_key(
        GooglePlacesAdapter().normalize(_place())
    ) is None


def test_a_place_without_an_id_is_dropped_rather_than_given_one():
    adapter = _adapter(_ok([_place(), {"displayName": {"text": "No Id Co"}}]))
    records = list(adapter.search({"text_query": "x"}))
    assert len(records) == 1


# --- the request the adapter actually makes --------------------------------


def test_only_the_fields_m2_consumes_are_requested():
    """The field mask is the bill. A field M2 does not read is spend for nothing."""
    seen = {}

    def handler(request):
        seen["mask"] = request.headers["x-goog-fieldmask"]
        seen["key"] = request.headers["x-goog-api-key"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"places": [_place()]})

    list(_adapter(handler).search({"text_query": "commercial HVAC contractor"}))

    requested = set(seen["mask"].split(","))
    assert requested == set(FIELD_MASK.split(","))
    for field in ("places.id", "places.displayName", "places.websiteUri",
                  "places.formattedAddress", "places.types"):
        assert field in requested
    # Fields M2 has no use for are not requested, at any price.
    for unused in ("places.reviews", "places.photos", "places.rating",
                   "places.priceLevel", "places.editorialSummary",
                   "places.currentOpeningHours"):
        assert unused not in requested
    assert seen["key"] == KEY
    assert seen["body"]["regionCode"] == "US"


def test_the_page_size_never_exceeds_the_provider_maximum():
    seen = {}

    def handler(request):
        seen["size"] = json.loads(request.content)["pageSize"]
        return httpx.Response(200, json={"places": []})

    _adapter(handler).search_page({"text_query": "x", "limit": 500})
    assert seen["size"] == MAX_PAGE_SIZE


def test_a_search_without_a_text_query_is_refused_before_the_request():
    calls = []

    def handler(request):                               # pragma: no cover
        calls.append(request)
        return httpx.Response(200, json={"places": []})

    with pytest.raises(GtmError, match="text_query"):
        _adapter(handler).search_page({"text_query": "  "})
    assert calls == []


# --- pagination -------------------------------------------------------------


def test_pagination_follows_the_provider_token_and_stops_when_it_ends():
    pages = [
        ({"places": [_place(id="p1")], "nextPageToken": "T1"}),
        ({"places": [_place(id="p2")], "nextPageToken": "T2"}),
        ({"places": [_place(id="p3")]}),
    ]
    seen_tokens = []

    def handler(request):
        body = json.loads(request.content)
        seen_tokens.append(body.get("pageToken"))
        return httpx.Response(200, json=pages[len(seen_tokens) - 1])

    records = list(_adapter(handler).search({"text_query": "x"}))
    assert [r.native_external_id for r in records] == ["p1", "p2", "p3"]
    assert seen_tokens == [None, "T1", "T2"]


def test_a_single_page_is_addressable_on_its_own():
    """`search_page` is the unit of query provenance: one page, one query row."""
    page = _adapter(_ok([_place()], token="NEXT")).search_page({"text_query": "x"})
    assert len(page.records) == 1
    assert page.next_page_token == "NEXT"


def test_pagination_stops_at_the_provider_page_limit():
    def handler(request):
        return httpx.Response(200, json={"places": [_place()], "nextPageToken": "loop"})

    records = list(_adapter(handler).search({"text_query": "x"}))
    assert len(records) == 3, "Places serves at most three pages per query"


# --- failures ---------------------------------------------------------------


@pytest.mark.parametrize("status", [401, 403])
def test_a_credential_rejection_is_reported_not_worked_around(status):
    with pytest.raises(ProviderQuotaError) as caught:
        _adapter(lambda r: httpx.Response(status)).search_page({"text_query": "x"})
    assert str(status) in str(caught.value)


def test_a_rate_limit_is_recorded_truthfully():
    with pytest.raises(ProviderQuotaError, match="rate limit or quota"):
        _adapter(lambda r: httpx.Response(429)).search_page({"text_query": "x"})


@pytest.mark.parametrize("status", [500, 502, 503])
def test_a_provider_failure_raises_rather_than_returning_nothing(status):
    """An empty result and a broken provider must not look the same."""
    with pytest.raises(ProviderQuotaError):
        _adapter(lambda r: httpx.Response(status)).search_page({"text_query": "x"})


def test_a_missing_credential_fails_before_any_request(monkeypatch):
    monkeypatch.delenv(API_KEY_ENV, raising=False)
    calls = []

    def handler(request):                               # pragma: no cover
        calls.append(request)
        return httpx.Response(200, json={"places": []})

    adapter = GooglePlacesAdapter(client=_client(handler), key=None)
    with pytest.raises(MissingCredentialError, match=API_KEY_ENV):
        adapter.search_page({"text_query": "x"})
    assert calls == [], "no request is made without a credential"


def test_the_credential_comes_from_the_environment_only(monkeypatch):
    monkeypatch.setenv(API_KEY_ENV, "from-env")
    assert api_key() == "from-env"
    monkeypatch.setenv(API_KEY_ENV, "   ")
    with pytest.raises(MissingCredentialError):
        api_key()


# --- normalization ----------------------------------------------------------


def test_the_display_name_becomes_a_trading_name_never_a_legal_name():
    """Google's display name is what a business trades under, not its registration.

    M2 writes `legal_name` as a `FACT` about legal identity, so mapping a Places
    display name onto it would assert a fact nobody established (M2-ADR-042).
    """
    candidate = GooglePlacesAdapter().normalize(_place())
    assert candidate.legal_name is None
    assert candidate.trading_names == ["Big Mechanical Inc"]


def test_the_website_becomes_a_registrable_domain():
    candidate = GooglePlacesAdapter().normalize(_place())
    assert candidate.registrable_domain == "www.bigmechanical.com"
    from boro_gtm.discovery.services.resolution import normalize_domain
    assert normalize_domain(candidate.registrable_domain) == "bigmechanical.com"


def test_address_components_map_to_city_postcode_and_country():
    candidate = GooglePlacesAdapter().normalize(_place())
    assert candidate.city == "Dallas"
    assert candidate.postal_code == "75201"
    assert candidate.country == "US"
    assert candidate.address.endswith("USA")
    assert candidate.phone == "(214) 555-0100"


def test_provider_categories_are_carried_as_hints_with_the_primary_first():
    candidate = GooglePlacesAdapter().normalize(_place())
    assert candidate.vertical_hints[0] == "hvac_contractor"
    assert "general_contractor" in candidate.vertical_hints


def test_nothing_is_invented_that_the_provider_did_not_return():
    """Employee counts, revenue, fleet and branch counts are not in Places."""
    candidate = GooglePlacesAdapter().normalize(_place())
    assert candidate.employee_count_min is None
    assert candidate.employee_count_max is None
    assert candidate.founded_year is None
    assert candidate.legal_form is None
    assert candidate.registry_ids == {}


def test_omitted_fields_become_none_rather_than_empty_strings():
    minimal = {"id": "ChIJbare", "displayName": {"text": "Bare Listing"}}
    candidate = GooglePlacesAdapter().normalize(minimal)
    assert candidate.trading_names == ["Bare Listing"]
    assert candidate.registrable_domain is None
    assert candidate.city is None
    assert candidate.postal_code is None
    assert candidate.country is None
    assert candidate.address is None
    assert candidate.phone is None
    assert candidate.vertical_hints == []


def test_a_listing_with_no_name_at_all_normalizes_without_raising():
    candidate = GooglePlacesAdapter().normalize({"id": "ChIJx"})
    assert candidate.trading_names == []
    assert candidate.legal_name is None


# --- purity and identity ----------------------------------------------------


def test_parse_and_canonicalize_are_pure_and_stable():
    adapter = GooglePlacesAdapter()
    raw = RawRecord(
        body=json.dumps(_place(), sort_keys=True).encode(),
        content_type="application/json", native_external_id="ChIJplace1",
    )
    parsed = adapter.parse(raw)
    assert parsed["id"] == "ChIJplace1"
    assert adapter.canonicalize(parsed) == adapter.canonicalize(parsed)


def test_reordering_the_provider_json_does_not_change_canonical_identity():
    """`JSON_CANONICAL_V1`: a provider reformatting its payload is a no-op."""
    adapter = GooglePlacesAdapter()
    place = _place()
    shuffled = dict(reversed(list(place.items())))
    assert adapter.canonicalize(place) == adapter.canonicalize(shuffled)


def test_the_stored_body_is_the_place_alone_not_the_page():
    """So two queries finding one place converge on one version."""
    adapter = _adapter(_ok([_place(), _place(id="p2")]))
    records = list(adapter.search({"text_query": "x"}))
    for record in records:
        payload = json.loads(record.body)
        assert "places" not in payload
        assert "nextPageToken" not in payload
        assert payload["id"]
    assert records[0].source_url == SEARCH_URL
