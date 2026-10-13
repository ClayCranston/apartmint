# Local validation

## Booking and lifecycle preview 0.4.0 — 2026-10-12

This version packages the earlier audit changes plus durable tour approval,
private contact profiles, manual provider handoff, managed service ownership,
stop/uninstall, and optional private Metal runtime provisioning. It still has no
live booking adapter and has not booked a real appointment.

Approval tests exercise expiry/revocation, changed slots/terms/URLs, concurrent
submission claims, lost responses, receipt mismatch, restart reconciliation, and
private contact handling. The new local HTTP acceptance fixture checks zero
unapproved submissions, one approved submission, duplicate suppression, and a
receipt fetched from the restarted provider. The separate Qwen booking fixture
continues to test actual local model tool selection.

Lifecycle tests use tiny disposable models/runtime files and real test processes.
They check complete stdlib CLI uninstall, forced process-group shutdown, stale and
orphaned records, shared symlink retention, ownership conflicts, matching plans,
private-data retention/purge, and scoped Docker cleanup using stub inventories.
Runtime provisioning tests use tiny synthetic wheel bytes and controlled tools;
they verify digest failures and cleanup of owned partial installations. These
tests do not remove the real development model or stop its server.

Release acceptance runs from `dist/apartmint-0.4.0.tar.gz` using
`scripts/test_tar_deployment.py`. Its report records the exact archive SHA-256,
extracted import provenance, extracted offline test result, approval fixture,
and real-local-Qwen synthetic booking. The report and logs live outside the
archive under `.runtime/deployment-tests/`; inspect their status and archive hash
before claiming that a particular release passed. A successful source test alone
does not establish extracted-release acceptance.

The optional new installer pins vLLM core/Metal 0.30.0 native wheels, each with a
verified upstream SHA-256. The pair has **not** been installed or GPU-tested here,
and its transitive dependencies are not fully locked. Live fixture tests reuse
the older preprovisioned local model server documented below. Fresh Mac setup,
NVIDIA serving, reboot/upgrade recovery, a real provider, and genuine human
approval of a live tour remain unverified.

The 26-case model-quality corpus was not rerun for this lifecycle/approval change.
Its last result remains **20/26 exact cases and a failed quality gate**. No passing
approval or uninstall test overrides that limitation. This archive is a development
preview, not a completed customer release.

## Usability and quality audit — 2026-10-11

The working checkout now has a readiness diagnostic, a repeatable verification
runner, a 26-case independently labeled synthetic extraction corpus, and evidence
regressions. At the time of this audit, the 0.3.0 archive did not include these
changes; they are now included in 0.4.0. The detailed test layers and unfinished product work are in
[the usability and verification plan](TESTING.md).

- All **281 offline tests passed** in the final audit run. Added checks include
  cross-unit evidence attribution, tour availability versus vacancy, plainly
  hidden/struck-out HTML prices, readiness diagnostics, evaluation scoring and
  interrupted-report behavior, collector broker selection, and tar acceptance
  model settings.
- The baseline model-plus-validator run matched all facts in **16 of 26 cases**,
  recovered **20 of 29 listing identities**, and confirmed **13 of 18 expected
  prices**. It accepted zero invented listing identities, false vacancies, or
  false confirmed prices in this small sample. Seven cases failed extraction or
  validation; two sale cases returned invalid extraction after about 119 seconds
  each. Other failures omitted known fields or entire listings, including a valid
  listing on a page containing prompt-injection text. This is a failed quality
  gate, not a production accuracy estimate.
- After the bounded evidence fixes, the final run matched **20 of 26 cases**,
  recovered **24 of 29 listing identities**, and confirmed **15 of 18 expected
  prices**. It still failed the strict quality gate. There were no accepted false
  vacancies, invented identities, or false confirmed prices in this corpus.
  Remaining failures omitted unknown-status units, omitted a studio's fields,
  returned no listing for the injection example, and rejected two sale responses.
  The median case took 4.12 seconds; the two sale failures took about 104–105
  seconds. Those failures occurred in response-envelope/completion/JSON checks,
  not the transport-timeout path; their exact cause needs additional safe metadata.
  These timings are observations from a small synthetic run, not a throughput
  benchmark. All labels remained unchanged between runs.
- The final real-local-model market scenario passed, including separate rental
  and sale price drops and duplicate-snapshot suppression. The synthetic booking
  scenario also passed its tool sequence, receipt, idempotency, and provider-restart
  checks. The combined runner exited nonzero because model quality failed. No new
  archive installation, real-provider submission, or fresh-machine acceptance was
  performed in this audit.
- The corpus hash is
  `1d3150a298694de05de0259af96d302c19ee379c95fde592f8a61852df952844`.
  Labels were not changed in response to model results. The run used the existing
  local `apartmint-qwen3` model alias, backed by the Qwen3 8B MLX 4-bit installation
  described below. No external model or real property booking was used.
- `doctor` found the local model health endpoint, Redis, and data directory ready.
  Source configuration is empty, so overall readiness is false. Collector,
  inference-worker, and scheduler liveness remain unverified by this diagnostic.

The baseline artifact is `.runtime/evaluation/baseline-2026-10-11.json`.
Final audit artifacts are under `.runtime/verification/audit-final-2026-10-11/`:
`summary.json`, `offline-tests.xml`, `model-quality.json`, individual check logs,
`doctor.json`, and `source-manifest.json`. Consult completion and status fields; an incomplete run cannot
establish acceptance. The runner deliberately does not claim release readiness
from successful internal or synthetic checks.

## Initial deployment validation — 2026-10-10

Host: Apple M4 Mac mini, 24 GiB unified memory, macOS arm64. About 24 GiB disk space
was available before setup. No NVIDIA GPU is present. SSH to the configured
`clay-personal` host timed out, so NVIDIA GPU capacity remains unverified.

- 194 application tests passed after review fixes, covering source-address policy,
  DNS pinning, redirects, local-only model endpoints, JSON/evidence checks,
  negated availability, monthly price evidence, snapshot age, over-budget page
  rejection, deduplication, concurrent observation ordering, and Celery separation.
  Additional checks cover configurable areas, rentals/sales/currencies, immutable
  source scope, conservative price baselines, legacy history migration, freshness,
  source retirement, local request boundaries, and unsupported CLI scope filters.
  Version 0.3 adds discovery parsing/address checks and synthetic booking tests,
  including simultaneous slot conflicts, receipt verification, and restart safety.
- Every model snapshot file matched its Git blob or LFS SHA-256 metadata from
  the pinned Hugging Face revision. Model safetensors size: 4,351,884,216 bytes;
  SHA-256 `fcc83d7537bee76cc143be3e25f8954b816798f93528103db1cddb02977c5617`.
- The real Qwen endpoint returned schema-constrained JSON. The two-unit apartment
  fixture extracted both units, found one available, and skipped duplicate
  analysis. The final direct run took approximately 8.1 seconds. This is one small
  synthetic smoke test, not an accuracy or throughput benchmark.
- The new live multi-area fixture passed all four Qwen extractions in 18.3 seconds:
  monthly rent USD 1,850 → 1,750 and sale asking price CAD 450,000 → 425,000.
  SQLite recorded separate drops of -100.00 (-5.405405%) and -25000.00
  (-5.555556%). Repeating analysis created no duplicate events. No real website,
  real location, private file, or external model was used for these checks.
- Five live local routing requests passed: one-area refresh, all-area report,
  all-area refresh, unrelated shell/upload rejection, and unconfigured-area
  rejection. Calls took approximately 1.5–2 seconds each. These were interpretation
  checks without actually enqueueing collection. A prior routing error motivated
  explicit area-evidence checks; this is a small regression sample, not a general
  guarantee of natural-language accuracy.
- Redis → sandboxed Celery → real local Qwen → SQLite passed with a separate
  synthetic unit. Broker task results are disabled; only source IDs are queued.
- The worker Docker image built successfully for this Mac's arm64 Docker engine.
  Compose validated with both CUDA and monitor profiles.
- The 0.2.0 image also passed a multi-area fixture as UID 10001 with networking
  disabled, a read-only container root and inbox, and writable results storage.
  It verified two price drops, null-price comparison resets, disappearance
  handling, and current-source filtering. Disposable test resources were removed.
- A disposable Compose deployment verified collector snapshot writes with a
  read-only container root, worker access to the isolated `0600` configuration
  copy, and blocked external connections on the internal worker network.
- The native sandbox denied a direct external TCP connection with PermissionError
  while allowing local Redis. This does not test resistance to same-user local
  proxies, host administration, filesystem access, or covert channels.
- The source archive was inspected for excluded runtime/secret paths and its
  extracted source passed the offline fixture demo. Model weights, actual source
  configuration, logs, `.env`, and runtime environments are not packaged.
- Version 0.3 was installed from its tarball into an independent directory and
  Python environment. The extracted application completed a real-local-Qwen tool
  sequence against a synthetic booking provider, persisted a confirmation, replayed
  the submission without a duplicate reservation, and read the confirmation after
  provider restart. Reports explicitly mark `real_appointment_booked: false`.
- A separate cold-start check launched the model using the extracted release's
  launcher and a digest-verified clone of the model files. It reused the already
  installed native Metal runtime; fresh runtime provisioning was not tested. An
  isolated Redis instance and the extracted worker launcher processed a queued
  listing, then the extracted application completed another synthetic booking.
  This test found and fixed a hardcoded worker-broker setting and macOS socket-path
  overflow when installed in a deeply nested directory.
- Public city discovery returned real HTTPS apartment/search candidates in a
  Seattle development smoke test. It did not fetch property pages, verify live
  tour slots, or contact a property. Search challenges and markup changes remain
  an external dependency; this result does not establish worldwide coverage.

The CUDA vLLM service, NVIDIA driver/kernel compatibility, authenticated sources,
real-area coverage, and larger/JavaScript-rendered pages have not been accepted.
Existing Metal runtime warnings are recorded in the README and runtime log.
