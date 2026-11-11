"""Copy v3 (strategy 2026-11.2, active since 2026-11-11): short, partner-situation hook,
nearshore pitch, interest CTA. The superseded 2026-11.1 (v1) is loaded directly, only to
prove v3 changed copy and never rules.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from boro_gtm.engine import outreach
from boro_gtm.engine.pipeline import EngineError
from boro_gtm.engine.strategy import Strategy, load_strategy

REPO = Path(__file__).resolve().parents[2]
DRAFT_PATH = REPO / "commercial" / "strategies" / "active-2026-11.2.json"
V1_PATH = REPO / "commercial" / "strategies" / "superseded-2026-11.1.json"
LEFTOVER = re.compile(r"\{[a-z_]+\}")
SIGNATURE = "Amaru\nBoRo Studio · somosboro.com"
ODOO_EVIDENCE = "aparece como Silver Partner en el directorio oficial de Odoo para Chile"
FIRM_EVIDENCE = "desarrolla software para clientes, según su perfil en Get on Board"


def _strategy(raw: dict, path: Path = DRAFT_PATH) -> Strategy:
    data = json.dumps(raw).encode()
    return Strategy(raw=raw, sha256=hashlib.sha256(data).hexdigest(), path=path)


@pytest.fixture(scope="module")
def draft() -> Strategy:
    return _strategy(json.loads(DRAFT_PATH.read_text(encoding="utf-8")))


@pytest.fixture(scope="module")
def active() -> Strategy:
    """The superseded v1 (2026-11.1), read directly: the engine no longer loads it."""
    return _strategy(json.loads(V1_PATH.read_text(encoding="utf-8")), V1_PATH)


def _objs(*, lane="PARTNER_DELIVERY", country="CL", name="Blue Minds SpA",
          domain="blueminds.cl", email="contacto@blueminds.cl", generic=True, full_name=None,
          role=None, segment="odoo_partner", evidence=ODOO_EVIDENCE,
          title="Silver Partner — Odoo partner directory (CL)"):
    target = SimpleNamespace(id=uuid.uuid4(), lane=lane, country=country)
    account = SimpleNamespace(name=name, domain=domain)
    contact = SimpleNamespace(email=email, is_generic=generic, full_name=full_name, role=role)
    signal = SimpleNamespace(title=title, excerpt="", attributes={
        "segment": segment, "evidence_es": evidence, "evidence_en": "is listed as a partner"})
    return dict(target=target, account=account, contact=contact, signal=signal)


def _words_before_signature(body: str) -> int:
    return len(body.split(SIGNATURE)[0].split())


# --- v1 is preserved as it was -----------------------------------------------------

def test_the_superseded_v1_still_renders_exactly_as_before(active):
    out = outreach.render(active, step="INITIAL", **_objs())
    assert out["body"].startswith("Hola equipo,\n\nVi que Blue Minds SpA aparece como Silver")
    assert out["subject"] == "Capacidad de entrega para Blue Minds SpA"
    assert out["variant"] == "partner-capacity-v1"


def test_the_engine_loads_v3_and_never_the_superseded_v1():
    assert load_strategy().version == "2026-11.2"
    assert load_strategy().path == DRAFT_PATH
    with pytest.raises(ValueError, match="not an ACTIVE strategy"):
        load_strategy(V1_PATH)


def test_the_draft_changes_copy_only_never_rules(draft, active):
    changed = {k for k in set(draft.raw) | set(active.raw)
               if draft.raw.get(k) != active.raw.get(k)}
    assert changed <= {"strategy_version", "status", "supersedes", "superseded_by", "effective_from",
                       "change_note", "copy", "templates", "proof_points"}
    assert draft.raw["proof_points"].keys() == active.raw["proof_points"].keys()


def test_the_draft_names_no_client_and_repeats_no_unverified_claim(draft):
    """Proof points describe BoRo's own code, anonymized: there is no written consent to name
    clients (acos/specs/somosboro-web-b2b-casos.md). The Qué Compro API runs on Express, and
    the catalog size could not be verified, so neither old claim may come back."""
    text = json.dumps({k: draft.raw[k] for k in ("copy", "templates", "proof_points")},
                      ensure_ascii=False).lower()
    for banned in ("psicoterapiamundial", "vanessa", "lanutrivane", "envista", "nestjs",
                   "6.027", "6,027"):
        assert banned not in text, banned


def test_odoo_partners_cite_the_odoo_clinic_and_software_firms_cite_que_compro(draft):
    odoo = outreach.render(draft, step="INITIAL", **_objs())["body"]
    assert "módulos Odoo 17 para una clínica de psicoterapia online: agenda con pagos" in odoo
    firm = outreach.render(draft, step="INITIAL", **_objs(
        segment="delivery_firm_directory", evidence=FIRM_EVIDENCE,
        title="Software delivery firm — Get on Board company profile"))["body"]
    assert "Qué Compro, una app en Expo con API en Express" in firm


# --- the v3 message ---------------------------------------------------------------

def test_an_odoo_partner_gets_the_partner_hook_its_localization_and_a_team_question(draft):
    out = outreach.render(draft, step="INITIAL", **_objs())
    body = out["body"]
    assert out["subject"] == "Blue Minds: ¿un frente de DTE sin quién lo tome?"
    assert body.startswith("Hola, equipo de Blue Minds,\n\nCuando un partner Odoo tiene más "
                           "proyectos que equipo")
    assert "un ajuste de DTE." in body
    assert "bajo su marca, en su horario y en español" in body
    assert "¿Les interesaría ver cómo funcionaría con uno de sus proyectos?" in body
    assert SIGNATURE in body and "Founder" not in body
    assert body.rstrip().endswith('responde "no" y no te vuelvo a escribir.')
    assert out["variant"] == "partner-capacity-v3"


def test_a_named_contact_is_addressed_as_usted(draft):
    out = outreach.render(draft, step="INITIAL", **_objs(
        country="MX", email="manuel.capdevielle@iosbox.mx", generic=False, name="IOSBOX Spa"))
    assert out["body"].startswith("Hola Manuel,")
    assert "¿Le interesaría ver cómo funcionaría" in out["body"]
    assert "¿Les interesaría" not in out["body"]
    assert out["subject"] == "IOSBOX: ¿un frente de CFDI sin quién lo tome?"


@pytest.mark.parametrize(("country", "word"), [
    ("PE", "SUNAT"), ("MX", "CFDI"), ("CO", "DIAN"), ("AR", "ARCA"), ("UY", "localización")])
def test_each_country_gets_its_own_e_invoicing_word(draft, country, word):
    out = outreach.render(draft, step="INITIAL", **_objs(country=country))
    assert f"un ajuste de {word}." in out["body"]
    assert out["subject"] == f"Blue Minds: ¿un frente de {word} sin quién lo tome?"


def test_a_software_firm_that_is_not_an_odoo_partner_gets_the_general_hook(draft):
    out = outreach.render(draft, step="INITIAL", **_objs(
        segment="delivery_firm_directory", name="experti", domain="experti.cl",
        evidence=FIRM_EVIDENCE, title="Software delivery firm — Get on Board company profile"))
    assert out["subject"] == "experti: ¿un frente técnico sin quién lo tome?"
    assert "Cuando una empresa de software tiene más proyectos" in out["body"]
    assert "partner Odoo" not in out["body"] and "DTE" not in out["body"]


def test_an_imported_row_whose_evidence_names_odoo_gets_the_partner_hook(draft):
    out = outreach.render(draft, step="INITIAL", **_objs(
        segment="imported_pool",
        evidence="aparece como Gold Partner en el directorio oficial de Odoo"))
    assert "Cuando un partner Odoo" in out["body"]


@pytest.mark.parametrize(("lane", "country", "segment", "evidence"), [
    ("PARTNER_DELIVERY", "CL", "odoo_partner", ODOO_EVIDENCE),
    ("PARTNER_DELIVERY", "CL", "delivery_firm_directory", FIRM_EVIDENCE),
    ("PARTNER_DELIVERY", "US", "odoo_partner", ODOO_EVIDENCE),
    ("TRIGGER_OVERFLOW", "CL", "delivery_firm_hiring",
     'está contratando "Desarrollador/a Full-Stack" en Get on Board'),
    ("TRIGGER_OVERFLOW", "US", "delivery_firm_hiring", "is hiring"),
])
def test_first_emails_stay_short(draft, lane, country, segment, evidence):
    out = outreach.render(draft, step="INITIAL", **_objs(lane=lane, country=country,
                                                         segment=segment, evidence=evidence))
    # Under ~80 words before the signature (benchmark: top campaigns stay below 80).
    assert _words_before_signature(out["body"]) <= 80, out["body"]


def test_a_hiring_company_is_offered_cover_while_it_hires(draft):
    out = outreach.render(draft, step="INITIAL", **_objs(
        lane="TRIGGER_OVERFLOW", name="Xintec", domain="xintec.cl", segment="delivery_firm_hiring",
        evidence='está contratando "Desarrollador/a Full-Stack" en Get on Board'))
    assert out["subject"] == "Mientras Xintec contrata"
    assert 'Vi que Xintec está contratando "Desarrollador/a Full-Stack"' in out["body"]
    assert "¿Les interesaría que les cuente cómo lo haríamos?" in out["body"]


def test_a_follow_up_cites_one_short_piece_of_work_for_the_segment(draft):
    odoo = outreach.render(draft, step="F1", **_objs(country="MX"))["body"]
    assert "si tienen un proyecto con un ajuste de CFDI o una migración de versión esperando" in odoo
    firm = outreach.render(draft, step="F1", **_objs(segment="delivery_firm_directory",
                                                     evidence=FIRM_EVIDENCE))["body"]
    assert "una API o una integración que no avanza" in firm and "CFDI" not in firm


def test_the_odoo_follow_up_adds_boro_os_and_the_others_add_nothing(draft):
    odoo = outreach.render(draft, step="F1", **_objs())["body"]
    assert ("sin compromiso. Es el mismo tipo de trabajo de BoRo OS, nuestra plataforma sobre "
            "Odoo para RRHH, cobranza, inventario y ventas.\n\n") in odoo
    assert "BoRo OS" not in outreach.render(draft, step="INITIAL", **_objs())["body"]
    firm = outreach.render(draft, step="F1", **_objs(segment="delivery_firm_directory",
                                                     evidence=FIRM_EVIDENCE))["body"]
    assert "BoRo OS" not in firm and "sin compromiso.\n\nAmaru" in firm


def test_a_template_that_needs_copy_the_strategy_lacks_is_refused(draft):
    broken = copy.deepcopy(draft.raw)
    del broken["copy"]["hook"]
    with pytest.raises(EngineError, match="hook"):
        outreach.render(_strategy(broken), step="INITIAL", **_objs())


@pytest.mark.parametrize("lane", ["PARTNER_DELIVERY", "EXPLICIT_DEMAND", "TRIGGER_OVERFLOW"])
@pytest.mark.parametrize("country", ["CL", "US"])
@pytest.mark.parametrize("step", ["INITIAL", "F1", "F2"])
def test_every_draft_template_renders_with_no_empty_or_leftover_placeholder(draft, lane, country,
                                                                            step):
    out = outreach.render(draft, step=step, **_objs(lane=lane, country=country))
    assert not LEFTOVER.search(out["subject"] + out["body"])
    assert SIGNATURE in out["body"]
    assert "\n\n\n" not in out["body"] and " ," not in out["body"] and "  " not in out["body"]
    assert out["language"] == ("es" if country == "CL" else "en")


# --- names -------------------------------------------------------------------------

@pytest.mark.parametrize(("email", "generic", "full_name", "role", "expected"), [
    ("manuel.capdevielle@iosbox.cl", False, None, None, "Manuel"),
    ("ana_perez@x.cl", False, None, None, "Ana"),
    ("grobles@blueminds.cl", False, None, None, None),     # no separator: not a sure name
    ("contacto@blueminds.cl", True, None, None, None),     # generic desk
    ("soporte.tecnico@x.cl", False, None, None, None),     # role mailbox
    ("x@x.cl", False, "Lucía Fernández", None, "Lucía"),   # a recorded full name wins
    ("x@x.cl", True, "Equipo Ventas", None, None),         # never a name for a generic box
])
def test_a_name_is_used_only_when_the_contact_is_clearly_one_person(email, generic, full_name,
                                                                     role, expected):
    contact = SimpleNamespace(email=email, is_generic=generic, full_name=full_name, role=role)
    assert outreach.person_name(contact) == expected


@pytest.mark.parametrize(("name", "short"), [
    ("Blue Minds SpA", "Blue Minds"),
    ("Visión Empresarial en Datos SPA", "Visión Empresarial en Datos"),
    ("BCN CONSULTORES LIMITADA", "BCN CONSULTORES"), ("Alwa Peru SA", "Alwa Peru"),
    ("ALABOOL TECHNOLOGIES S.A. de C.V.", "ALABOOL TECHNOLOGIES"),
    ("Ingeniosys Ingeniería de software SA de CV", "Ingeniosys Ingeniería de software"),
    ("Giraffos", "Giraffos"), ("1-800CLOUD SAS", "1-800CLOUD"), ("Spa", "Spa"),
    ("G&S GESTION Y SISTEMAS S.A.C", "G&S GESTION Y SISTEMAS"), ("NEXTPRO S.A.C", "NEXTPRO"),
    ("I.A.S Ingeniería, Aplicaciones y Software S.A.S", "I.A.S Ingeniería, Aplicaciones y Software"),
    ("OD.ONE. S.A.S.", "OD.ONE")])
def test_legal_forms_are_dropped_from_the_short_name(draft, name, short):
    assert outreach.short_company(draft, name) == short


def test_the_superseded_v1_keeps_full_names(active):
    assert outreach.short_company(active, "Blue Minds SpA") == "Blue Minds SpA"
