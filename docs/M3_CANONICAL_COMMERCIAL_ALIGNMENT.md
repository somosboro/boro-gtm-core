# M3 ↔ Canonical Commercial Ontology Alignment

**Status:** M3 design **revision 5**. Reconciliation only — no acquisition or
extraction service is implemented by this pass.

Revision 4 was frozen before the canonical commercial ontology
(**Operations OS Price Book v2.0**) was adopted. That ontology is a legitimate
implementation-discovered design input, so this revision reconciles GTM Core to
it. Revision 4's history is not rewritten.

---

## 1. Authority

```
Markdown Price Book  >  YAML machine companion  >  GTM implementation  >  historical GTM definitions
```

**Where GTM Core conflicts with the Price Book, GTM Core changes.** Commercial
policy is never bent to preserve an implementation. One test in this pass
already changed under that rule: it asserted the classifier's anti-rule using
the word "price", and the canonical wording is *"Never classify from contract
value alone"*. The canonical vocabulary won.

## 2. What is committed, and what is not

**Neither canonical file is in this repository, and that is deliberate.**

Both carry internal economics: the normalized gross-margin band and hard floor,
per-product price floors, the economic price-floor formula, discount-authority
thresholds and founder delivery economics. The Price Book states its margin
target *"is an internal management target, not a customer-facing claim"*. This
repository is **public**, and a commit is effectively irreversible — history,
forks, caches and search indexing.

What is committed is `commercial/CANONICAL_CONTRACT.json`: the ontology
**identifiers and shape**, generated from the canonical YAML by an explicit
allowlist.

| Committed | Not committed |
| --- | --- |
| 43 capability ids | Any price, floor or band |
| 18 evidence signal ids, names, strengths, supports | Gross-margin target and floor |
| 14 sales-motion stages, in order | The price-floor formula |
| 6 qualification dimension ids, score range, when scored | Discount authority thresholds |
| 12 classifier dimension ids, Core gates, anti-rule | Founder delivery economics |
| 25 Q1 field **names**, 7 Q2 field names | Any value for those fields |
| Outbound rules and feature-selling prevention | Product pricing of any kind |
| SHA-256 of all three canonical files | The files themselves |

Two guards keep it that way, both tested: an allowlist of reviewed top-level
keys, and a walk that rejects any economics-shaped key anywhere in the
document. Knowing a field is *called* `price_floor_usd` discloses nothing;
knowing it equals a number would.

**Canonical authority location:** held outside this repository by the document
owner. The contract records each file's SHA-256 so drift is detectable without
the file being present.

## 3. The central architectural rule

```
EVIDENCE → HYPOTHESIS → QUESTION → DIAGNOSTIC → ARCHITECTURE → IMPLEMENTATION DECISION
```

and **never**

```
EVIDENCE → PAIN CERTAINTY → FEATURE
```

Worked example, the one the canonical spec uses:

| Layer | Content | Owner |
| --- | --- | --- |
| Observation | A job posting states the president approves additional work | **M3** — a `approval_step` observation with actor, source and locator |
| Commercial signal | `EV-MANUAL-APPROVAL` | **M4** — derived, never stored as FACT |
| Hypothesis | "a decision-right dependency may exist" | **M4** — bounded, labelled |
| Forbidden | "build an approval queue" / `CAP-APPROVALS` selected | **Nobody before architecture** |

M3 may record that the posting says it. M3 may not conclude that it is a
problem, and no layer before the Architecture Sprint may conclude what to build
about it.

## 4. Three things that must never collapse

| | Example | Owner | Fact type |
| --- | --- | --- | --- |
| **A. Operational claim** | "The posting states the Service Manager coordinates dispatch, technicians, customer communication and reporting." | M3 | `FACT` — the posting says it |
| **B. Commercial evidence signal** | `EV-MULTI-HANDOFF` | M4 | **Not automatically FACT** even though its inputs are facts |
| **C. Hypothesis** | "the role may be absorbing system-boundary work" | M4 | `HYPOTHESIS`, always |

A is a quotation. B is an interpretation of A. C is a possibility raised by B.
They live in different rows, with different fact types, and B never inherits
A's certainty.

## 5. The 18-signal coverage matrix

`supports` is quoted from the canonical YAML. Note how often the canonical
spec **itself** names its output a hypothesis — that is the spec agreeing that
these are interpretations, not observations.

| # | EV signal | canonical `supports` | Coverage before | Coverage after | M3 primitives | Owner | Max fact type for the signal | Public evidence sufficient? |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | `EV-RECURRING-SERVICE` | operational_complexity, repeatable_workflows | **PARTIAL** | **DIRECT** | `recurring_service_contracts`, `preventive_maintenance`, `service_categories` | **M3-direct** | FACT | Yes — a company states its maintenance offering |
| 2 | `EV-FIELD-SCALE` | handoff_density, scheduling_complexity | **PARTIAL** | **DIRECT** | `technician_count`, `field_workforce_present`, `fleet_presence` | **M3-direct** | ESTIMATE (counts are usually approximate) | Yes |
| 3 | `EV-MULTI-HANDOFF` | fragmentation_risk | **NONE** | **PARTIAL** | `actor_responsibilities`, `handoff_observation`, `system_touchpoint` | **M4-derived** | INFERENCE | No — "risk" is an interpretation |
| 4 | `EV-FRAGMENTED-TOOLS` | integration_or_process_gap | **PARTIAL** | **PARTIAL** | `erp`, `field_service_management`, `dispatch_system`, `crm`, `system_touchpoint` | **M4-derived** | INFERENCE | No — observing several systems is not observing a gap |
| 5 | `EV-MANUAL-APPROVAL` | decision_right_bottleneck_**hypothesis** | **NONE** | **PARTIAL** | `approval_step`, `actor_responsibilities` | **M4-derived** | HYPOTHESIS (the spec says so) | No |
| 6 | `EV-DUPLICATE-ENTRY` | data_flow_gap | **NONE** | **PARTIAL** | `data_reentry_observation`, `system_touchpoint` | **M4-derived** | INFERENCE | Rarely — usually customer-confirmed |
| 7 | `EV-ASSET-HISTORY` | source_of_truth_gap | **PARTIAL** | **PARTIAL** | `asset_tracking`, `source_of_truth_observation` | **M4-derived** | INFERENCE | No |
| 8 | `EV-OWNER-BOTTLENECK` | decision_right_dependency | **NONE** | **PARTIAL** | `approval_step`, `actor_responsibilities` | **M4-derived** | INFERENCE | No — a title is not a dependency |
| 9 | `EV-QUOTE-DELAY` | field_to_commercial_handoff_gap | **NONE** | **PARTIAL** | `estimate_handoff`, `handoff_observation` | **M4-derived** | INFERENCE | No — delay is rarely public |
| 10 | `EV-MISSED-ADDITIONAL-WORK` | revenue_leakage_**hypothesis** | **NONE** | **PARTIAL** | `additional_work_process` | **M4-derived** | HYPOTHESIS (the spec says so) | No |
| 11 | `EV-FIELD-OFFICE-DISCONNECT` | operational_continuity_gap | **NONE** | **PARTIAL** | `field_finding_handoff`, `handoff_observation` | **M4-derived** | INFERENCE | No |
| 12 | `EV-BILLING-READINESS` | service_to_cash_delay | **NONE** | **PARTIAL** | `completion_to_billing_handoff`, `evidence_collection_method` | **M4-derived** | INFERENCE | No |
| 13 | `EV-MULTI-LOCATION` | standardization_complexity | **DIRECT** | **DIRECT** | `branch_count`, `service_area`, `operating_markets` | **M3-direct** | FACT | Yes — a locations page |
| 14 | `EV-GROWTH` | change_trigger | **PARTIAL** | **PARTIAL** | `branch_expansion`, `hiring_signal`, `acquisition` | **M4-derived** | PROXY | Partly — events are public, "growth" is the reading |
| 15 | `EV-SYSTEM-MIGRATION` | architecture_trigger | **DIRECT** | **DIRECT** | `system_migration` | **M3-direct** | FACT when announced | Yes when announced |
| 16 | `EV-JOB-POSTING` | capacity_or_process_pressure_**hypothesis** | **DIRECT** | **DIRECT** | `hiring_signal`, `hiring_field_roles`, `field_roles` | **M3-direct** for the posting; **M4** for the pressure reading | FACT that it was posted; HYPOTHESIS for what it implies | Yes for the posting only |
| 17 | `EV-24-7` | exception_and_dispatch_complexity | **DIRECT** | **DIRECT** | `emergency_service` | **M3-direct** | FACT | Yes |
| 18 | `EV-FABRICATION` | cross_domain_handoff_complexity | **NONE** | **DIRECT** | `fabrication_operation_present`, `service_categories` | **M3-direct** for the operation; **M4** for the complexity reading | FACT | Yes |

**Before:** 4 DIRECT · 5 PARTIAL · 9 NONE.
**After:** 7 DIRECT · 11 PARTIAL · 0 NONE.

No signal is `NONE` any longer: every canonical signal now has at least one M3
primitive that can support its derivation. Eleven remain `PARTIAL` **on
purpose** — the missing part is not an observation M3 failed to model, it is
the interpretation M4 owns.

> These three numbers were wrong when first written — the prose said
> 5 · 5 · 8 while the table above it said 4 · 5 · 9. A test now counts the
> table's own rows, because a summary nobody recomputes is a summary that
> drifts. See `tests/unit/test_canonical_alignment_docs.py`.

### Rules that apply to every row

* **Negative evidence.** Absence of a primitive never produces a negative
  signal. No `handoff_observation` means we have not observed one, not that
  handoffs do not exist. This is M0's `NOT_AVAILABLE` rule, unchanged.
* **Contradiction.** Conflicting primitives leave the derived signal
  **contested**, never silently resolved.
* **Staleness.** A derived signal is no fresher than its stalest supporting
  claim.
* **Provenance.** A derived signal must carry the claim ids and evidence item
  ids it came from, or it is not a signal — it is an opinion.

## 6. The eleven new observable primitives

All are `PROCESS_OBSERVATION`, all optional, all `SET`/`JSON`, all
`FACT`/`PROXY` only.

| Primitive | Records | Why this is an observation, not a judgement |
| --- | --- | --- |
| `actor_responsibilities` | A role and the duties a source attributes to it | Quotes the posting; draws no conclusion about load |
| `handoff_observation` | A described transfer of work between roles, teams or systems | "Work passes from X to Y" is describable; "fragmented" is not |
| `approval_step` | A described approval and who performs it | An approval existing is a fact; it being a bottleneck is not |
| `system_touchpoint` | A system named as used at a described step | Names the system; asserts nothing about integration |
| `data_reentry_observation` | The same information described as entered more than once | Describes re-entry; does not call it waste |
| `field_finding_handoff` | The described path from a field finding to the office | A path, not a verdict on the path |
| `estimate_handoff` | The described path from finding to quote | Same |
| `completion_to_billing_handoff` | The described path from completion to invoice | Same |
| `source_of_truth_observation` | Where a source says a record of truth lives | Location, not adequacy |
| `additional_work_process` | The described process for work found beyond the order | Process, not leakage |
| `fabrication_operation_present` | Whether in-house fabrication is described | A capability the company states it has |

Deliberately **not** added, because each is a verdict wearing an attribute's
clothes: `pain_score`, `fragmentation_score`, `owner_bottleneck`,
`needs_automation`, `bad_process`, `digital_maturity`.

All eleven are **optional**, so coverage's denominator is unchanged and adding
them cannot make an existing plan's coverage drop.

## 7. The capability firewall

The 43 canonical capabilities describe **potential Architecture Sprint scope**.
They are selectable building blocks chosen *after* architecture, not a checklist
M3 detects as "needed".

M3 may observe facts about work orders, approvals, billing readiness, asset
history and integrations. M3 may **not** conclude `CAP-APPROVALS` is selected,
`CAP-INTEGRATIONS` is required, or `CAP-CUSTOM-APP` is needed. A test asserts
no M3 attribute key is or begins with a capability id.

## 8. The qualification firewall

Canonical qualification is six 0–2 dimensions scored **after a diagnostic
call** — the YAML says so in `rubric.when_scored`, and the contract records it.

Public research cannot know complexity in the canonical sense, executive
sponsorship, budget fit or economic consequence. M3 therefore may **never**
write `qualification_score`, `qualification_route`, or any canonical
qualification dimension.

What public research *may* produce: a **candidate trigger**, scale and
complexity **indicators**, and research hypotheses. None of these is
qualification, and none may be stored under a qualification field name.

Specifically forbidden inferences:

* an executive sponsor from a job title alone;
* a budget from company size;
* an economic consequence from an observed process.

## 9. The classifier firewall

The 12-dimension Core / Scale / Transformation classifier is explicitly based
on **approved architecture**. So no milestone before the Architecture Sprint
may set `commercial_level_final`.

`commercial_level_candidate` is more subtle. The earliest legitimate point is
**after the diagnostic call**, when scope and complexity are discussed with the
prospect — not from public evidence. Before that it is a guess wearing a field
name, and the canonical anti-rule already forbids the related error:
*"Never classify from contract value alone."*

Price never flows backward:

```
scope/complexity → classification → normalized economics → price
```

never

```
budget → desired price → package class
```

## 10. Q2 evidence object — a projection, not a second store

Canonical Q2 has seven fields: `source`, `source_type`, `date_observed`,
`fact`, `confidence`, `inference_allowed`, `related_hypothesis`.

M3's ledger is far richer: source → fetch event → body → artifact derivation →
text derivation → extraction → evidence item → claim. **Q2 is a read-only
projection over that ledger**, not a parallel evidence database, and M3's
provenance is not downgraded to fit seven fields.

| Q2 field | Derived from |
| --- | --- |
| `source` | `research_sources.normalized_locator` via the evidence item |
| `source_type` | `claim_evidence_links.source_class` |
| `date_observed` | `company_claims.observed_at`, or the artifact derivation's `source_published_at`; **never** `retrieved_at` |
| `fact` | the claim's value, rendered |
| `confidence` | `company_claims.confidence` |
| `inference_allowed` | derived from fact type and the outbound policy (§11) |
| `related_hypothesis` | the M4 hypothesis id, `NULL` until one exists |

The canonical rule attached to Q2 — *"Never store an inference as if it were a
fact"* — is already M3's `fact_type` discipline, so the projection carries it
rather than re-implementing it.

## 11. Outbound-safe evidence policy

Outbound may state observed evidence and a bounded hypothesis. It may not
diagnose or prescribe features. M3 therefore exposes an evidence **policy
output**, not message copy:

| Class | Condition | Outbound use |
| --- | --- | --- |
| `STATE_DIRECTLY` | `FACT`, public or customer-provided, not stale, not contradicted | May be stated as observed |
| `HYPOTHESIS_ONLY` | `PROXY`, `ESTIMATE` or `INFERENCE`, or a derived signal | May be raised as a possibility |
| `TOO_WEAK` | `HYPOTHESIS`, or below the review threshold | Not usable outbound |
| `CONTRADICTED` | contested by another lineage | Not usable until resolved |
| `STALE` | past the attribute's horizon | Not usable as current |

M3 computes the class. It does not write sentences.

## 12. Schema impact

**Migration `0004_m3` is unchanged.** The reconciliation needed registry and
seed changes only: the eleven primitives are `attribute_definitions` rows under
registry version `M3-1.0`, and `attribute_definitions` already exists with
`owner_milestone`.

No new migration was created, because no persisted structure changed. The
commercial contract is a checked-in JSON file, not a table — GTM Core validates
against it, it does not store it.
