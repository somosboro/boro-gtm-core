"""A deterministic, versioned, bounded query plan for BoRo's market.

Product-specific on purpose. The intents are the words BoRo's buyers use about
themselves, and the metros are the U.S. commercial markets BoRo sells into —
neither is a general-purpose taxonomy, and pretending otherwise would mean
building a geography engine nobody asked for (M2-ADR-044).

Nothing here knows anything about BoRo's existing outbound cohort. The metros and
intents are chosen from the market definition, never from the locations or names
of companies already known, so a discovery run cannot leak its own evaluation set
(M2-ADR-045).
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: Bump when intents, metros or the step ordering change meaning.
QUERY_PLAN_VERSION = "1"

#: What a commercial mechanical contractor calls itself. Five intents, not fifty:
#: synonyms past this point return the same listings and cost the same money.
QUERY_INTENTS: tuple[str, ...] = (
    "commercial HVAC contractor",
    "commercial mechanical contractor",
    "commercial HVAC service",
    "industrial HVAC contractor",
    "commercial refrigeration contractor",
)

#: Places' own category, used to bias the search without narrowing it to one
#: taxonomy value. Left unset means "whatever the text query matches".
INCLUDED_TYPE: str | None = None


@dataclass(frozen=True, slots=True)
class Metro:
    """One U.S. commercial market, with a coordinate for query bias only.

    The coordinate biases the provider's search. It is query provenance, never
    evidence about a company.
    """

    key: str
    label: str
    state: str
    latitude: float
    longitude: float
    #: Bias radius in metres. 50 km covers a metro's commercial belt without
    #: bleeding into the next one.
    radius_m: int = 50_000


#: Twenty-five U.S. metros, ordered by commercial construction and facilities
#: activity. The first two are the smoke-test set, the first ten the Phase B set,
#: and the whole list the full bounded plan — so a phase is a prefix of this
#: tuple rather than a separate hand-maintained list that can drift out of step.
METROS: tuple[Metro, ...] = (
    Metro("dallas_tx", "Dallas–Fort Worth, TX", "TX", 32.7767, -96.7970),
    Metro("atlanta_ga", "Atlanta, GA", "GA", 33.7490, -84.3880),
    Metro("chicago_il", "Chicago, IL", "IL", 41.8781, -87.6298),
    Metro("houston_tx", "Houston, TX", "TX", 29.7604, -95.3698),
    Metro("phoenix_az", "Phoenix, AZ", "AZ", 33.4484, -112.0740),
    Metro("los_angeles_ca", "Los Angeles, CA", "CA", 34.0522, -118.2437),
    Metro("columbus_oh", "Columbus, OH", "OH", 39.9612, -82.9988),
    Metro("charlotte_nc", "Charlotte, NC", "NC", 35.2271, -80.8431),
    Metro("denver_co", "Denver, CO", "CO", 39.7392, -104.9903),
    Metro("minneapolis_mn", "Minneapolis–St. Paul, MN", "MN", 44.9778, -93.2650),
    Metro("philadelphia_pa", "Philadelphia, PA", "PA", 39.9526, -75.1652),
    Metro("nashville_tn", "Nashville, TN", "TN", 36.1627, -86.7816),
    Metro("seattle_wa", "Seattle, WA", "WA", 47.6062, -122.3321),
    Metro("tampa_fl", "Tampa, FL", "FL", 27.9506, -82.4572),
    Metro("detroit_mi", "Detroit, MI", "MI", 42.3314, -83.0458),
    Metro("st_louis_mo", "St. Louis, MO", "MO", 38.6270, -90.1994),
    Metro("indianapolis_in", "Indianapolis, IN", "IN", 39.7684, -86.1581),
    Metro("kansas_city_mo", "Kansas City, MO", "MO", 39.0997, -94.5786),
    Metro("salt_lake_ut", "Salt Lake City, UT", "UT", 40.7608, -111.8910),
    Metro("las_vegas_nv", "Las Vegas, NV", "NV", 36.1699, -115.1398),
    Metro("orlando_fl", "Orlando, FL", "FL", 28.5383, -81.3792),
    Metro("raleigh_nc", "Raleigh, NC", "NC", 35.7796, -78.6382),
    Metro("cincinnati_oh", "Cincinnati, OH", "OH", 39.1031, -84.5120),
    Metro("milwaukee_wi", "Milwaukee, WI", "WI", 43.0389, -87.9065),
    Metro("portland_or", "Portland, OR", "OR", 45.5152, -122.6784),
)

METROS_BY_KEY = {m.key: m for m in METROS}

#: Phase A / B / C of §18, as prefixes of one ordered list.
SMOKE_METROS = tuple(m.key for m in METROS[:2])
PHASE_B_METROS = tuple(m.key for m in METROS[:10])


@dataclass(frozen=True, slots=True)
class PlanStep:
    """One provider query: one intent, in one metro, at one page.

    Each step becomes one `DiscoveryQuery` row, so every stored record can name
    the intent, metro, provider and page that produced it (§9).
    """

    intent: str
    metro: Metro
    page: int = 0
    page_token: str | None = None

    @property
    def text_query(self) -> str:
        return f"{self.intent} in {self.metro.label}"

    def as_query(self, *, limit: int) -> dict[str, object]:
        """The provider request, and the provenance recorded beside it."""
        query: dict[str, object] = {
            "text_query": self.text_query,
            "limit": limit,
            "region_code": "US",
            "location_bias": {
                "circle": {
                    "center": {"latitude": self.metro.latitude,
                               "longitude": self.metro.longitude},
                    "radius": float(self.metro.radius_m),
                }
            },
            # Provenance, carried on the query row.
            "query_plan_version": QUERY_PLAN_VERSION,
            "intent": self.intent,
            "metro": self.metro.key,
            "metro_label": self.metro.label,
            "page": self.page,
        }
        if INCLUDED_TYPE:
            query["included_type"] = INCLUDED_TYPE
        if self.page_token:
            query["page_token"] = self.page_token
        return query


@dataclass(frozen=True, slots=True)
class QueryBudget:
    """What the run is allowed to spend. No unbounded nationwide loop.

    `max_queries` bounds provider requests, which is what Places bills for;
    `max_results` bounds records stored. Both are checked before each request,
    so the cap is never exceeded rather than noticed afterwards.

    `max_queries = None` means "exactly enough for one page of every planned
    step" — breadth and no pagination. A fixed default of 50 could not execute
    the 125-step full plan, so `metros=all` advertised national coverage while
    silently giving the first 50 steps a chance and none to the rest
    (M2-ADR-051). Pagination is opt-in: raise the number explicitly.
    """

    max_queries: int | None = None
    max_results: int = 1_000
    page_size: int = 20
    #: Provider pages per (intent, metro). Places serves at most three.
    max_pages_per_query: int = 3


@dataclass
class PlannedRun:
    """The plan, costed, before anything touches the network."""

    query_plan_version: str
    intents: tuple[str, ...]
    metros: tuple[str, ...]
    budget: QueryBudget
    steps: list[PlanStep] = field(default_factory=list)

    @property
    def planned_first_page_queries(self) -> int:
        return len(self.steps)

    @property
    def max_queries(self) -> int:
        """The effective cap: breadth-only unless an operator raised it."""
        if self.budget.max_queries is None:
            return self.planned_first_page_queries
        return self.budget.max_queries

    @property
    def first_page_coverage_possible(self) -> bool:
        """Whether the budget can even ask every planned step once."""
        return self.max_queries >= self.planned_first_page_queries

    @property
    def pagination_capacity(self) -> int:
        """Requests left over for second and third pages. Zero by default."""
        return max(self.max_queries - self.planned_first_page_queries, 0)

    @property
    def worst_case_queries(self) -> int:
        """Every step paginated to the provider's limit, capped by the budget."""
        return min(
            len(self.steps) * self.budget.max_pages_per_query, self.max_queries
        )

    @property
    def worst_case_results(self) -> int:
        return min(
            self.worst_case_queries * self.budget.page_size, self.budget.max_results
        )

    def as_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "query_plan_version": self.query_plan_version,
            "provider": "google_places",
            "intents": list(self.intents),
            "metros": list(self.metros),
            "metro_labels": [METROS_BY_KEY[m].label for m in self.metros],
            "first_page_queries": self.planned_first_page_queries,
            "max_queries": self.max_queries,
            "first_page_coverage_possible": self.first_page_coverage_possible,
            "pagination_capacity": self.pagination_capacity,
            "worst_case_queries": self.worst_case_queries,
            "worst_case_results": self.worst_case_results,
            "budget": {
                "max_queries": self.budget.max_queries,
                "effective_max_queries": self.max_queries,
                "max_results": self.budget.max_results,
                "page_size": self.budget.page_size,
                "max_pages_per_query": self.budget.max_pages_per_query,
            },
        }
        if not self.first_page_coverage_possible:
            shortfall = self.planned_first_page_queries - self.max_queries
            payload["WARNING"] = (
                f"max_queries={self.max_queries} cannot ask every planned step "
                f"once: {self.planned_first_page_queries} steps planned, "
                f"{shortfall} would never be attempted. Raise max_queries to "
                f"{self.planned_first_page_queries} for full first-page coverage."
            )
        return payload


def plan_run(
    *,
    metros: tuple[str, ...] | None = None,
    intents: tuple[str, ...] | None = None,
    budget: QueryBudget | None = None,
) -> PlannedRun:
    """Build the plan. Deterministic, and touches no network.

    Step order is `(metro, intent)` in the declared order, so two plans with the
    same inputs are the same plan — which is what lets a run be re-read, and what
    makes a budget cut reproducible rather than arbitrary.
    """
    metro_keys = metros or tuple(m.key for m in METROS)
    unknown = [m for m in metro_keys if m not in METROS_BY_KEY]
    if unknown:
        raise ValueError(f"unknown metro keys: {unknown}; known: {sorted(METROS_BY_KEY)}")
    chosen_intents = intents or QUERY_INTENTS
    budget = budget or QueryBudget()

    steps = [
        PlanStep(intent=intent, metro=METROS_BY_KEY[key])
        for key in metro_keys
        for intent in chosen_intents
    ]
    return PlannedRun(
        query_plan_version=QUERY_PLAN_VERSION,
        intents=tuple(chosen_intents),
        metros=tuple(metro_keys),
        budget=budget,
        steps=steps,
    )
