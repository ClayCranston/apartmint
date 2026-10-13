# Usability and verification plan

Updated: 2026-10-12 for 0.4.0. This is a local development prototype. Monitoring and
synthetic booking are implemented separately. Durable CLI proposals, approval,
manual handoff, and managed uninstall are implemented; a live provider adapter
and graphical approval screen remain unfinished. Passing internal tests does not
establish real-property coverage or customer deployment readiness.

## Product work, in priority order

| Priority | Current friction or risk | User-facing improvement | Acceptance evidence |
| --- | --- | --- | --- |
| 1 | Discovery returns candidates, but users must manually author source JSON; no real booking adapter connects the flows. | Guided setup: city/area, rental or sale, budget, then review compatible sources. Show unsupported sites explicitly. Start with one supported provider and keep location configurable. | A new user configures an area, reviews a discovered property, collects its listing, observes a price change, and proposes an offered tour using the same property identity. |
| 1 | A queued refresh or empty `status` result can look like success. | Display last attempted and last successful checks, next check, stale data, failures, and a retry action per source. Add a payload-free run ledger and worker/scheduler heartbeats. | Stop each dependency; the interface names the failed stage and never labels queued work completed. |
| 1 | Extracted text can confuse unit boundaries, appointment availability, and price context. | Show quoted evidence and observation time alongside each fact. Keep uncertain facts unknown and avoid price alerts across gaps. | Gold-data extraction and stateful history tests detect wrong-unit facts, false vacancies, and false price drops. |
| 1 | Setup requires several terminals and a separately installed Metal runtime. | Provide one install/start/stop/status flow, a supported-platform preflight, and supervised startup/recovery. | Fresh machine and reboot tests pass without the developer's existing runtime or filesystem paths. |
| 2 | Results are JSON and exceptions give little operational context. | A small local dashboard with Sources, Changes, and Pending approvals. Use clear error categories and recovery actions without logging private payloads. | A first-time user completes setup, fixes an unavailable source, and understands why an item needs review without editing code. |
| 2 | Durable approval exists in the CLI, but no real provider is registered. | Add a review screen and the first supported provider, showing property/unit, exact time, recipient/contact profile, and terms. Preserve uncertain outcomes. | Existing approval, expiry, slot-change, duplicate, and response-loss tests pass against a real provider sandbox, followed by an approved live canary. |
| 2 | The tarball is a source distribution, with no customer update/rollback flow. | Versioned platform bundles or a bootstrap installer, signed/checksummed releases, data backup/migration, and rollback. | Upgrade an older populated installation, restore it, and verify histories, settings, and booking idempotency records. |

The audit added `apartmint doctor`, conservative evidence regressions, a labeled
model evaluation, and a unified verification runner. Version 0.4 adds an immutable
approval ledger, private contact profiles, a deterministic HTTP approval fixture,
installation ownership, supervised stop/uninstall, and an optional owned Metal
runtime provisioner. Provisioner tests use stubs; the new runtime pin still needs
fresh-machine GPU acceptance. A dashboard, run ledger, and real provider adapter
remain unfinished. See [booking](BOOKING.md) and [uninstall](UNINSTALL.md).

For this narrow product, prefer a provider's supported structured data or a tested
listing-card adapter for identities, prices, timestamps, and tour slots. Keep
comparison arithmetic deterministic. Use the local model for request routing,
summaries, and extraction where needed; send uncertain interpretations to review.
A general computer-control agent adds failure modes without solving the present
source coverage and extraction problems. A human booking approval does not by
itself recover listings the monitor failed to find.

The first corpus runs also identify a diagnostic gap: the production client
collapses non-stop completions, invalid JSON, and other response-envelope failures
into one safe error. Add payload-free failure-stage, completion-reason, and token
usage metadata to the synthetic evaluation before attributing long failures to a
particular model or runtime defect. Unknown-unit omissions are observable model
recall failures; the validator rejects a bad extraction rather than silently
dropping individual rows.

## Run the checks

Run from an installed checkout with the development dependencies:

```bash
./scripts/setup.sh
.venv/bin/apartmint doctor
.venv/bin/python scripts/verify.py
.venv/bin/python scripts/verify.py --live-model
```

`doctor` checks source configuration, the local model health endpoint, local Redis,
and data-directory permissions. It exits nonzero if a prerequisite is unavailable.
It does not prove collector/worker/scheduler liveness or successful inference.
An empty source configuration is not ready for monitoring. `status` continues to
report stored observations, rather than service health.

The default verification run is offline and writes pytest JUnit XML plus a JSON
summary. `--live-model` additionally calls the configured local model for the
independently labeled corpus, a two-area price-change scenario, and a synthetic
booking. The model must already be running. None of these flows book a real tour.

```bash
# Repeat the same gold cases to expose nondeterministic failures.
.venv/bin/python scripts/verify.py --live-model --repeats 3

# Evaluate extraction alone; expected labels are never generated by the model.
.venv/bin/python scripts/eval_local_model.py \
  --output .runtime/evaluation/manual.json

# Test a specific packaged release in a separately extracted environment.
.venv/bin/python scripts/verify.py --live-model \
  --archive dist/apartmint-0.4.0.tar.gz
```

Verification artifacts are under `.runtime/verification/<timestamp>/` unless
`--output` selects a new directory. A failing selected check makes the command
exit nonzero. Interrupted runs remain incomplete; a successful prefix cannot
report a pass. Skipped layers are marked `not_run`. `release_ready` remains false
because the missing real-provider, fresh-machine, and recovery acceptance layers
cannot be inferred from synthetic success.

Package acceptance checks the specified archive, which may contain different code
from the working checkout. Do not silently overwrite an older release to make it
match the checkout: create a new version and retain its hash for release evidence.
This acceptance flow provisions the application environment but reuses an existing
model server. The earlier cold-start experiment reused an existing native Metal
runtime too; neither is a clean-machine installer test.

## Test layers and remaining work

| Layer | Implemented now | Next acceptance work |
| --- | --- | --- |
| Deterministic logic | Configuration, HTTP/DNS policy, evidence/schema validation, price arithmetic, freshness, omissions, deduplication, SQLite ordering, routing, booking fixture conflicts and receipts. | Property-based sequences of observations and crashes; backup/migration fixtures for every supported release. |
| Actual local-model quality | 26 hand-labeled synthetic documents; counts missed/invented units, false availability, false prices, exact case accuracy, precision and recall. | An independent held-out corpus of permitted representative pages, including supported locales, page layouts, unit identifiers and provider changes. Preserve labels independently of implementation. |
| Stateful market behavior | Real-model rental and sale price drops across two areas; repeated snapshots create no duplicate events. | Model-driven sequences for unknown price, disappearance, reappearance, stale pages, mixed currencies, and adjacent units, with expected event timelines. |
| Process integration | Earlier live Redis → sandboxed Celery → local model → SQLite smoke; launcher broker-selection regressions. | Repeatable isolated collector/worker/scheduler integration in the runner, task completion tracking, retry/kill/restart tests, and offline operation after provisioning. |
| Distribution | Tar checksum/content inspection, fresh extraction, separate application environment, extracted offline suite, approval and Qwen fixtures. Tiny-file uninstall tests stop real disposable process groups and remove owned models/runtime without a working app environment. Provisioning tests check hash failures and owned partial cleanup. | Fully fresh supported Mac and Linux NVIDIA machines, actual pinned runtime provisioning, long/spaced paths, low memory/disk, port conflicts, reboot, and upgrade/rollback. |
| Privacy boundaries | Endpoint allowlists, private destination rejection, secret-free task payloads, packaging exclusions, prior network-isolation experiments. | Repeated sandbox/container egress checks using synthetic secret canaries across errors, logs, traces, prompts, queues and archives. Never put real secrets in test fixtures. |
| Real provider and human approval | Durable exact approval, expiry/revocation, changed offers, concurrent submission claims, receipt lookup, response-loss/restart reconciliation, and private-profile tests; CLI manual handoff. | Implement one authorized provider adapter, run sandbox contract tests, and perform one explicitly approved live canary with genuine contact details. |
| Usability | Diagnostic output with remediation; no graphical workflow. | Observe unfamiliar users installing, selecting an area, reviewing changes, stopping monitoring, and approving/rejecting a proposal. Record completion, time, errors, and needed assistance. |

The 26-case corpus is a regression gate, not an estimate of market accuracy. Its
strict pass criterion requires all expected unit identities, statuses, confirmed
prices, and bedroom counts to match. Rejecting an entire page lowers recall and
fails its case; conservative rejection cannot masquerade as perfect extraction.
The score measures the combined model and production validators, not the raw model
alone. Repeat runs do not increase the number of independent test examples.

Run deterministic checks on each code change; run the local-model corpus after
prompt, validator, model, or runtime changes; and run the full platform matrix
before each release. Keep inference in a controlled local runner with synthetic
or explicitly permitted test data. Record application/archive hashes, model
revision and quantization, runtime versions, launch flags, hardware, corpus hash,
and test artifacts together. Hardware availability must produce an explicit skip,
not a passing NVIDIA or fresh-Mac deployment claim.

Current evidence rules recognize explicit unit markers and exclude tour-related
availability clauses conservatively. They do not understand arbitrary unlabeled
tables or external CSS/layout, and broad negation can reject valid statements such
as a unit being available with no deposit. Adapters and human review should handle
these unsupported cases; adding more regular expressions alone is not a general
website parser.

Before customer release, require zero false bookings, wrong-property submissions,
duplicate reservations, and false price-change events in the relevant acceptance
suite. Set precision/recall targets for each supported provider before looking at
the held-out results; report uncertainty and exclusions. One small passing corpus
cannot justify a general accuracy claim or support for every city and website.

## Human approval contract

The 0.4 store implements a durable state machine:

`proposed → approved → submitting → confirmed`

It also preserves rejection, revocation, expiry, failure, and unknown outcomes. Monitoring and
price comparisons can run unattended. The human approves the specific proposed
appointment, and deterministic code performs the provider action.

1. Bind approval to an immutable proposal ID and digest of provider, property,
   unit, slot start/end/timezone, contact fields, and material terms. Page text and
   model output cannot grant approval. Before approval, assert zero provider
   booking POSTs.
2. Rejecting, revoking, or expiring a proposal prevents submission. Changing the
   property, slot, recipient, contact fields, or terms requires a new proposal and
   approval. Revalidate that the exact slot is still offered before submitting.
3. Use a durable idempotency key. Concurrent workers, double clicks, retries, and
   process restarts must produce at most one reservation for the approved action.
4. If the provider commits but its response is lost, record `outcome unknown` and
   reconcile with that same key or provider reference. Do not create a fresh
   booking automatically. Providers without a reliable reconciliation mechanism
   need a manual resolution path.
5. Treat HTTP 200 alone as insufficient. Match a stored/provider-readable receipt
   to the approved property and slot. A missing or inconsistent receipt remains
   unconfirmed. Preserve only the necessary private audit data locally.

Automate these scenarios against a controllable provider fixture, then the real
provider's sandbox. A live canary requires a real supported property, offered
time, genuine recipient details, and explicit submission authorization. Synthetic
details belong in the fixture/sandbox. `tour approval-demo` checks the durable
approval path using simulated approval and a local HTTP provider. The separate
`booking-demo` has no human approval stage and proves only its documented
synthetic model tool and receipt flow. Neither establishes real-provider support.
