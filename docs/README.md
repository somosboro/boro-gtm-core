# Documentation index

## Current design documents

These describe the system **as built**.

| Document | What it covers |
| --- | --- |
| [ADRS.md](ADRS.md) | Architecture decision records, ADR-001 … ADR-020 |
| [SCORING.md](SCORING.md) | Scoring models, percentile convention, confidence, ranking, both score modes |
| [DESIGN_NOTE_market_gtm_profiles.md](DESIGN_NOTE_market_gtm_profiles.md) | Why `market_gtm_profiles` was removed and where GTM context lives instead |

## M2 — Company Discovery (implemented)

| Document | What it covers |
| --- | --- |
| [M2_COMPANY_DISCOVERY_DESIGN.md](M2_COMPANY_DISCOVERY_DESIGN.md) | Identity anchors vs projections, provider versioning and capability, the attribute registry, entity resolution, run lifecycle, domain policy, temporal relationships |
| [M2_SCHEMA_GRAPH.md](M2_SCHEMA_GRAPH.md) | The table graph as built: ownership, cardinality and indexes |
| [M2_ACCEPTANCE_CRITERIA.md](M2_ACCEPTANCE_CRITERIA.md) | The scenarios M2 satisfies, each naming the test that executes it |
| [M2_ADRS.md](M2_ADRS.md) | 34 decision records, including every correction found while implementing |

The design is at **revision 6**, and the code implements it.

* **Revision 1 → 2** corrected internal contradictions: an impossible
  provider-record constraint, append-only tables that required updates,
  evidence that recorded sources but not values, and an undefined notion of
  "company".
* **Revision 2 → 3** resolved six structural invariants: identity anchors
  versus truncatable projections, concurrent resolution-chain roots, raw-payload
  fidelity, providers without stable external ids, temporal relationships, and
  a typed attribute registry in place of untyped EAV.
* **Revision 3 → 4** removed the last two sources of non-determinism from
  projections and made cross-entity supersession unrepresentable.
* **Revision 4 → 5** put the canonicalization contract into version identity
  and made market presence temporal.
* **Revision 5 → 6** records what *building* it proved: sixteen defects the
  specification could not have revealed on paper, from a resolvability gate
  that a later pipeline stage silently overwrote, to a collision flag stored on
  an append-only table where it could never be set.

Superseded reasoning is retained in the ADRs rather than deleted, so the trail
from each contradiction to its resolution stays readable.

## M3 — Operational Research (design only, not implemented)

The design is at **revision 3**. Revision 2 resolved six structural
contradictions in revision 1, all instances of an append-only row carrying a
value that changes. Revision 3 resolved fourteen more under a second rule: a
globally deduplicated identity row may not carry a fact belonging to one of the
contexts that produced it, and every provenance walk must be single-valued.

| Document | What it covers |
| --- | --- |
| [M3_OPERATIONAL_RESEARCH_DESIGN.md](M3_OPERATIONAL_RESEARCH_DESIGN.md) | Responsibility and boundaries, source/artifact/version model, the claim-ledger audit, operational attribute taxonomy, extraction and model-assisted evidence, contradictions, temporal semantics, gaps and coverage, fetch policy, firewalls |
| [M3_SCHEMA_GRAPH.md](M3_SCHEMA_GRAPH.md) | Proposed table graph: ownership, mutability, keys, indexes and what is deliberately *not* created |
| [M3_ACCEPTANCE_CRITERIA.md](M3_ACCEPTANCE_CRITERIA.md) | 99 Given/When/Then scenarios M3 must satisfy |
| [M3_ADRS.md](M3_ADRS.md) | 28 decision records, including the one-ledger decision and the M3/M4 boundary |

**No M3 code, migrations or tables exist in this repository.** M3 is roadmap
only; the released milestones are M0, M1 and M2.

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
