"""Engine pieces that need neither a database nor a network."""

from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient

from boro_gtm.engine import states as S
from boro_gtm.engine.business_days import BusinessCalendar
from boro_gtm.engine.pipeline import account_key, normalize_email
from boro_gtm.engine.scoring import score_target
from boro_gtm.engine.settings import engine_settings
from boro_gtm.engine.strategy import load_strategy


@pytest.fixture
def strategy():
    return load_strategy()


def test_the_active_strategy_is_versioned_and_never_the_old_doctrine(strategy):
    assert strategy.version == "2026-11.1"
    text = strategy.path.read_text().lower()
    assert "architecture sprint" not in text.replace("architecture sprint entry offer", "")
    assert strategy.daily_cap == 20
    assert set(strategy.lane_mix) == set(S.LANES)


def test_shadow_mode_is_on_unless_explicitly_disabled(monkeypatch):
    monkeypatch.delenv("GTM_ENGINE_SHADOW_MODE", raising=False)
    assert engine_settings().shadow_mode is True
    monkeypatch.setenv("GTM_ENGINE_SHADOW_MODE", "")
    assert engine_settings().shadow_mode is True
    monkeypatch.setenv("GTM_ENGINE_SHADOW_MODE", "false")
    assert engine_settings().shadow_mode is False


def test_every_open_state_can_reach_a_terminal_state():
    for state in S.OPEN_STATES:
        seen, frontier = set(), {state}
        while frontier:
            current = frontier.pop()
            seen.add(current)
            frontier |= set(S.TRANSITIONS[current]) - seen
        assert seen & set(S.TERMINAL_STATES), state


def test_terminal_states_are_terminal():
    for state in S.TERMINAL_STATES:
        assert S.TRANSITIONS[state] == frozenset()


def test_business_days_skip_weekends_and_holidays(strategy):
    cal = BusinessCalendar(strategy)
    assert cal.add_business_days(date(2026, 11, 19), 1) == date(2026, 11, 23)  # Thu → (Fri hol) Mon


@pytest.mark.parametrize(("raw", "expected"), [
    ("Ana@Acme.CL", "ana@acme.cl"), ("mailto:ana@acme.cl?subject=x", "ana@acme.cl"),
    ("not-an-email", None), ("", None)])
def test_email_normalization(raw, expected):
    assert normalize_email(raw) == expected


@pytest.mark.parametrize(("domain", "key"), [
    ("https://www.acme.cl/x", "acme.cl"), ("acme.lovable.app", None), ("facebook.com", None)])
def test_account_keys_follow_m2_identity_rules(domain, key):
    assert account_key(domain) == key


def test_a_fresh_explicit_request_scores_high_priority(strategy):
    from datetime import UTC, datetime, timedelta

    score = score_target(strategy, kind="EXPLICIT_DEMAND",
                         title="Buscamos proveedor freelance para integración Odoo y API",
                         excerpt="proyecto urgente, presupuesto definido", 
                         posted_at=datetime.now(UTC) - timedelta(days=2), expires_at=None,
                         has_verified_channel=True, channel_is_person=True)
    assert score.total >= strategy.high_priority


def test_a_vague_old_trigger_does_not_qualify(strategy):
    from datetime import UTC, datetime, timedelta

    score = score_target(strategy, kind="HISTORIC_TRIGGER", title="Empresa", excerpt="",
                         posted_at=datetime.now(UTC) - timedelta(days=90), expires_at=None,
                         has_verified_channel=False, channel_is_person=False)
    assert score.total < strategy.threshold


def test_the_engine_api_is_closed_without_a_token(monkeypatch):
    from boro_gtm.api.main import create_app

    monkeypatch.delenv("GTM_ENGINE_API_TOKEN", raising=False)
    client = TestClient(create_app())
    assert client.get("/api/v1/engine/ready").status_code == 503


def test_the_engine_api_refuses_a_wrong_token(monkeypatch):
    from boro_gtm.api.main import create_app

    monkeypatch.setenv("GTM_ENGINE_API_TOKEN", "right-token-for-tests")
    client = TestClient(create_app())
    response = client.get("/api/v1/engine/ready", headers={"Authorization": "Bearer wrong"})
    assert response.status_code == 401


def test_keywords_match_whole_words_only(strategy):
    from boro_gtm.engine.scoring import _hits

    assert _hits("Escríbenos por WhatsApp y apply now", ["app", "api"]) == []
    assert _hits("Necesitamos una app y una API REST", ["app", "api"]) == ["api", "app"]


def test_a_non_software_posting_cannot_qualify_on_keywords(strategy):
    from datetime import UTC, datetime, timedelta

    score = score_target(strategy, kind="EXPLICIT_DEMAND",
                         title="Growth Hacker & Acquisition Manager",
                         excerpt="freelance, trabajarás con nuestra app y plataforma web, API",
                         posted_at=datetime.now(UTC) - timedelta(days=1), expires_at=None,
                         has_verified_channel=True, channel_is_person=False)
    assert score.total < strategy.threshold
    assert "title_not_software_delivery" in score.components["technical_fit"]["evidence"]
