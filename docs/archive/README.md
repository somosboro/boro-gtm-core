# Historical commercial strategy (2026-09) — archived

These artifacts describe the strategy BoRo ran from mid-2026 until 2026-11:
HVAC and Energy Project Delivery as the ICP, operational-architecture
positioning, the Architecture Sprint as the entry offer, and pre-outreach
evidence gates.

| Artifact | Where it stays |
| --- | --- |
| Canonical commercial contract | `commercial/CANONICAL_CONTRACT.json` (and `boro_gtm.commercial.contract`) |
| Milestone ↔ sales-motion map (M4–M9) | `docs/GTM_MILESTONE_OWNERSHIP.md` |
| M3 canonical alignment | `docs/M3_CANONICAL_COMMERCIAL_ALIGNMENT.md` |

They are kept because they record what was tried and why, and because M0–M3
were built against them. They are **not active**: the commercial engine loads
only `commercial/strategies/active-*.json` (CE-ADR-010), and a test asserts the
active strategy does not reintroduce the Architecture Sprint as an entry offer.
