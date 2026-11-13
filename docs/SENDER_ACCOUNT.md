# Separate sender account: checklist (CE-ADR-022)

**Status: configured, not active.** Cold outreach still goes from the corporate
mailbox named in the strategy's `sender`. `GTM_ENGINE_SENDER_ACCOUNT` holds the
address to move to; `GTM_ENGINE_SENDER_ACCOUNT_ENABLED=true` makes the worker
refuse to send at all until the switch below is built and approved, so turning it
on never sends from the wrong mailbox by surprise.

Why: bounces and spam complaints from cold email count against the sending
domain's reputation. Client mail (proposals, invoices, support) should not share
that risk.

## 1. Domain and mailbox

- [ ] A sibling domain, not a subdomain of the client domain (example: `somosboro.co`
      or `boro-studio.com`), with a one-page site that links to somosboro.com.
- [ ] One Google Workspace mailbox on it for a real person (`amaru@…`), not `info@`.
- [ ] Replies forwarded or delegated so Amaru reads them in the usual inbox.

## 2. Authentication (all three before the first send)

- [ ] **SPF**: one TXT record `v=spf1 include:_spf.google.com ~all` (one SPF record only).
- [ ] **DKIM**: generate the 2048-bit key in Google Admin → Apps → Gmail →
      Authenticate email, publish the `google._domainkey` TXT, then press *Start
      authentication*.
- [ ] **DMARC**: TXT at `_dmarc` with `v=DMARC1; p=none; rua=mailto:dmarc@<domain>`;
      move to `p=quarantine` after two clean weeks.
- [ ] Check with a message to a Gmail address: *Show original* must say SPF, DKIM
      and DMARC **PASS**.
- [ ] Google Postmaster Tools set up for the domain.

## 3. Warm-up (about four weeks)

- [ ] Week 1: 5–10 real one-to-one emails a day to people who will answer.
- [ ] Week 2: 10–15 a day; no cold email yet.
- [ ] Week 3: first personalized cold emails, at most 5 a day, through the gate.
- [ ] Week 4 onward: raise only while bounces stay under 5% and Postmaster shows
      no spam-rate warning.

## 4. Engine switch (code, reviewed)

- [ ] A new OAuth connection for the new mailbox (`gtm engine gmail-connect`).
- [ ] A strategy version whose `sender` names the new address; the worker refuses
      credentials that open any other mailbox.
- [ ] Gmail history of the old mailbox stays the authority for "already contacted".
- [ ] Amaru approves; only then `GTM_ENGINE_SENDER_ACCOUNT_ENABLED=true` with the code
      that uses it.
