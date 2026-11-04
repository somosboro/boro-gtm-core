"""What makes a company a software *delivery* firm, as it describes itself.

Shared by the adapters that turn a company profile or a hiring post into a
partner or overflow signal. The test is the company's own words: it builds
software for clients (a factory, a consultancy, an implementer, an agency with
engineering, staff augmentation). A product company hiring for its own product
is not a delivery partner, however many developers it hires.
"""

from __future__ import annotations

import re

#: Phrases that by themselves say "we build software for clients".
DELIVERY_FIRM = re.compile(
    r"(software factory|f[aá]brica de software|desarrollo (de software )?a medida"
    r"|software a medida|custom software|consultora (de |en )?(tecnol|software|ti\b|it\b)"
    r"|consultor[ií]a (tecnol[oó]gica|de software|en ti\b|ti\b|it\b)"
    r"|staff augmentation|nearshor|body ?shopping|software house|dev shop"
    r"|servicios (de )?(desarrollo de software|ingenier[ií]a de software)"
    r"|implementaci[oó]n de (erp|odoo|sap|salesforce|crm)"
    r"|partner (de|of) (odoo|sap|microsoft|salesforce|google cloud|aws)"
    r"|integrador(a)? de sistemas|system integrator)", re.I)
#: Phrases that say "we serve clients" or "IT services": delivery only when the
#: same text is about building software, not marketing or recruiting.
CLIENT_WORK = re.compile(
    r"((para|a) (nuestros|sus|diversos|distintos) clientes|our clients|client projects"
    r"|proyectos (de|para) (clientes|nuestros clientes)|outsourcing"
    r"|servicios (de )?(desarrollo|tecnolog[ií]a|ti\b|it\b)"
    r"|agencia (digital|de desarrollo|de software)|digital agency|transformaci[oó]n digital)",
    re.I)
SOFTWARE_CONTEXT = re.compile(
    r"(desarroll|software|aplicaci[oó]n|aplicaciones|apps?\b|plataformas? (web|digital)"
    r"|sistemas|erp\b|crm\b|integraci[oó]n|cloud|nube|backend|frontend|full.?stack)", re.I)


def delivery_evidence(text: str) -> str | None:
    """The phrase that shows client software delivery, or None."""
    text = text or ""
    strong = DELIVERY_FIRM.search(text)
    if strong:
        return strong.group(0)
    weak = CLIENT_WORK.search(text)
    if weak and SOFTWARE_CONTEXT.search(text):
        return weak.group(0)
    return None
