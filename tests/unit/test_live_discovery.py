"""Relevance and budget policy. Deterministic, no database, no network."""

from __future__ import annotations

import pytest

from boro_gtm.research.live.policy import (
    FIRST_PARTY_POLICY_VERSION,
    CrawlBudget,
    is_relevant,
    relevance_hint,
)


@pytest.mark.parametrize("path", [
    "/", "/services", "/services/commercial", "/commercial-hvac",
    "/preventive-maintenance", "/about-us", "/company/history",
    "/locations", "/locations/columbus", "/careers", "/jobs/hvac-technician",
    "/industries/healthcare", "/our-fleet", "/projects/data-center",
    "/contact-us", "/24-7-emergency-service",
])
def test_operational_pages_are_worth_fetching(path):
    assert is_relevant(f"https://bigmechanical.com{path}")


@pytest.mark.parametrize("path", [
    "/privacy-policy", "/terms-of-service", "/cookie-policy",
    "/blog/", "/blog/5-hvac-tips", "/news/2025/award", "/tag/hvac",
    "/category/commercial", "/author/jane", "/page/4", "/feed",
    "/cart", "/checkout", "/my-account", "/wp-login.php", "/wp-json/v2",
    "/logo.png", "/style.css", "/app.js", "/brochure.zip", "/hero.mp4",
    "/search?q=service",
])
def test_low_value_areas_are_skipped(path):
    assert not is_relevant(f"https://bigmechanical.com{path}")


def test_a_blog_post_about_commercial_service_is_still_a_blog_post():
    """The exclusions are checked first, on purpose.

    "/blog/commercial-hvac-maintenance-tips" matches three relevant terms and is
    marketing content, not a statement about how this company operates.
    """
    assert not is_relevant(
        "https://bigmechanical.com/blog/commercial-hvac-maintenance-tips"
    )


def test_the_homepage_always_earns_a_fetch():
    """Even when its path says nothing, it is the page M2 pointed at."""
    assert is_relevant("https://bigmechanical.com/")
    assert relevance_hint("https://bigmechanical.com/") == 1.0


def test_relevance_ordering_puts_operational_pages_before_contact_details():
    """The hint only decides fetch order when the budget is tight."""
    assert (relevance_hint("https://x.com/services/commercial")
            > relevance_hint("https://x.com/about"))
    assert (relevance_hint("https://x.com/about")
            > relevance_hint("https://x.com/projects"))
    assert (relevance_hint("https://x.com/careers")
            > relevance_hint("https://x.com/contact"))
    assert relevance_hint("https://x.com/whatever") == 0.40


def test_the_policy_is_versioned():
    """It is a judgement about one market, not a fact, so a run records it."""
    assert FIRST_PARTY_POLICY_VERSION


def test_the_budget_defaults_are_conservative():
    """A pilot, not a scrape. Asserted so a later change is a visible decision."""
    budget = CrawlBudget()
    assert budget.max_pages <= 30
    assert budget.max_depth <= 2
    assert budget.max_retrievals <= 50
    assert budget.max_bytes <= 10_000_000
    assert budget.delay_seconds >= 1.0
    assert budget.max_redirects <= 5
    assert budget.timeout_seconds <= 30


def test_presentation_parameters_do_not_make_a_second_document():
    """Found in the first pilot, against a real site.

    `/service-maintenance` and `/service-maintenance?hsLang=en` returned
    byte-identical bodies. M3's corroboration counted them as one document, so
    confidence was never inflated — but they were still two requests for one
    page, and the site was owed no reason for the second.
    """
    from boro_gtm.research.live.policy import strip_presentation_params

    base = "https://bigmechanical.com/service-maintenance"
    for noisy in (f"{base}?hsLang=en", f"{base}?lang=en", f"{base}?gclid=xyz",
                  f"{base}?hsLang=en&lang=es"):
        assert strip_presentation_params(noisy) == base

    # A parameter that really does select a different document survives.
    paged = f"{base}?location=columbus"
    assert strip_presentation_params(paged) == paged
    assert strip_presentation_params(f"{base}?location=columbus&hsLang=en") == paged


# --- §4 the default budget buys breadth, and says what it cannot do ----------


def test_the_default_budget_is_exactly_one_page_per_planned_step():
    """A fixed default of 50 could not execute the 125-step full plan.

    `metros=all` then advertised national coverage while silently giving the
    first 50 steps a chance and none to the other 75 (M2-ADR-051).
    """
    from boro_gtm.discovery.live.plan import (
        METROS,
        PHASE_B_METROS,
        SMOKE_METROS,
        plan_run,
    )

    for metros, expected in ((SMOKE_METROS, 10), (PHASE_B_METROS, 50),
                             (tuple(m.key for m in METROS), 125)):
        plan = plan_run(metros=metros)
        assert plan.planned_first_page_queries == expected
        assert plan.max_queries == expected, "breadth, exactly"
        assert plan.first_page_coverage_possible is True
        assert plan.pagination_capacity == 0, "pagination is opt-in"


def test_raising_the_budget_buys_pagination_capacity():
    from boro_gtm.discovery.live.plan import QueryBudget, plan_run

    plan = plan_run(budget=QueryBudget(max_queries=200))
    assert plan.planned_first_page_queries == 125
    assert plan.max_queries == 200
    assert plan.pagination_capacity == 75
    assert plan.first_page_coverage_possible is True
    assert "WARNING" not in plan.as_dict()


def test_a_budget_below_first_page_coverage_warns_loudly_before_spend():
    from boro_gtm.discovery.live.plan import QueryBudget, plan_run

    plan = plan_run(budget=QueryBudget(max_queries=50))
    payload = plan.as_dict()
    assert plan.first_page_coverage_possible is False
    assert plan.pagination_capacity == 0
    warning = payload["WARNING"]
    assert "125 steps planned" in warning
    assert "75 would never be attempted" in warning
    assert "Raise max_queries to 125" in warning


def test_the_plan_reports_the_four_coverage_fields():
    from boro_gtm.discovery.live.plan import SMOKE_METROS, plan_run

    payload = plan_run(metros=SMOKE_METROS).as_dict()
    for field in ("first_page_queries", "max_queries",
                  "first_page_coverage_possible", "pagination_capacity"):
        assert field in payload, field


def test_phases_are_prefixes_so_they_cannot_drift_apart():
    from boro_gtm.discovery.live.plan import METROS, PHASE_B_METROS, SMOKE_METROS

    keys = tuple(m.key for m in METROS)
    assert SMOKE_METROS == keys[:2]
    assert PHASE_B_METROS == keys[:10]
    assert SMOKE_METROS == PHASE_B_METROS[:2]
