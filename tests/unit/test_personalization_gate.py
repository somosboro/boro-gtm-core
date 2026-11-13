"""The personalization gate (CE-ADR-022): briefs and drafts, no database, no network."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date

import pytest

from boro_gtm.engine import personalization_gate as gate

TODAY = date(2026, 11, 13)
DOMAINS = {"acme-odoo.cl"}


@dataclass(frozen=True)
class Brief:
    recipient_name: str | None = "Ana Pérez"
    recipient_role: str | None = "Gerente de proyectos"
    recipient_email: str | None = "ana.perez@acme-odoo.cl"
    email_source_url: str | None = "https://www.acme-odoo.cl/equipo"
    fact_text: str | None = "Publicaron dos implementaciones de Odoo 17 en octubre"
    fact_source_url: str | None = "https://www.acme-odoo.cl/blog/odoo-17"
    fact_date: date | None = date(2026, 10, 20)
    hypothesis: str | None = "No sé si les falta gente para módulos a medida en diciembre"
    question: str | None = "¿Cómo resolvieron la última migración de versión que tomaron?"
    why_now: str | None = "Dos proyectos nuevos y fin de año"
    proof_case_id: str | None = "nutrition_practice"


def errors(brief: Brief, mx_ok: bool | None = True) -> list[str]:
    return gate.check_brief(brief, company_domains=DOMAINS, today=TODAY, mx_ok=mx_ok).errors


def test_a_researched_brief_passes():
    assert errors(Brief()) == []


@pytest.mark.parametrize("local", ["info", "contacto", "contact", "hola", "hello", "ventas",
                                   "soporte", "admin", "office"])
def test_a_generic_mailbox_is_refused(local):
    assert any("generic mailbox" in e
               for e in errors(Brief(recipient_email=f"{local}@acme-odoo.cl")))


@pytest.mark.parametrize(("field", "fragment"), [
    ("recipient_name", "recipient_name missing"),
    ("recipient_role", "recipient_role missing"),
    ("why_now", "why_now missing"),
    ("fact_source_url", "fact_source_url missing"),
    ("fact_date", "fact_date missing"),
])
def test_required_fields(field, fragment):
    assert any(fragment in e for e in errors(replace(Brief(), **{field: None})))


def test_the_address_and_its_source_must_be_the_company_s_own():
    assert any("not on the company's domain" in e
               for e in errors(Brief(recipient_email="ana@gmail.com")))
    assert any("email_source_url" in e
               for e in errors(Brief(email_source_url="https://www.linkedin.com/in/ana")))
    # A subdomain of the company counts as its own.
    assert errors(Brief(email_source_url="https://blog.acme-odoo.cl/equipo")) == []


def test_a_domain_without_mx_or_unchecked_is_refused():
    assert any("no MX record" in e for e in errors(Brief(), mx_ok=False))
    assert any("MX not checked" in e for e in errors(Brief(), mx_ok=None))


def test_the_fact_must_be_recent_and_dated():
    assert any("older than 90 days" in e
               for e in errors(Brief(fact_date=date(2026, 8, 1))))
    assert any("future" in e for e in errors(Brief(fact_date=date(2026, 12, 1))))
    assert errors(Brief(fact_date=date(2026, 8, 15))) == []  # 90 days exactly


@pytest.mark.parametrize("hypothesis", ["Les falta gente para diciembre",
                                        "Seguro necesitan ayuda"])
def test_the_hypothesis_must_be_a_doubt(hypothesis):
    assert any("doubt" in e for e in errors(Brief(hypothesis=hypothesis)))


@pytest.mark.parametrize("hypothesis", ["Puede que diciembre les quede corto",
                                        "Podría ser que tengan un frente sin dueño"])
def test_doubt_phrasings_pass(hypothesis):
    assert errors(Brief(hypothesis=hypothesis)) == []


@pytest.mark.parametrize("question", [
    "¿Le interesaría ver cómo funcionaría con uno de sus proyectos?",
    "¿Tienen interés en conversar?",
    "¿Les gustaría una llamada?",
])
def test_interest_questions_are_refused(question):
    assert any("asks for interest" in e for e in errors(Brief(question=question)))


def test_exactly_one_question_about_the_past():
    assert any("exactly one question" in e for e in errors(
        Brief(question="¿Cómo lo resolvieron? ¿Y quién lo hizo?")))
    assert any("past behaviour" in e for e in errors(
        Brief(question="¿Qué planes tienen para el próximo año?")))
    for ok in ("¿Quién tomó el último módulo a medida que entregaron?",
               "¿Cómo han manejado las migraciones de versión este año?",
               "¿Qué pasó la última vez que les faltó un desarrollador?"):
        assert errors(Brief(question=ok)) == [], ok


def test_only_cases_with_written_permission_are_citable():
    assert errors(Brief(proof_case_id="quecompro")) == []
    for case in ("psychotherapy_clinic", "boro_os", None):
        assert any("not a citable case" in e for e in errors(Brief(proof_case_id=case)))


# --- drafts ---------------------------------------------------------------------

GOOD = ("Hola Ana,\n\nVi que publicaron dos implementaciones de Odoo 17 en octubre. No sé si "
        "diciembre les queda corto de manos para módulos a medida.\n\n¿Cómo resolvieron la última "
        "migración de versión que tomaron?\n\nAmaru\nBoRo Studio · somosboro.com\n\n"
        "Si no es relevante, responda \"no\" y no le vuelvo a escribir.")


def test_a_good_draft_passes():
    verdict = gate.check_draft("Acme: migraciones de Odoo", GOOD, recipient_name="Ana Pérez")
    assert verdict.ok and verdict.warnings == []


@pytest.mark.parametrize("claim", ["CFDI", "SUNAT", "DIAN", "Verifactu"])
def test_undelivered_localizations_are_never_claimed(claim):
    body = GOOD.replace("módulos a medida", f"ajustes de {claim}")
    verdict = gate.check_draft("Acme", body)
    assert not verdict.ok and any(claim in e for e in verdict.errors)


def test_dte_chile_may_be_claimed():
    assert gate.check_draft("Acme", GOOD.replace("módulos a medida", "ajustes de DTE")).ok


@pytest.mark.parametrize("case", ["psicoterapia", "BoRo OS"])
def test_cases_without_permission_are_never_cited(case):
    verdict = gate.check_draft("Acme", GOOD.replace("Vi que", f"Hicimos {case}. Vi que"))
    assert not verdict.ok


def test_length_opt_out_attachments_and_interest():
    long_body = GOOD + " palabra" * 100
    assert any("words" in e for e in gate.check_draft("A", long_body).errors)
    no_opt_out = GOOD.split("\n\nSi no es relevante")[0]
    assert any("opt out" in e for e in gate.check_draft("A", no_opt_out).errors)
    assert any("attachments" in e for e in gate.check_draft("A", GOOD, attachments=1).errors)
    pitchy = GOOD.replace("¿Cómo resolvieron", "¿Le interesaría saber cómo resolvemos")
    assert any("interest" in e for e in gate.check_draft("A", pitchy).errors)


def test_warnings_need_a_person_but_do_not_block():
    verdict = gate.check_draft("A", GOOD.replace("Hola Ana", "Hola") + "\nhttps://x.cl",
                               recipient_name="Ana Pérez")
    assert verdict.ok
    assert {"body does not name Ana", "body contains a link"} <= set(verdict.warnings)
