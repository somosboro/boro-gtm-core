# READY audit — 2026-11-03

Sampled with `gtm engine audit-sample` (stratified by lane and source).
For every target, the contact's source page and the signal page were
re-fetched from the VPS (`contact_on_source_page`, `domain_on_signal_page`),
and the rendered first email was read. Thresholds: `cutover.AUDIT_THRESHOLDS`.

## Sample 1 (seed 11): found defects, fixed before sample 2

| Defect | Count | Fix |
| --- | --- | --- |
| Company name carried the directory's level badge and raw HTML entities into subject and body ("C&amp;M Consultoría … Ready") | 9/30 | `clean_partner_name`; 342 stored names repaired |
| Every partner described as a generic "Odoo Partner" (the level badge was not read) | 26/30 | Badge becomes the level ("Ready/Silver/Gold Partner"); evidence repaired |
| Service or finance desk chosen as the channel (`servicio@`, `cfo@`, `admon@`) | 3/30 | Added to role mailboxes. Requalification ran on all READY targets: 10 requalified; 3 found a commercial channel, 7 BLOCKED (no guess) |
| Second account for one company: pool address `contacto@odoosistema.com` vs directory `consulnet.io` | 1/30 | A known company keeps its identity on import; duplicate BLOCKED |

## Sample 2 (seed 23): after the fixes (recorded for the cutover report)

Composition: 28 partner (Odoo CL/CO/MX/PE + 1 pool row), 2 trigger
(Get on Board delivery firms hiring), 0 explicit. This mirrors the READY pool:
explicit demand has no emailable supply today (see SOURCE_COVERAGE).

| Check | Result | Threshold |
| --- | --- | --- |
| Account identity correct | 30/30 | ≥95% |
| Domain correct | 30/30 | ≥95% |
| Signal correct and not misleading | 30/30 | ≥95% |
| Lane correct | 30/30 | ≥95% |
| Source correct | 30/30 | 100% |
| Score components defensible | 29/30 | ≥90% |
| Contact belongs to the company (address shown on its cited page) | 30/30 | ≥95% |
| Channel appropriate | 30/30 | ≥90% |
| No support mailbox | 30/30 | 100% |
| **No Gmail duplicate** | **28/30** | 100%: **FAILS** |
| No suppression | 30/30 (vacuous: suppressions load with the Gmail backfill) | 100% |
| Message reflects actual evidence | 29/30 | ≥95% |
| No invented pain | 30/30 | 100% |

**Defects remaining.**
* **`cmcorp.cl` and `adhoc.inc`.** The ChatGPT automation really sent to both
  today, and the engine cannot know that before the Gmail backfill. After the
  backfill both are adopted as CONTACTED (CE-ADR-019), and their READY rows
  disappear. This is the reason the audit **fails today**, by design.
* **LeytonMedia** (trigger). Its delivery-firm evidence is the weak phrase
  "transformación digital".
* **Alfa Data.** The level "Silver" comes from the pool's notes; the engine
  did not read it on a directory page.

**Verdict.** Identity, contact and message precision pass. The audit as a whole
does not pass until the Gmail backfill. Repeat it after the backfill:
`E audit-sample --n 30 --seed <new>`, then `E record-audit`.
