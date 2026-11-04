# Source coverage audit — 2026-11

**Question.** Can the engine sustain 20 *qualified, contactable,
allowed-jurisdiction* new targets per business day, at a 40% partner / 40%
explicit demand / 20% trigger mix, from sources it may lawfully automate?

**Rule applied (CE-ADR-021).** A source is automated only through a public API
or robots-allowed pages, with no login wall, CAPTCHA or anti-bot challenge.
It must also yield a company identity (the company's own domain) and a
legitimate channel. Anything else is a `MANUAL_SOURCE`, with its reason in
`engine/sources/manual.py`.

## Candidates audited (2026-11-03)

| Source | Lane | Access checked | Identity | Channel | Verdict |
| --- | --- | --- | --- | --- | --- |
| Odoo partner directory (14 LatAm + ES) | Partner | robots allows listing and partner pages | partner's own website (`itemprop=website`) | email the partner publishes in the directory, or its own site | **Automated** (6 → 14 countries) |
| Get on Board jobs API, freelance/project posts | Explicit | public API; robots `search=yes, ai-train=no` | company `web` field | company site | **Automated** (existing) |
| Get on Board jobs API, delivery firms hiring | Trigger | same API, `categories/*/jobs` | company `web` | company site | **Automated** (new `getonboard_hiring`) |
| Get on Board company directory | Partner | same API, `/companies` (≈17k profiles) | company `web` | company site | **Automated** (new `getonboard_companies`) |
| HN "Who is hiring?" via Algolia API | Trigger | public API built for this; no robots restriction | company site linked in the post, accepted only if the domain carries the company name | address the post publishes (role `contractor_recruiting`) | **Automated** (new `hn_who_is_hiring`) |
| HN "Seeking freelancer" thread | Explicit | discontinued (only "Who is hiring" and "Who wants to be hired" are posted now) | — | — | Not available |
| Upwork | Explicit | job search paths disallowed in robots; terms forbid scraping | anonymous client | proposal on the platform | **MANUAL_SOURCE** |
| Freelancer.com | Explicit | public `projects/active` API answers | owner hidden (`owner_id: null`) | bid on the platform | **MANUAL_SOURCE** (nothing to email) |
| Workana | Explicit | robots disallows `/api/` | anonymous buyer | proposal on the platform | **MANUAL_SOURCE** |
| LinkedIn jobs/posts | Trigger | login wall, anti-bot, user agreement | — | — | **MANUAL_SOURCE** |
| Indeed | Trigger | anti-bot, terms | — | — | **MANUAL_SOURCE** |
| Clutch | Partner | Cloudflare challenge (HTTP 403 even on robots.txt) | — | — | **MANUAL_SOURCE** |
| HubSpot / Shopify partner directories | Partner | client-rendered apps with no public listing API | — | — | **MANUAL_SOURCE** |
| Mercado Público (ChileCompra) API | Explicit | official API needs a free ticket (`Ticket no válido` without it) | public buyer | bid through the portal, not email | **MANUAL_SOURCE** until a ticket exists; even then it is a bidding queue, not email |
| Remotive / Jobicy / Himalayas remote-job APIs | Trigger | public APIs with attribution terms | no company domain in the payload (company name and an aggregator link only) | aggregator application | Not automated: identity would have to be guessed from names |

## What changed

* Four automated families now exist where there were two: a partner directory
  (Odoo, now 14 countries), a tech-company directory (Get on Board companies),
  a job-board demand source (Get on Board freelance/project) and two hiring
  trigger sources (Get on Board delivery firms, HN contract posts).
* Each lane has at least one automated source:
  * Partner: Odoo and the Get on Board company directory.
  * Explicit: Get on Board freelance and project posts.
  * Trigger: Get on Board delivery firms hiring, and HN contract posts.
* The ladder sources each lane toward its own share and reports any lane's
  shortfall with every source's counts. It never fills one lane from another.

## Measured on the VPS (2026-11-03)

| Source | Inspected | Accepted as targets | Main rejections | READY after enrichment |
| --- | --- | --- | --- | --- |
| Odoo CL / PE / CO / MX | 36 / 83 / 48 / 97 | 23 / 53 / 27 / 41 | no website on listing; 38 recovered from the partner's own published address | 26 / 52 / 31 / 98 |
| Get on Board freelance/project (explicit) | 179 (this pass) | 0 | 66 full-time employment, 6 not software, 4 expired, 103 already seen | 0 |
| Get on Board delivery firms hiring (trigger) | 50 | 13 | 13 not a delivery firm, 6 not software | 2 (9 had no published email) |
| HN "Who is hiring" (trigger) | 175 | 4 | 156 full-time, 10 not software, 5 no company site | 0 READY, 2 in COMPLIANCE_REVIEW (US, CA) |
| Get on Board company directory (partner) | 100 (1 of ~173 pages) | 9 | 79 not a delivery firm, 12 no description | 0 (7 no published email, 2 below threshold) |
| Week-1 pool (100 rows) | 100 | 10 created, 48 attached | 32 platform applications (20 Upwork, 12 LinkedIn/ATS), 10 no identity | 44 READY |

Where the engine loses targets is now measured, not guessed. The biggest loss
after sourcing is **no published email**: most company sites offer only a
form. The engine blocks those rather than guessing an address.

## Conclusion

Partner supply is ample for months: hundreds of Odoo partners across 14
countries, plus the Get on Board directory. Trigger supply is real but
moderate. **Explicit demand is the structural gap.** The demand is mostly
real, but most of it lives on marketplaces where the only channel is a
platform proposal, and the engine must not turn that into a guessed email.
The engine therefore keeps those as MANUAL applications (`gtm engine
manual`). It will record an explicit-lane shortage most days, rather than
lowering quality or letting Odoo partners fill the explicit share.
