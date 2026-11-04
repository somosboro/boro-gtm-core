# Shadow parity — VPS engine vs the ChatGPT automations, 2026-11

**Purpose.** Before cutover, compare what the engine *would* have done, day by
day, with what the ChatGPT automations actually did from the same mailbox.
Identical decisions are not the goal. The goal is to find where either side is
wrong, and to make sure the engine never makes a critical error:
* re-contacting someone already contacted;
* the wrong company or contact;
* a support mailbox;
* a fabricated signal;
* expired demand;
* a wrong follow-up date;
* a suppressed target.

**Method.** `gtm engine parity --day D --record`, run once Gmail history is
ingested, compares:
* the day's SHADOW first emails, including any later replaced by an adopted
  Gmail send (CE-ADR-019);
* the first emails of new outbound threads Gmail shows that day, which are
  ChatGPT's.

Critical mismatches are computed and recorded. Identity, contact and evidence
quality beyond the mechanical checks come from the READY audit
([READY_AUDIT_2026-11.md](READY_AUDIT_2026-11.md)). The cutover report needs
**two business days after the Gmail backfill with zero critical mismatches**.

## Status

| Day | Gmail ingested | Counts toward cutover | Result |
| --- | --- | --- | --- |
| 2026-11-03 (Tue) | no (OAuth pending) | no, before the backfill | Day 0: manual comparison below |
| 2026-11-04 | — | pending OAuth | — |
| 2026-11-05 | — | pending OAuth | — |

The two counted days can only start after Amaru completes the Gmail OAuth
step and the history backfill. They are **not complete**, so cutover is
**NOT READY**.

## Day 0 — 2026-11-03 (manual, before Gmail)

ChatGPT *BoRo Initial Outreach* sent 10 and failed 4, as reported by Amaru.
The VPS planned 21 SHADOW first emails for 09:19–13:06 Chile time, from the
Odoo Chile directory. One was blocked at send time as a support mailbox.

| Company | ChatGPT | ChatGPT contact | VPS (shadow) | VPS contact | Note |
| --- | --- | --- | --- | --- | --- |
| CM Corp | SENT | ventas@cmcorp.cl | planned | contacto@cmcorp.cl | Same company. Both are published general inboxes. |
| Blanco Martín & Asociados | SENT | odoocl@bmya.cl | planned | odoocl@bmya.cl | Same |
| ADHOC | SENT | tev@adhoc.inc | planned | tev@adhoc.inc | Same |
| BCN Consultores | SENT | jescalona@bcncons.com | planned | jescalona@bcncons.com | Same |
| BELUCKY | SENT | contacto@belucky.cl | planned | contacto@belucky.cl | Same |
| Giraffos | SENT | contacto@giraffos.com | planned | contacto@giraffos.com | Same |
| Tierra Nube | SENT | ventas@tierranube.cl | planned | ventas@tierranube.cl | Same |
| Alfa Data | SENT | (pool: contacto@alfadata.cl) | not chosen | — | The directory listing has no website. The VPS dropped it. **Fixed** (`8212ac1`): identity from the partner's published address. |
| Open Solutions | SENT | info@opens.lat | not chosen: BLOCKED `no_published_channel` | — | The listing's website is opens.cl and its address is @opens.lat, a sister domain. **Fixed**: sister domain accepted. |
| Blue Minds | SENT | grobles@blueminds.cl | not chosen | — | No website on the listing. **Fixed** (same as Alfa Data). |
| Konos | FAILED | ventas@konos.cl | planned | ventas@konos.cl | Same choice |
| Consulnet | FAILED | contacto@odoosistema.com | planned | contacto@consulnet.io | VPS better: the address is on the company's identity domain. odoosistema.com is a different name. |
| Dimabe | FAILED | contacto@dimabe.cl | planned | amunoz@dimabe.cl | VPS better: a named person published on the site |
| OpenDrive | FAILED | ventas@opendrive.cl | planned | contacto@opendrive.cl | Same company |
| Elite Solutions | not sent | soporte@elitesolutions.cl (pool) | **BLOCKED at send**: support mailbox | soporte@ | Correct block. The target was then requalified for a commercial channel. **Fixed** |
| 9 others (vedata, evoedge, indasoge, ingmetrica, iosbox, tpco, tichile, kpbsolutions, grupoloyalle) | — | — | planned | named people for 7 of 9 | VPS-only choices from the same directory |

**Reading.**
* **Agreement.** Of the 14 companies ChatGPT attempted, the VPS independently
  chose 11, with the same contact in 7 of them.
* **Contact differences.** Where the two disagreed, the VPS chose a named
  person or an address on the company's identity domain (Dimabe, Consulnet).
* **VPS defects found.** The three companies only ChatGPT chose exposed real
  VPS defects. Partners without a website were dropped, and a sister-domain
  address was refused. Both are fixed and tested. After the fix, 38 more
  directory partners were identified on the VPS.
* **ChatGPT pool defect.** The pool carried `soporte@elitesolutions.cl` as a
  READY contact. The VPS refused it.
* **Critical mismatches (day 0).** The VPS could not have known about the
  ChatGPT sends: Gmail is not connected. Had it been live *instead of* ChatGPT,
  nothing would have been sent twice. Had both been live, 7 companies would
  have received two first emails. This is exactly why the engine stays in
  shadow mode until the Gmail backfill. After the backfill, these 7 (and any
  others) are adopted as CONTACTED from Gmail, and their shadow rehearsals are
  replaced (CE-ADR-019).

## Days 1–2

To be filled by `gtm engine parity --day … --record` after the Gmail backfill.
Record each day's table and the critical count here.
