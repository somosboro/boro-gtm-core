# GTM Technical Milestones ↔ Canonical Sales Motion

**Status:** adopted with M3 design revision 5. Supersedes the informal roadmap
in which M4 was called "Qualification".

---

## 1. Why the old roadmap had to change

The old sequence was M2 Company Discovery → M3 Operational Research →
**M4 Qualification** → M5 Buyer Discovery → M6 Outbound.

That is semantically incompatible with the canonical sales motion, for one
concrete reason: **canonical qualification is scored after a diagnostic call**,
and needs complexity, impact, sponsor, trigger and budget. None of those can
truthfully be known from public research before outreach.

Calling a pre-outreach inference layer "Qualification" would have given a name
— and therefore a set of expectations, field names and eventually scores — to
something that cannot be qualification. The layer that sits after operational
evidence is not qualification; it is **interpretation**.

## 2. The revised technical milestones

There is deliberately **not** one technical milestone per sales stage. A sales
stage is a human activity; a milestone is a system that holds state.

| Milestone | Owns | Status |
| --- | --- | --- |
| **M0** Market Intelligence | Markets, observations, provenance, scoring | Implemented |
| **M1** Contextual Market Intelligence | Market × vertical × ICP × channel context, research gaps | Implemented |
| **M2** Company Discovery + Entity Resolution | Canonical company identity, provider evidence, resolution | Implemented |
| **M3** **Operational Evidence** | Sources, immutable evidence, typed operational claims, gaps, coverage | Schema implemented; services pending |
| **M4** **Account Evidence Interpretation** | Canonical `EV-*` signal derivation, bounded hypotheses, outbound-safety classing, contradiction state | Not started |
| **M5** Buyer Discovery | People, roles, contactability | Not started |
| **M6** Outreach & Response | Sequences, replies, response capture | Not started |
| **M7** Commercial Qualification | The canonical six-dimension rubric, routing, disqualifiers | Not started |
| **M8** Diagnostic & Opportunity | Diagnostic capture, trigger, budget band, sponsor | Not started |
| **M9** Architecture & Commercialization | Architecture Sprint output, capability selection, commercial level, normalized economics, price | Not started |

**M3 was renamed** from "Operational Research" to "Operational Evidence". The
old name invited the reading that M3 *researches conclusions*; it observes and
records. The table names remain as built — no schema churn for a noun.

## 3. Mapping the 14 canonical sales stages

| # | Canonical stage | Technical milestone(s) | Note |
| --- | --- | --- | --- |
| 1 | `ACCOUNT_DISCOVERY` | M0, M1, M2 | ICP plausibility from market context and canonical identity |
| 2 | `EVIDENCE` | **M3**, then **M4** | M3 observes; M4 derives `EV-*` and hypotheses |
| 3 | `OUTREACH` | M5, M6 | M4 supplies outbound-safe evidence classes; M6 sends |
| 4 | `RESPONSE` | M6 | Reply capture |
| 5 | `QUALIFICATION` | **M7** | Only here does the canonical rubric become writable |
| 6 | `DIAGNOSTIC_CALL` | M8 | Where sponsor, trigger and budget band first legitimately exist |
| 7 | `ARCHITECTURE_SPRINT_SALE` | M9 | Commercial motion for the sprint |
| 8 | `ARCHITECTURE_SPRINT` | M9 | Produces approved architecture |
| 9 | `IMPLEMENTATION_PROPOSAL` | M9 | Capability selection and commercial level become legitimate |
| 10 | `OPERATIONS_OS_DELIVERY` | — | Delivery, not a GTM Core concern |
| 11 | `GO_LIVE` | — | Delivery |
| 12 | `HYPERCARE` | — | Delivery |
| 13 | `BORO_CARE` | — | Recurring service |
| 14 | `EXPANSION` | M1, M2, M3 re-entry | Expansion restarts the evidence loop against a known account |

Four stages map to no technical milestone, and that is the correct answer
rather than a gap. Delivery and care are run by people and a delivered system,
not by GTM Core. **Avoid milestone inflation**: inventing M10 "Go Live" would
create a system with nothing to hold.

## 4. The boundaries that matter

```
M3  observes          → may never conclude
M4  interprets        → may never qualify
M7  qualifies         → may never classify commercially
M9  architects        → only here may capabilities be selected
                        and a commercial level set
```

Each arrow is enforced by a test, not a convention:

* M3 carries no capability id, qualification dimension, classifier dimension or
  judgement-shaped attribute.
* Qualification's own canonical rubric records `when_scored: after a diagnostic
  call`, which is upstream of nothing M3 can see.
* The classifier is explicitly based on approved architecture.
