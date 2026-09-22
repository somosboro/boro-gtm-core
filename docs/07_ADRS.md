# 07 — Architecture Decision Records

## ADR-001 — Modular monolith first

**Status:** Accepted

Use one deployable backend with clear modules and worker-ready boundaries.

Reason:
- BoRo is currently a small development team.
- distributed microservices would increase operational burden before scale requires them.
- domain boundaries can later be extracted.

Rejected for M0/M1:
- Kafka-first
- Kubernetes-first
- service-per-module architecture

---

## ADR-002 — Scores are immutable run outputs, not market attributes

**Status:** Accepted

A country's score depends on model, snapshot, universe and context. Therefore canonical `markets` must not contain mutable score fields.

---

## ADR-003 — Snapshot immutability

**Status:** Accepted

Imported market-intelligence artifacts are immutable and content-hashed. New research creates a new snapshot/version rather than rewriting historical data.

---

## ADR-004 — Provenance over false precision

**Status:** Accepted

Unknown values remain unknown. We prefer lower coverage/confidence to fabricated completion.

---

## ADR-005 — Score / confidence / coverage are separate

**Status:** Accepted

A market may have a high estimated score and low evidence coverage. APIs and UI must not collapse these dimensions.

---

## ADR-006 — OpenGTM core remains generic

**Status:** Accepted

BoRo-specific market strategy, ICP and offers are seed/config data. The public engine cannot require BoRo concepts to function.

---

## ADR-007 — M0 supports exact reference reproduction and honest native recalculation

**Status:** Accepted

The source dataset includes derived component scores whose full raw prerequisites are not all present (notably implied population for the ICP-density formula). The engine therefore distinguishes exact reference reproduction from native raw-data recalculation.

It is prohibited to invent hidden raw inputs purely to force parity.

---

## ADR-008 — Company discovery starts in M2

**Status:** Accepted

M0/M1 must first create a reproducible market-intelligence foundation. Scraping/discovery before this layer is stable is explicitly deferred.
