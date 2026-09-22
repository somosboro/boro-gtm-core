# Documentation index

## Current design documents

These describe the system **as built**.

| Document | What it covers |
| --- | --- |
| [ADRS.md](ADRS.md) | Architecture decision records, ADR-001 … ADR-020 |
| [SCORING.md](SCORING.md) | Scoring models, percentile convention, confidence, ranking, both score modes |
| [DESIGN_NOTE_market_gtm_profiles.md](DESIGN_NOTE_market_gtm_profiles.md) | Why `market_gtm_profiles` was removed and where GTM context lives instead |

## M2 design (not implemented)

| Document | What it covers |
| --- | --- |
| [M2_COMPANY_DISCOVERY_DESIGN.md](M2_COMPANY_DISCOVERY_DESIGN.md) | Canonical identity, provider versioning, claims, entity resolution, run lifecycle, domain policy |
| [M2_SCHEMA_GRAPH.md](M2_SCHEMA_GRAPH.md) | Proposed table graph, ownership, cardinality and indexes |
| [M2_ACCEPTANCE_CRITERIA.md](M2_ACCEPTANCE_CRITERIA.md) | Scenarios M2 must satisfy before it is considered done |
| [M2_ADRS.md](M2_ADRS.md) | Decisions taken during M2 design, including revision-2 corrections |

The design is at **revision 2**. Revision 1 contained internal contradictions —
an impossible provider-record constraint, append-only tables that required
updates, evidence that recorded sources but not values, and an undefined notion
of "company". Those are corrected in place, with the superseded reasoning
retained in the ADRs rather than deleted.

No M2 code, migrations or tables exist in this repository.

## `implementation-pack/` — preserved source material

[`implementation-pack/`](implementation-pack/) holds the original M0/M1 handoff
specification exactly as it was received: ten files, byte-for-byte unmodified.

**It uses the earlier working name "OpenGTM" throughout.** That name was
retired — it is already in use by several active GTM products — and the project
is now *BoRo GTM Core*. See [ADR-008](ADRS.md) for the naming decision.

The pack is kept verbatim rather than rebranded because it is the historical
record the implementation was accepted against: acceptance criteria, scoring
formulas and domain model as originally specified. Editing it to match current
naming would destroy that record's value as an audit trail. Where the pack and
the current design disagree, **the current design documents win**, and the ADRs
say why.

The same reasoning applies to `data/market_intelligence_v1.schema.json`, a
supplied artifact whose `$id` still points at `opengtm.dev`
(see [ADR-019](ADRS.md)).
