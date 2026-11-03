# GTM Commercial Engine Audit — 2026-11

**Scope:** `somosboro/boro-gtm-core` at `ee711bd` (branch `feat/m2-live-discovery`),
audited before any change, then redesigned on `feat/commercial-engine`.
**Decision records:** [CE_ADRS.md](../CE_ADRS.md).

## 1. Repository state at the start

| | |
|---|---|
| Commit | `ee711bd41c606492961d02b4ceb62782ff81529f` (`main` is `fc76be4`; M2 live discovery is four commits ahead and unmerged) |
| Tests | **1272 passed, 0 failed, 0 skipped** (9 min 40 s) |
| Migrations | 0001–0010, SHA-256 manifest, `db check --strict` clean |
| Engine / package version | scoring `ENGINE_VERSION 0.2.0`, package `0.2.1` |
| Milestones built | M0 market scoring, M1 context, M2 company discovery (incl. Google Places), M3 operational evidence (incl. live first-party research) |
| Milestones planned, not built | M4 interpretation … M9 Architecture Sprint / commercialization |
| Deployment | `docker-compose.yml` for local dev only; nothing ran on the VPS |

Major invariants that held, and were kept: append-only evidence enforced by
database triggers; canonical company identity by registrable domain under the
Public Suffix List; deterministic resolution with an auditable decision chain;
immutable, provenance-linked evidence; robots, SSRF and politeness rules on
every live fetch; migrations that cannot be edited after recording.

## 2. Architectural strengths

* **Discipline at the database.** Invariants are constraints and triggers, not
  service conventions. This is why the engine could be added safely.
* **Identity.** M2's domain rules (`normalize_domain`, `domain_role`, the
  hosting-tenant and deep-path policy) are exactly what outreach deduplication
  needs, and are reused unchanged.
* **A safe, polite HTTP transport.** M3's production transport (SSRF checks at
  every hop, robots, per-host delays, login-wall detection) is reused for
  sourcing and enrichment.
* **A PostgreSQL job queue already existed** (M2-ADR-010). No broker is needed.
* **Test culture.** Reproduce-before-fix and adversarial tests caught real
  defects at every phase.

## 3. Obsolete assumptions

* **Research before distribution.** The roadmap made operational research
  (M3) and interpretation (M4) prerequisites for outreach (M5–M6), and placed
  qualification after a diagnostic call (M7–M8). That is coherent as an
  ontology and wrong as a business: three months produced 45 prospects, 78
  emails, 2 human replies and no revenue. The constraint was distribution.
* **A single ICP.** HVAC and Energy Project Delivery were hard-wired as *the*
  market. The new strategy explores three demand surfaces in parallel.
* **The Architecture Sprint as the entry offer.** It asked cold buyers to buy a
  diagnosis of a problem they had not said they had.
* **Pain inference as the hook.** Messages inferred internal pain from public
  evidence. The new lanes start from observable demand.

## 4. Missing capabilities (before this change)

No outreach, no Gmail integration, no follow-up scheduling, no reply handling,
no suppression list, no send idempotency, no daily caps, no commercial state
machine, no per-lane funnel, no worker running anywhere, no backups.

## 5. Technical debt found

* `docs/README.md` once claimed 63 M2 ADRs against 34 on disk (fixed earlier).
* `discovery plan-live`: `max_results` defaults to 1000 while the 25-metro plan
  returns ~2,400 records, and `first_page_coverage_possible` ignores it. Still
  open; recorded for the M2 merge.
* M3 extraction rules are fixture-shaped sentence templates (M3-ADR-078): live
  research yields few claims. Not touched here — it is no longer on the
  critical path.
* The `jobs` package is three empty modules.

## 6. Data-model gaps (closed)

Commercial entities had no home. Migration 0011 adds `ce_accounts`,
`ce_signals`, `ce_source_runs`, `ce_source_cursors`, `ce_contacts`,
`ce_targets`, `ce_target_events` (append-only), `ce_suppressions`,
`ce_messages`, `ce_mail_events`, `ce_deals`, `ce_problem_signals`,
`ce_controls`, `ce_worker_heartbeats`, `ce_task_runs`. Conversations,
opportunities and proposals are **states** of a target with a deal row for
money, not three more tables: a conversation that becomes an opportunity is
the same pursuit, and splitting it would invite rows that disagree.

## 7. Commercial-state gaps (closed)

An explicit state machine (DISCOVERED → ENRICH → READY → CONTACTED → F1 → F2 →
REPLIED → CONVERSATION → OPPORTUNITY → PROPOSAL → WON, with LOST, EXPIRED,
BLOCKED, SUPPRESSED, CLOSED_NO_REPLY and COMPLIANCE_REVIEW), illegal moves
refused in code, every move an append-only event, one open pursuit per account
enforced by a partial unique index.

## 8. Security and deployment risks

* **Shared VPS.** 2 vCPU / 8 GB already running Odoo (three tenants), ACOS,
  Qué Compro (prod + staging), BoRo OS and monitoring, with ~3.2 GB available.
  The engine runs as an isolated compose project with CPU/memory limits and
  uses ~240 MB.
* **Port 8000 is taken** by BoRo OS on `0.0.0.0`. The engine API binds
  `127.0.0.1:8110` only; no nginx site exposes it.
* **Backups on the host.** Only ACOS Odoo had a backup job. The engine now has
  nightly dumps and a tested restore — but on the same disk. Off-host copy is a
  blocker until a bucket exists.
* **Deliverability.** SPF includes Google and Hostinger, DKIM is published,
  DMARC is `p=none` (monitoring). Acceptable to start at 20/day; move DMARC to
  `quarantine` once reports look clean.
* **Secrets.** Generated on the VPS, mode 600, never printed or committed.
* **Personal mailbox.** The ATS job-application automation and personal Gmail
  are out of scope and untouched.

## 9. What is preserved

M0–M3 in full, unchanged: schema, services, tests, ADRs. M2 identity and M3's
transport are reused by the engine. Nothing was deleted.

## 10. What is deprecated

* `commercial/CANONICAL_CONTRACT.json` and the 2026-09 doctrine (HVAC/Energy
  ICP, Architecture Sprint entry offer, pre-outreach evidence gates) are
  **historical**. They stay in the repository and are never read by the engine
  ([archive note](../archive/README.md)).
* Milestones M4–M9 as a *prerequisite chain* for outreach. M3 research remains
  available, proportional to an account's value, never a gate.

## 11. Migration plan

1. Deploy in SHADOW_MODE (done): sourcing, enrichment, planning and rendering
   run; nothing is sent; targets do not advance.
2. Connect the corporate Gmail (human OAuth step), backfill history with
   `gtm engine ingest-history`, import the Week-1 pool if available.
3. Run shadow for at least two business days; compare planned first contacts
   with what the ChatGPT automations sent.
4. `gtm engine cutover-report` must pass every check.
5. Switch the ChatGPT commercial automations off, ingest once more, then set
   `GTM_ENGINE_SHADOW_MODE=false`.

## 12. Final architecture

```
sources (Get on Board API, Odoo partner directory, imports)
   │ ce_source_runs / ce_source_cursors (exhaustion recorded)
   ▼
ce_signals ──► ce_accounts (M2 domain identity) ──► ce_targets (state machine)
                                                       │  enrichment: published channel only,
                                                       │  100-pt score with evidence,
                                                       │  jurisdiction gate
                                                       ▼
                         planner (20/day, lane mix, spread) ──► ce_messages
                                                       │  SENDING committed before Gmail call,
                                                       │  fixed Message-ID, reconcile on restart
                                                       ▼
Gmail (corporate) ◄──────────── sender ───────────────┘
   │
   ▼ ingestion (idempotent) ──► ce_mail_events ──► rules: bounce / auto-reply / opt-out /
                                                   human; escalation terms; model optional
                                                       │
                       suppressions, follow-up cancel/postpone, REPLIED/LOST/SUPPRESSED
                                                       ▼
          Amaru: CONVERSATION → OPPORTUNITY → PROPOSAL → WON/LOST (+ ce_deals)
                                                       ▼
                    per-lane funnel, today, health, cutover report (CLI + API)
```

One worker process runs every task under a PostgreSQL advisory lock, writes a
heartbeat and a `ce_task_runs` row per execution. Runtime: FastAPI + worker +
PostgreSQL 16 in Docker Compose. No Redis, no broker, no model required.

## 13. Measured on the first live runs

* **Get on Board:** 458 postings inspected in one pass; 421 were full-time
  hires (rejected: employment, not vendor demand), 25 expired, 7 duplicates,
  5 accepted. Project-based demand on that board is thin. After the title gate,
  a "Growth Hacker" posting that had scored 87 is now rejected.
* **Odoo partner directory:** reachable from the VPS; from the development
  machine every Python client stalled (curl did not), so the source records
  itself BLOCKED rather than retrying around it.
* **Implication for the 20/day target:** two automated sources will not supply
  20 qualified, contactable, allowed-jurisdiction targets a day on their own.
  The ladder records exactly where it fell short. More sources are the next
  lever (see blockers).
