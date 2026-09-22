"""BoRo Studio strategy seed data.

ADR-006: the engine is generic; these rows are *configuration*. Deleting this
module must leave a working — if empty — system. Ticket prices are seed values,
never engine constants.
"""

from __future__ import annotations

from typing import Any

VERTICALS: list[dict[str, Any]] = [
    {
        "key": "commercial_hvac",
        "name": "Commercial HVAC",
        "description": (
            "Commercial heating, ventilation and air-conditioning contractors "
            "running installation plus recurring maintenance contracts."
        ),
        "taxonomy_codes": {
            "naics": ["238220"],
            "nace": ["43.22"],
            "sic": ["1711"],
        },
    },
    {
        "key": "mechanical_contractors",
        "name": "Mechanical contractors",
        "description": (
            "Mechanical/plumbing/piping contractors with field crews and "
            "project plus service revenue."
        ),
        "taxonomy_codes": {"naics": ["238220", "238290"], "nace": ["43.22", "43.29"]},
    },
    {
        "key": "industrial_maintenance",
        "name": "Industrial maintenance",
        "description": (
            "Third-party industrial maintenance and reliability providers "
            "servicing plant and production assets."
        ),
        "taxonomy_codes": {"naics": ["811310"], "nace": ["33.12"]},
    },
    {
        "key": "facilities_management",
        "name": "Facilities management",
        "description": (
            "Integrated/technical facilities management with multi-site "
            "planned and reactive work orders."
        ),
        "taxonomy_codes": {"naics": ["561210"], "nace": ["81.10"]},
    },
    {
        "key": "refrigeration",
        "name": "Commercial refrigeration",
        "description": (
            "Commercial and industrial refrigeration service, heavy on "
            "emergency response and compliance logging."
        ),
        "taxonomy_codes": {"naics": ["238220", "811310"], "nace": ["33.12", "43.22"]},
    },
    {
        "key": "electrical_contractors",
        "name": "Electrical contractors",
        "description": "Commercial/industrial electrical contractors with service divisions.",
        "taxonomy_codes": {"naics": ["238210"], "nace": ["43.21"]},
    },
    {
        "key": "elevator_service",
        "name": "Elevator and lift service",
        "description": (
            "Elevator/escalator installation and mandated periodic "
            "inspection and maintenance."
        ),
        "taxonomy_codes": {"naics": ["238290"], "nace": ["43.29"]},
    },
    {
        "key": "industrial_equipment_service",
        "name": "Industrial equipment service",
        "description": (
            "OEM and independent service organisations maintaining installed "
            "industrial equipment fleets."
        ),
        "taxonomy_codes": {"naics": ["811310", "333"], "nace": ["33.12", "33.20"]},
    },
    {
        "key": "energy_services",
        "name": "Energy services",
        "description": (
            "Energy service companies and renewables O&M with distributed "
            "assets and scheduled field work."
        ),
        "taxonomy_codes": {"naics": ["221118", "541690"], "nace": ["35.11", "71.12"]},
    },
]


ICPS: list[dict[str, Any]] = [
    {
        "key": "boro_field_service_midmarket_v1",
        "name": "BoRo field-service mid-market (v1)",
        "version": "1",
        "description": (
            "B2B service/maintenance businesses with enough operational "
            "complexity and purchasing power for $15k-$40k+ operational "
            "architecture projects."
        ),
        "definition": {
            "business_model": "B2B",
            "employees": {"min": 20, "max": 150, "preferred": True},
            "field_workers": {"min": 10, "max": 75, "preferred": True},
            "revenue_model": "recurring service or maintenance",
            "positive_signals": [
                "installed assets under contract",
                "work orders",
                "maintenance contracts",
                "dispatch/scheduling of technicians",
                "inventory and parts management",
                "multi-location operations",
                "emergency/after-hours service",
                "field evidence capture (photos, checklists, signatures)",
            ],
            "negative_signals": [
                "pure product resale with no service arm",
                "single-operator businesses",
                "fully office-based delivery",
                "no recurring maintenance revenue",
            ],
            "buyers": [
                "Owner/President",
                "CEO/General Manager",
                "COO/VP Operations",
                "VP Service/Service Manager",
                "Operations Manager",
                "Maintenance Manager",
            ],
            "problem_pattern": (
                "request -> planning -> work order -> dispatch -> technician -> "
                "materials/parts -> evidence -> approval -> close -> invoice -> "
                "asset history"
            ),
            "fragmentation_sources": [
                "ERP/accounting",
                "spreadsheets",
                "email",
                "messaging",
                "paper",
                "field-service tools",
                "undocumented employee knowledge",
            ],
        },
    }
]


OFFERS: list[dict[str, Any]] = [
    {
        "key": "operations_architecture_sprint",
        "name": "Operations Architecture Sprint",
        "description": "Diagnostic and operational blueprint engagement.",
        "currency": "USD",
        "ticket_min": 3000,
        "ticket_max": 7500,
        "definition": {"engagement_weeks": [2, 4], "outcome": "operational blueprint"},
    },
    {
        "key": "operations_os_core",
        "name": "Operations OS Core",
        "description": "Core Operations OS implementation for a single operating unit.",
        "currency": "USD",
        "ticket_min": 15000,
        "ticket_max": 30000,
        "definition": {"engagement_weeks": [8, 16], "outcome": "operational system live"},
    },
    {
        "key": "operations_os_scale",
        "name": "Operations OS Scale",
        "description": "Multi-site or multi-process Operations OS rollout.",
        "currency": "USD",
        "ticket_min": 25000,
        "ticket_max": 60000,
        "definition": {"engagement_weeks": [12, 24], "outcome": "multi-site rollout"},
    },
    {
        "key": "operations_transformation",
        "name": "Operations Transformation",
        "description": "End-to-end operational transformation programme.",
        "currency": "USD",
        "ticket_min": 40000,
        "ticket_max": 100000,
        "definition": {
            "engagement_weeks": [24, 52],
            "outcome": "transformation programme",
            "ticket_max_is_floor": True,
        },
    },
]


CHANNELS: list[dict[str, Any]] = [
    {
        "key": "email",
        "name": "Cold email",
        "definition": {
            "requires": ["b2b_email_legal_access", "language_access"],
            "compliance_note": (
                "B2B email legality varies by jurisdiction; the scoring input is "
                "a simplified proxy and not legal advice."
            ),
        },
    },
    {
        "key": "phone",
        "name": "Phone / cold call",
        "definition": {"requires": ["timezone_overlap", "language_access"]},
    },
    {
        "key": "linkedin",
        "name": "LinkedIn outbound",
        "definition": {"requires": ["language_access"]},
    },
    {
        "key": "partner",
        "name": "Partner / referral",
        "definition": {"requires": ["language_access"], "note": "Indirect motion."},
    },
    {
        "key": "event",
        "name": "Events and associations",
        "definition": {"requires": ["language_access"], "note": "Trade associations."},
    },
    {
        "key": "multichannel",
        "name": "Multichannel sequence",
        "definition": {
            "requires": ["b2b_email_legal_access", "language_access", "timezone_overlap"],
            "note": "Blended email + phone + LinkedIn motion.",
        },
    },
]


#: Deep-dive ``priority_verticals`` strings -> seeded vertical keys.
#: Used to derive market x vertical profiles from snapshot evidence *only*.
#: Anything not matched here stays absent rather than being invented.
DEEP_DIVE_VERTICAL_ALIASES: dict[str, str] = {
    "commercial hvac/mechanical": "commercial_hvac",
    "commercial hvac": "commercial_hvac",
    "hvac": "commercial_hvac",
    "hvac/mechanical": "commercial_hvac",
    "mechanical": "mechanical_contractors",
    "mechanical contracting": "mechanical_contractors",
    "mechanical services": "mechanical_contractors",
    "refrigeration": "refrigeration",
    "industrial equipment service": "industrial_equipment_service",
    "industrial equipment": "industrial_equipment_service",
    "industrial maintenance": "industrial_maintenance",
    "industrial services": "industrial_maintenance",
    "facilities": "facilities_management",
    "facilities management": "facilities_management",
    "facility management": "facilities_management",
    "electrical": "electrical_contractors",
    "electrical contracting": "electrical_contractors",
    "elevator": "elevator_service",
    "elevator service": "elevator_service",
    "lifts": "elevator_service",
    "energy services": "energy_services",
    "energy": "energy_services",
    "renewables o&m": "energy_services",
    # Additional deep-dive spellings observed in the 2026 snapshot.
    "mep": "mechanical_contractors",
    "industrial technical service": "industrial_maintenance",
    "industrial service": "industrial_maintenance",
    "marine/industrial": "industrial_maintenance",
    "industrial/equipment service": "industrial_equipment_service",
    "industrial/equipment": "industrial_equipment_service",
    "equipment service": "industrial_equipment_service",
    "machinery service": "industrial_equipment_service",
    "mining/equipment service": "industrial_equipment_service",
    "data centers": "facilities_management",
    "data-center service": "facilities_management",
}

#: Deep-dive labels seen in the snapshot that are deliberately NOT mapped,
#: because a defensible mapping would be a guess. They are logged at seed time
#: and left out of the profile set rather than being forced into a vertical.
UNMAPPED_DEEP_DIVE_LABELS: tuple[str, ...] = (
    "Building technology",
    "Installation technology",
    "fire/service",
    "equipment",
)
