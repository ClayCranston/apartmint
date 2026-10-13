# Real-tour readiness and approval

Version 0.4.0 adds durable proposals and an approval gate. It does **not** register
a live booking provider. The CLI can prepare and approve a proposal and show the
official provider link for manual completion. `submit` and `reconcile` deliberately
fail with an actionable message until a real adapter exists; approval is not a
reservation. The synthetic acceptance commands never contact a property.

## Information needed for a real test

1. City/area, budget, bedroom count, and other required filters; or an existing
   listing URL. The engine remains city-agnostic. These inputs select a real test.
2. Acceptable dates/time windows, timezone, and guided, video, or self-guided tour
   preference. The chosen time must be offered by the property.
3. Genuine name, email, and phone that the selected property may receive. Supply
   a private local JSON profile or the path and names of three environment-file
   fields; never put real contact details in tests or packaged examples.
4. Required screening answers and any provider login/SMS/identity verification.
   Complete identity documents and account verification in the provider UI; those
   do not belong in model prompts.
5. Review of the exact property/unit, offered time and timezone, tour type,
   recipient, contact profile, and material terms before the external submission.

Local inference does not mean offline booking. The collector and provider adapter
need network access; an actual appointment sends the approved contact fields to
the selected provider. The local model should not receive credentials, contact
profiles, or identity documents. Deleting software does not cancel a real tour.

## Prepare and review locally

```bash
.venv/bin/apartmint tour requirements
cp config/tour-contact.example.json data/tour-contact.json
chmod 600 data/tour-contact.json
# Edit the private copy with real details; do not use the fictional example.
# Prepare data/tour-offer.json using a verified property and a live offered slot.
.venv/bin/apartmint tour prepare \
  --offer data/tour-offer.json --contact data/tour-contact.json \
  --expires-at 'YYYY-MM-DDTHH:MM:SS-07:00'
.venv/bin/apartmint tour show PROPOSAL_ID
.venv/bin/apartmint tour approve PROPOSAL_ID --digest EXACT_REVIEWED_DIGEST
.venv/bin/apartmint tour handoff PROPOSAL_ID
```

The timestamp above is a placeholder. Expiry must be in the future and no later
than the offered appointment. The offer's IANA timezone and explicit UTC-offset
timestamps are required. `config/tour-offer.example.json` describes the fields;
its placeholder property and future date are not an actual offer.

Alternatively, read only three named assignments from a local private environment
file without executing it:

```bash
.venv/bin/apartmint tour prepare --offer data/tour-offer.json \
  --contact-env /absolute/path/to/private.env \
  --name-var TOUR_NAME --email-var TOUR_EMAIL --phone-var TOUR_PHONE \
  --expires-at 'YYYY-MM-DDTHH:MM:SS-07:00'
```

Only literal scalar values are accepted; quote names containing spaces. Shell
commands, substitutions, variable expansion, duplicate assignments, symlinks,
and group/world-readable contact files are rejected. Unselected variables are
not included in the contact profile. Values never appear in status or errors.

The proposal binds the contact profile's digest. Inspect the private profile
locally before approving; public status displays its digest instead of the
values. The SQLite booking ledger and profile must remain on private local
storage. The ledger directory is 0700 and database 0600; this is filesystem
access control, not encryption or isolation from the same OS user/administrator.

`handoff` prints the scheduling URL and proposal; it neither opens a browser nor
fills/submits a form. The provider may require extra information or offer a
different time. Review those changes in its interface; the stored approval does
not approve altered details. Manual completion outside Apartmint does not inherit
the automated adapter's duplicate-prevention guarantee. A human-reported receipt
must not be mislabeled as provider-verified confirmation.

## Automated provider contract

The `BookingProvider` protocol has `revalidate`, `submit`, and read-only `lookup`.
The store persists an immutable proposal, expiry, exact approval digest, and
durable idempotency key. It claims an approved submission atomically before any
provider side effect. Changed terms, URL, property, or time invalidate approval.
Revocation and expiry are rechecked after revalidation. A submitted response must
match a stored provider receipt before the state becomes confirmed.

After a claim, retries never submit again. A lost response or process crash stays
`unknown`/`submitting` until lookup reconciles the same key. A missing lookup result
does not justify a fresh booking. Revoking a proposal cannot cancel an already
submitted or uncertain appointment; cancellation needs its own provider action.

Future adapters must discover authoritative property IDs, offered slots and
timezones, screening requirements, and consent terms. They must represent pending
requests and verification requirements accurately and provide reliable duplicate
prevention/reconciliation. They must validate network destinations independently;
an offer URL by itself is not an HTTP allowlist or permission to submit.

ShowMojo is an API candidate **for authorized partners**, with documented
[available times](https://docs.showmojo.com/docs/legacy/get-available-tour-times),
[screening questions](https://docs.showmojo.com/docs/legacy/get-screening-questions),
[scheduling](https://docs.showmojo.com/docs/legacy/schedule-tour), and
[tour results](https://docs.showmojo.com/docs/legacy/get-tour-result). Confirm API
access and the property's enabled booking permissions first. A public scheduling
page alone does not grant API credentials. Tenant Turner also exposes an
[account/partner API](https://api.tenantturner.com/). For renter-only use and
unsupported providers, use the official browser flow with user handoff.
[Rently's verification flow](https://use.rently.com/blog/rently-security-checks/)
illustrates why login, phone, and identity steps may require the user.

## Acceptance

```bash
.venv/bin/apartmint tour approval-demo
.venv/bin/pytest -q tests/test_booking.py tests/test_booking_cli.py
```

The demo uses a local HTTP provider with fictional contact data. It proves zero
submissions before simulated approval, one submission afterward, duplicate
suppression, and a receipt read from the restarted provider. It is deterministic
and does not call a model. The separate `booking-demo` still exercises real local
Qwen tool selection against the synthetic provider. The state-machine tests add
concurrent processes, expired/revoked approval, changed offers, response loss,
receipt mismatch, restart, and private-contact handling. A real provider and real
human approval remain separate acceptance milestones.
