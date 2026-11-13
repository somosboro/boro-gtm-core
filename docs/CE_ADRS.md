# Commercial Engine — Architecture Decision Records

Decisions behind `boro_gtm.engine` and migration 0011. Context and audit:
[audits/GTM_COMMERCIAL_ENGINE_AUDIT_2026-11.md](audits/GTM_COMMERCIAL_ENGINE_AUDIT_2026-11.md).

---

## CE-ADR-001 — PostgreSQL is the single source of commercial truth

**Status:** accepted

**Context.** Commercial state lived in spreadsheets, chat history and four
ChatGPT automations. Nobody could answer "has BoRo already written to this
company?" from one place.

**Decision.** Every commercial fact that decides whether BoRo may write to
someone — target state, contact provenance, suppression, every planned and sent
message, every ingested Gmail message — is a row in the engine's PostgreSQL
database (`ce_*`). Spreadsheets are import formats, never state. Gmail is the
authority for *what was sent*; it is ingested, and the engine checks it before
any first contact.

**Consequences.** One query answers any operational question. The VPS database
must be backed up and restore-tested like production, because it is.

---

## CE-ADR-002 — Accounts reuse M2 identity rules, not M2's provenance chain

**Status:** accepted

**Context.** M2 resolves companies through providers, runs, record versions and
resolution decisions. A Get on Board posting or a row in an imported pool is not
a provider record.

**Decision.** `ce_accounts` is keyed by the registrable domain under M2's rules
(`normalize_domain`, `domain_role == IDENTITY`). Hosting tenants, platforms and
directories cannot be accounts. An account points at an M2 company when one
holds that identity domain; it does not have to.

**Consequences.** "Same company" means the same thing in discovery and outreach
without forcing every job post through provider-version machinery.

---

## CE-ADR-003 — An explicit, deterministic state machine

**Status:** accepted

**Decision.** `engine/states.py` defines every state and every legal transition.
`transition()` refuses anything else and writes an append-only
`ce_target_events` row (database trigger). One open pursuit per account is a
partial unique index. Terminal states are terminal; a *new material signal*
opens a new target linked by `reopened_from_id`, never a transition back.
Past REPLIED, only a person moves a target (CONVERSATION, OPPORTUNITY, PROPOSAL,
WON, LOST).

**Consequences.** No model and no worker can move a deal. A rejection blocks a
reopen for 180 days.

---

## CE-ADR-004 — Exactly-once sending

**Status:** accepted

**Decision.** `UNIQUE (target_id, step)` makes a second INITIAL, F1 or F2
unrepresentable. Each message gets a deterministic RFC 822 Message-ID at
planning time. Sending commits `SENDING` (with `claimed_at`) *before* the Gmail
call, and first asks Gmail whether that Message-ID already exists. A worker that
dies mid-send leaves a SENDING row; the reconciler resolves it by searching
Gmail for the Message-ID: found → SENT, not found → rescheduled, three attempts
→ FAILED. Due messages are claimed with `FOR UPDATE SKIP LOCKED`.

**Consequences.** A restart can delay a message; it cannot duplicate one.

---

## CE-ADR-005 — A practical 100-point qualification score

**Status:** accepted

**Decision.** Signal 25, urgency 20, technical fit 20, vendor suitability 15,
channel access 10, economic signal 10; ≥70 enters outreach, ≥85 is high
priority. Every component keeps the evidence it was awarded for. Terms match on
word boundaries; a job-board posting must have a software-delivery title to
count as explicit demand. Enrichment uses only addresses the company published
on its own domain, with the page recorded; nothing is guessed.

**Consequences.** Scores are arguable line by line. Deep research is not a gate.

---

## CE-ADR-006 — Follow-up dates are computed in America/Santiago business days

**Status:** accepted

**Decision.** Partner and trigger lanes: F1 after 4 business days; F2 at least
14 calendar days after the initial and 7 after F1; no reply 7 days after F2 →
CLOSED_NO_REPLY. Explicit demand: F1 after 2 business days; F2 5–7 days after
the initial; a follow-up that would land after the opportunity's deadline moves
before it or is dropped, and a passed deadline → EXPIRED. Business hours
09:15–17:30; Chilean holidays are configuration. An out-of-office reply with a
return date moves pending follow-ups past it. Any human reply cancels them.

---

## CE-ADR-007 — Gmail over its REST API, ingested idempotently

**Status:** accepted

**Decision.** The corporate sender `abourgeoisroa@somosboro.com` only. Four REST
calls over httpx (no SDK). OAuth refresh token in a mode-600 file outside the
repository. Every Gmail message is stored once (`UNIQUE gmail_message_id`) with
selected headers and a snippet, never full bodies. Replies are matched by thread,
then In-Reply-To/References, then the contact's address.

---

## CE-ADR-008 — Rules decide; models may suggest

**Status:** accepted

**Decision.** Bounces, auto-replies and opt-outs are recognized by rules,
because each one suppresses an address or moves a date. Escalation (price,
discount, scope, dates, SLA, exclusivity, IP, legal, capacity, subcontracting,
negotiation) is keyword-driven and flags the reply for Amaru. A model
(`GTM_ENGINE_LLM=anthropic`, off by default) may suggest a label for an
otherwise unlabelled human reply; its answer is validated against a closed list
and can never produce a suppression, a state, a date or a cap.

---

## CE-ADR-009 — Persistent sources with recorded exhaustion

**Status:** accepted

**Decision.** Each source is an adapter returning pages, a cursor and whether it
is exhausted. `run_source` records queries, pages, inspected, duplicates,
expired, rejected (with reasons) and accepted. The ladder runs until the daily
target is met or every source is exhausted or blocked, and states which. A
source whose every request fails is BLOCKED and rests a week; it is never
worked around. Sources at launch: Get on Board's public API (project/freelance
postings), Odoo's official partner directory (CL, PE, CO, MX, AR, ES), and
CSV/XLSX imports. LinkedIn, Indeed and Upwork are not scraped: login walls and
anti-bot measures are respected.

---

## CE-ADR-010 — One versioned active strategy

**Status:** accepted

**Decision.** Runtime reads `commercial/strategies/active-2026-11.3.json`
(version `2026-11.3`, from 2026-11-13; `2026-11.1` and `2026-11.2` are kept as
`superseded-*.json`): sender, caps, lane mix, cadences, jurisdictions,
templates, proof points. Every target and message records the strategy version.
The 2026-09 doctrine is archived and never loaded.

---

## CE-ADR-011 — Visibility through queries, not a dashboard

**Status:** accepted

**Decision.** `gtm engine summary | funnel | ready | sends | followups | replies
| opportunities | health | failed | sources` and the matching API routes are
all queries over durable rows.

---

## CE-ADR-012 — Lanes are measured separately

**Status:** accepted

**Decision.** Every message records lane, segment, country, variant, offer and
strategy version. Funnels are reported per lane (or country/variant/segment),
never pooled. Review checkpoints at ~100, ~200 and ~400 contacts per lane.

---

## CE-ADR-013 — One scheduler loop, advisory-locked tasks

**Status:** accepted

**Decision.** `gtm engine worker` loops over tasks (ingest, reconcile, guard,
source, enrich, plan, send, close) with minimum intervals. Each task holds a
session-level advisory lock on a dedicated connection, so any number of workers
is safe. Each run is a `ce_task_runs` row; each loop writes a heartbeat.

---

## CE-ADR-014 — The engine API is closed by default

**Status:** accepted

**Decision.** `/api/v1/engine/*` requires a bearer token; with none configured
it answers 503. On the VPS it listens on 127.0.0.1 only.

---

## CE-ADR-015 — Shadow mode until a cutover report passes

**Status:** accepted

**Decision.** `GTM_ENGINE_SHADOW_MODE` defaults to true; a missing variable never
means live. In shadow mode messages are rendered and stored as SHADOW, nothing
is sent and targets do not advance. `gtm engine cutover-report` checks Gmail
credentials, history depth, that no planned first contact is already in Gmail,
that no suppressed contact is READY, business-hours scheduling, nothing stuck
SENDING, that a shadow rehearsal ran, a live worker, and one initial per target.

---

## CE-ADR-016 — An isolated compose project on the existing VPS

**Status:** accepted

**Decision.** `/opt/boro_gtm`, project `boro_gtm`, network
`boro_gtm_internal`, volume `boro_gtm_pgdata`, its own PostgreSQL 16. Limits:
db 0.5 CPU / 512 MB, api and worker 0.5 CPU / 384 MB each. Image built on the
host at low priority. Nightly `pg_dump` (14 days), a restore test that loads the
latest dump into a scratch database and compares counts, and an optional
off-host copy via rclone once a bucket exists.

**Consequences.** Other workloads are untouched. Until an off-host target is
configured, a lost disk loses the commercial database.

---

## CE-ADR-017 — The deliverability guard pauses sending

**Status:** accepted

**Decision.** Over the last 40 real sends (after at least 10), a bounce rate
above 5% sets a durable pause; sending resumes only when a person resumes it.
Sends are spaced at least 12 minutes apart inside business hours.

---

## CE-ADR-018 — Cutover is earned, not configured; off-host backup blocks live sending

**Status:** accepted (2026-11 hardening sprint; supersedes the check list in CE-ADR-015)

**Context.** The first cutover report could pass on a credentials file, a
large-enough history count and one shadow message. The ChatGPT automations
were still sending from the same mailbox.

**Decision.** `gtm engine cutover-report` returns `ready_for_live: true` only
when every blocking check is met by recorded fact:

* Gmail read successfully within the last hour with a mode-600 credentials file;
* a *complete* history backfill reaching back to 2026-06-01, followed by a
  history reconciliation; nothing stuck SENDING or FAILED;
* no READY target already contacted, suppressed or on a role mailbox, and one
  first email per target;
* SHADOW first emails on at least two business days *after* the backfill;
* a recorded READY audit of at least 30 targets meeting the thresholds in
  `cutover.AUDIT_THRESHOLDS` (identity, domain, signal, lane ≥95%; contact ≥95%;
  channel ≥90%; support mailbox, Gmail duplicate, suppression, invented pain: 100%);
* shadow parity recorded for at least two business days with no critical mismatch;
* the history backfill loaded suppressions; no suppressed contact READY;
* `gtm engine verify` passed within 7 days (bounce, opt-out and follow-up
  dates on the production database, rolled back);
* both workers healthy, a recent completed source run, and each worker
  restarted at least once with a successful task after the restart;
* a backup within 26 hours, a restore test within 8 days (both written to
  `ce_controls` by the scripts), schema equal to the migrations, a valid
  active strategy;
* **an off-host copy of the latest backup.**

**Off-host backup decision.** It blocks real sending, not shadow operation.
Before live, Gmail can rebuild who was contacted. After live, the database also
holds what Gmail cannot rebuild: manual suppressions, lanes and scores behind
every send, human pipeline moves and deal values. A free encrypted copy on the
existing Google Workspace Drive takes about ten minutes, so the protection is
cheap and losing the disk unprotected is not.

---

## CE-ADR-019 — Gmail history is adopted, not just used as a block

**Status:** accepted

**Context.** Until cutover the ChatGPT automations send first emails and
follow-ups from the corporate mailbox. Blocking every company Gmail shows as
contacted would prevent duplicates, but at cutover every running ChatGPT
sequence would lose its follow-ups.

**Decision.** Only Gmail messages labelled SENT are outreach (drafts never are).
When an open, not-yet-contacted target's company appears in Gmail's outbound
history within 35 days, the engine adopts that thread: the first email (and up
to two follow-ups) become SENT messages on the target with variant
`external_gmail`, replacing any shadow rehearsal (kept in the transition's
details), the state follows, replies already in the thread take effect, and the
next follow-up is scheduled on the lane's cadence. Older threads block
re-contact (`gmail_history_sequence_finished`). A follow-up someone else sends
in an adopted thread is recorded as that step. Companies with a recent external
sequence but no engine pursuit are reported, never invented.

---

## CE-ADR-020 — The lane mix is protected from the easiest source

**Status:** accepted

**Decision.** The planner fills each lane's share of the day (40/40/20). A
short lane's share may go to other lanes' best READY targets, but no lane takes
more than 60% of the day's first emails (`outreach.MAX_LANE_SHARE`). The rest
is reported as `lane_shortage` / `unfilled_slots`, never filled by lowering the
threshold. Sourcing likewise runs each lane's sources toward that lane's share
and reports per-lane shortage with every source's counts and cursor.

---

## CE-ADR-021 — Sources are audited before they are automated

**Status:** accepted

**Decision.** A source is automated only through a public API or pages its
robots.txt allows, without login walls, CAPTCHAs or anti-bot challenges, and
only when it yields a company identity (its own domain) and a legitimate
channel. Everything else is a `MANUAL_SOURCE` with a written reason and
workflow (`engine/sources/manual.py`), listed in every ladder report.
Platform-only channels (Upwork, LinkedIn, ATS forms) are kept as MANUAL
applications, never turned into guessed email addresses. Historic contractor
usage is labelled `HISTORIC_SIGNAL`, never current explicit demand.

---

## CE-ADR-022 — A first email needs a researched, approved brief; one lane until February

**Status:** accepted (2026-11-13, Amaru). Supersedes the 40/40/20 mix of CE-ADR-020
and the template first email of CE-ADR-010 while `personalization.required` is on.

**Context.** In the mailbox, the emails that got replies named a person, cited a
checkable fact and asked a specific question. The campaign to info@/contacto@
produced at least 10 bounces and no reply, and emails to Mexico promised CFDI,
which BoRo has not delivered.

**Decision.**

* A first email exists only for an APPROVED `ce_research_briefs` row: a named
  person and role, an address on the company's own domain whose source page is on
  that domain (never a generic mailbox, MX checked), a fact under 90 days old with
  its source, a hypothesis phrased as a doubt, one question about past behaviour
  (never "¿le interesaría…?"), and why now. `engine/personalization_gate.py` checks
  all of it deterministically; a failing brief stays NEEDS_RESEARCH with reasons.
* The model drafts only from the brief (`research.build_prompt`); prompt, model,
  text and the gate's verdict are kept in `ce_draft_generations`. A draft has at
  most 120 words, an opt-out and no attachment, and claims only what
  `config/capabilities.yaml` allows and cites only `config/proof_cases.yaml`
  (written permission). CFDI, SUNAT, DIAN and Verifactu are never claimed. Every
  message is checked again for these claims at send time.
* Amaru approves the first 20 drafts of each batch and any draft with warnings.
* 5 first emails per business day, limited by approved briefs. One active lane,
  `es_odoo_partners` (Spanish-speaking Odoo partners in Latin America, from the
  Odoo directory), until 2027-02-06; the other lanes are paused.
* ES, the EU, the UK and Canada never get automatic email: their briefs become
  MANUAL_OUTREACH tasks exported to CSV; a person records sent/accepted/replied.
* "No contactar", "no contacten", "baja" and "unsubscribe" are opt-outs and
  suppress the sender and, unless it is a freemail host, the domain.
* `ce_score_events` feeds a weekly scoreboard (`gtm engine scoreboard`,
  `GET /engine/scoreboard`) with three gates: by 2026-12-12 at least 10
  conversations and 3 named projects or capacity gaps, or the predefined exit is
  option E (a system for clinics on Odoo), which Amaru decides; 150 delivered with
  no positive reply raises "abandonar canal"; reply rate per batch.
* A separate sender mailbox is configuration only (`docs/SENDER_ACCOUNT.md`).

**Consequences.** Volume drops to what research supports. Migration 0012 seeds a
NEEDS_RESEARCH brief for every READY target (their state stays READY; the planner
needs an approved brief). Claims the gate cannot recognise as a blocked term are
caught by the approval step, not by code.
