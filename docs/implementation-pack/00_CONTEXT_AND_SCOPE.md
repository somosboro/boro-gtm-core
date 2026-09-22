# 00 — Context and Scope

## Product thesis

OpenGTM is an evidence-driven GTM intelligence and orchestration engine.

The long-term pipeline is:

`Market Intelligence → Market/Vertical Selection → ICP Compilation → Account Discovery → Research → Evidence → Qualification → Buyer Discovery → Contact Enrichment → Messaging → Campaigns → Experiments → Replies → Opportunities → Revenue → Market Learning`

M0 and M1 build only the Market Intelligence foundation.

## BoRo Studio context

BoRo Studio sells operational architecture and Operations OS implementations to B2B companies with complex field-service / field-operations workflows.

Core problem pattern:

`request → planning → work order → dispatch → technician → materials/parts → evidence → approval → close → invoice → asset history`

Common fragmentation:
- ERP/accounting
- spreadsheets
- email
- messaging
- paper
- field-service tools
- undocumented employee knowledge

Initial target profile used to seed M1:
- B2B service/maintenance businesses
- roughly 20–150 employees
- roughly 10–75 field workers/technicians
- recurring service or maintenance
- sufficient operational complexity and purchasing power for $15k–$40k+ projects

Initial buyers:
- Owner / President
- CEO / General Manager
- COO / VP Operations
- VP Service / Service Manager
- Operations Manager
- Maintenance Manager

Initial commercial market portfolio from the supplied study:
- United States
- United Kingdom
- Germany
- Australia
- Canada
- France
- Spain
- Italy
- United Arab Emirates
- Poland

The research dataset concludes that market attractiveness is contextual. A single immutable `country.score` is not sufficient. The engine must eventually evaluate:

`market_score(country, vertical, ICP, channel, ticket/offer, data_snapshot)`

## M0 scope

M0 is complete when the system can ingest the supplied JSON snapshot, persist all relevant data with provenance, and recalculate the supplied base score values deterministically.

### Included
- DB schema + migrations
- snapshot import
- source provenance
- base market observations
- scoring model v1
- score run v1
- top-50 ranking + Chile benchmark + normalization-universe markets
- TAM/SAM/SOM where present
- base read APIs
- unit/integration tests
- local Docker workflow

### Excluded
- account/company discovery
- crawling company websites
- people discovery
- email enrichment
- messaging
- sending
- inbox/reply handling
- Odoo/CRM integration
- Bayesian market learning
- billing/auth SaaS concerns

## M1 scope

M1 adds context-aware market intelligence.

### Included
- vertical registry
- ICP registry
- offer registry
- channel registry
- market × vertical profiles
- contextual score request model
- score/confidence/coverage separation
- research-gap detection
- contextual ranking APIs
- seed BoRo strategy objects
- ability to compare the same vertical/ICP across markets

### Explicit M1 constraint
Do not fabricate vertical-specific market scores from missing data. Contextual score components must be either:
1. derived from stored observations,
2. supplied as explicit configured priors/weights,
3. marked unknown and reflected in coverage/confidence.

## M2 begins later
M2 starts with Company Discovery. Do not implement it in this milestone.
