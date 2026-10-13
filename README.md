# Apartmint — local housing listing and price monitor

Development preview 0.4.0, 2026-10-12. Qwen3 8B is running on this Mac mini through native
vLLM Metal. The application has its own `.venv`; `.env` is private (`0600`) and
excluded from Git, container builds, and distribution archives. No cloud model
provider or fallback is implemented.

The M4 has 24 GiB unified memory and cannot run NVIDIA CUDA/NVFP4 on its GPU through
Docker Desktop. The compatible model is `Qwen/Qwen3-8B-MLX-4bit`, pinned to revision
`383413e909f3bc5303ce195ebbdf0339c5a1a2a3`. All snapshot files were checked against
Hugging Face's Git/LFS digests and staged under `models/qwen3-8b-mlx-4bit`. Existing
weights were reused with an APFS clone; another multi-GB download was unnecessary.

The CUDA Compose deployment is prepared for a Linux NVIDIA host. It has **not been
GPU-tested**: the configured `clay-personal` SSH host did not respond. See
[the model decision](docs/MODEL-DECISION.md) for NVFP4 compatibility, older-GPU
fallbacks, current upstream sources, and licensing.

## Current behavior

This remains a development preview. The latest independently labeled extraction
run matched 20 of 26 cases and failed its quality gate. The current release work
tests approval and installation lifecycle; it does not establish production
listing accuracy or a real booking integration. See [validation](docs/VALIDATION.md).

An internet-facing collector fetches only configured public HTTPS URLs, rejects
private-address destinations, pins DNS results during TLS connection, validates
redirect destinations, strips script/style content, and writes local snapshots.
A separate Celery worker passes document text to the local model, requests strict
JSON, validates each result against its quoted source evidence, and records state
and observation history in SQLite. Only source identifiers travel through Redis.
Deterministic code records first observations, availability changes, price drops,
and price increases. It computes amounts and percentages from confirmed prices;
the model does not invent market statistics. Duplicate snapshots do not create
duplicate change events. There are no messaging integrations, arbitrary model
tools, shell execution, or model-selected URLs. A separate explicit booking demo
exposes three bounded tools to local Qwen against a synthetic loopback provider.

The engine is city-agnostic. An area is a configurable label attached to approved
sources, and each source declares `listing_type` (`rental` or `sale`) and `currency`.
Reports keep area, housing type, and currency groups separate. The same engine can
serve multiple cities without a code change. Website-specific collection and
local number-format normalization belong in source adapters; no city is hardcoded
in the model prompt or business logic.

Live smoke testing used synthetic apartment data: two units, one available and one
leased. Real local Qwen inference passed extraction and deduplication. A second
fixture passed through Redis → sandboxed Celery → local Qwen → SQLite. The fixture
demo (`apartmint demo`) separately runs without a model or network and must not be
confused with those live-model checks.

The multi-area live test also tracked a monthly rental decrease from USD 1,850 to
1,750 and a sale asking-price decrease from CAD 450,000 to 425,000. Qwen extracted
the evidence; deterministic code calculated the two drops and kept their areas,
housing types, and currencies separate. See [validation](docs/VALIDATION.md).

No live sources are configured yet. Fill in
`config/sources.json` before enabling collection:

```json
{
  "area": "Default selected area",
  "sources": [
    {
      "id": "area-one-rentals",
      "url": "https://rentals.example/availability",
      "area": "Selected area one",
      "listing_type": "rental",
      "currency": "USD"
    },
    {
      "id": "area-two-sales",
      "url": "https://homes.example/listings",
      "area": "Selected area two",
      "listing_type": "sale",
      "currency": "CAD"
    }
  ]
}
```

The top-level `area` is the default for sources without their own area. Older
configuration remains valid and defaults to rentals in USD. Area labels do not
geofence results: choose source URLs or adapters that cover the requested region.
The example URLs above are placeholders. A source identifier is bound to its URL,
area, housing type, and currency; use a new identifier when changing those fields
so unrelated listing histories cannot be compared.

## Narrow local requests and market history

```bash
.venv/bin/apartmint refresh --area "Selected area one"
.venv/bin/apartmint request "Check listings in Selected area one"
.venv/bin/apartmint request "Show price changes across all configured areas over 30 days"
.venv/bin/apartmint market --area "Selected area one" --days 30
.venv/bin/apartmint events --area "Selected area one" --days 30
```

`refresh` queues a single collection for the area's configured source IDs. It
requires Redis and a collector worker. `request` uses the local model to select
only `refresh`, `report`, or `decline`, an existing area, and a bounded time window.
The model cannot create sources, change schedules, read secrets, or launch commands.
Scheduled monitoring remains a normal Celery Beat configuration. Model-selected
reports use only currently configured source IDs; the explicit `market` and `events`
commands can inspect retained history, including retired areas.

Name one exact configured area label or explicitly say "all configured areas" in
a natural-language request. Ambiguous or inconsistent selections are rejected.
Routing remains a model interpretation; use explicit `refresh --area` and `market`
commands when a deterministic command is preferred. CLI filters that a command
does not support are rejected instead of silently ignored.

Reports include observed-source counts, the observation time range, freshness,
first-seen listings, availability changes, and price-change counts. "First seen"
means first observed by this installation, not the original date listed on the
market. Missing listings become unknown, not automatically sold or leased.
Price events require consecutive, fresh, confirmed prices for the same source
and listing. Null, absent, ambiguous, or stale observations break comparison;
the historical price remains stored without asserting a drop across that gap.
Amounts use exact decimal strings and signed percentage changes. These are
observations of tracked sources, not estimates of an entire city's housing market.

Rental prices are monthly; sale prices are asking prices. Source currencies are
never converted or pooled. Fees/deposits and non-monthly rates cannot establish a
monthly-rent baseline. Stable listing identifiers should be preserved by each
source adapter; common Unit/Apt prefixes are normalized, but unrelated properties
or cross-site duplicates are not merged automatically.

## City discovery and booking acceptance

```bash
.venv/bin/apartmint discover "Your city, region"
.venv/bin/apartmint booking-demo "Your city, region"
```

`discover` searches DuckDuckGo Lite for public apartment/tour candidate URLs.
It sends only a public search query, uses no cloud model, and does not fetch the
candidate pages, reserve appointments, or modify approved monitoring sources.
Search results are untrusted candidates: `availability_verified` is always false.
The public search endpoint may challenge requests or change its markup; failures
are explicit and worldwide coverage is not guaranteed.

`booking-demo` is a **synthetic acceptance test**, not a real property booking
command. It starts a local fixture populated with fictional apartments using the
given city label. Local Qwen calls `find_properties`, `get_tour_slots`, and
`book_tour`. Deterministic code permits only discovered properties and offered
future slots, supplies fixed `@example.com` contact data outside the model prompt,
and verifies the saved confirmation. Duplicate submission must return the same
confirmation, and the confirmation must survive provider restart. Reports and
fixture state are stored under `data/booking-tests/` with private permissions.
No real appointment is booked, and no contact details are sent to a property.

Version 0.4 adds `apartmint tour prepare`, `show`, `approve`, `reject`, `revoke`,
and `handoff`. Proposals bind approval to the exact provider, property, offered
time, contact profile, and terms. The durable store checks expiry, revalidates
offers, prevents duplicate submissions, and reconciles uncertain outcomes through
provider lookup. The CLI currently has **no live provider adapter**: `handoff`
prints the official scheduling link for manual completion, while `submit` fails
explicitly. Approval alone is not a reservation. See [real-tour requirements and
commands](docs/BOOKING.md). `apartmint tour approval-demo` exercises these approval
and receipt rules against a local HTTP fixture without a model or real booking.

To verify an independent installation from a packaged release, with `uv` and a
local model server already provisioned:

```bash
.venv/bin/python scripts/test_tar_deployment.py dist/apartmint-0.4.0.tar.gz \
  --city "Synthetic Test City"
```

This checks the archive checksum and contents, extracts to a new directory,
installs its own environment from the included locks, verifies import provenance,
and runs the extracted offline suite, approval fixture, and synthetic booking
with local Qwen. Uninstall tests use tiny disposable model files and real test
processes; the running development model is preserved. On Mac, the booking process is
network-sandboxed to loopback. It preserves an acceptance report and extracted
installation under `.runtime/deployment-tests/`. It does not provision a new model
runtime or prove a real property's booking integration. Live tour availability and
real submissions require a tested provider adapter and genuine contact details.

## Local Mac setup

```bash
./scripts/setup.sh
python3 scripts/provision_metal.py --dry-run
python3 scripts/provision_metal.py --install
.venv/bin/python scripts/download_model.py mlx
python3 scripts/manage.py compose --file compose.local.yaml -- up -d redis
./scripts/start_metal.sh
```

On this workstation `.runtime/metal` points to the previously tested isolated
vLLM Metal environment in the `brain` project. That runtime is **not included** in
the tarball. The optional provisioner above is for a fresh Apple Silicon Mac on
macOS 15 or later: it installs private Python/runtime/cache directories, verifies
the pinned vLLM and Metal 0.30.0 wheels, and registers partial or complete assets
for uninstall. **That new runtime pin has not passed GPU or model-quality testing;
its transitive dependencies are not fully locked.** It refuses existing runtime
paths. The archive tests reuse the running, older development runtime instead.
Alternatively, install a compatible environment following the
[official guide](https://github.com/vllm-project/vllm-metal/blob/main/docs/installation.md)
and register an exclusively owned `.runtime/metal`, or set
`APARTMINT_METAL_RUNTIME` to an external environment that uninstall will retain.
The existing runtime is vLLM `0.25.1+cpu`, Metal `0.3.0.dev20260723125609`, MLX
`0.32.0`, and Transformers `5.12.1`. Its inherited tokenizer/CPU-extension warnings
remain; successful Qwen serving does not resolve those upstream compatibility
warnings. The newer upstream Metal release requires separate regression testing.

The launcher verifies the model offline, clears inherited environment variables,
disables telemetry and request/access logging, binds to `127.0.0.1:11437`, uses an
8192-token context, one concurrent sequence, and a 0.40 memory fraction. Local
IPC socket paths are relative to the installation, so long extraction paths do
not exceed macOS socket-name limits. Observed
KV allocation was approximately 1.99 GB in addition to 4.35 GB of model files and
runtime overhead. The memory fraction is not a claim of free GPU memory.

In another terminal:

```bash
./scripts/start_worker.sh
```

Both launchers run in the foreground and stop with Ctrl-C. Managed installations
also track service process groups for explicit stop/uninstall. `APARTMINT_MODEL_PORT`
selects the model port; point the worker at the same URL using
`APARTMINT_MODEL_BASE_URL`. `APARTMINT_BROKER_URL` selects the worker broker,
allowing isolated test deployments alongside an existing installation. For a configured source,
start the collector and schedule in additional terminals:

```bash
./scripts/start_collector.sh
./scripts/start_collector.sh beat
```

For services launched by a managed installation:

```bash
python3 scripts/manage.py status
python3 scripts/manage.py stop --dry-run
python3 scripts/manage.py stop --yes
```

The earlier development session's model and idle worker are untracked and remain
running; they were not adopted into a release installation. No collector or Beat
was started. Do not use old PID files as proof of current process ownership.

Beat polls every 900 seconds by default (`APARTMINT_POLL_SECONDS`, minimum 60).
It is deliberately not started before real source configuration exists.
The native CLI reads only named `APARTMINT_*` process settings; it does not source
`.env`. Its defaults match the local launchers. `once` is a convenient development
command that combines collection and inference in one process; use separate
collector/worker services when handling private data.

```bash
.venv/bin/apartmint demo
.venv/bin/python scripts/smoke.py
.venv/bin/python scripts/market_smoke.py
.venv/bin/apartmint collect
.venv/bin/apartmint analyze
.venv/bin/apartmint status
.venv/bin/apartmint doctor
.venv/bin/pytest -q
```

`doctor` distinguishes missing, empty, or invalid source configuration from local
model/broker failures and data-directory permission issues. Its success checks
prerequisites only; it does not establish worker/scheduler liveness or completed
monitoring. `status` reports stored observations. `apartmint --version` identifies
the installed application.

Run `.venv/bin/python scripts/verify.py` for offline checks or add `--live-model`
for the hand-labeled extraction corpus, market changes, and synthetic booking.
The runner writes explicit pass/fail/not-run results and exits nonzero on any
selected failure. See [the usability and test plan](docs/TESTING.md) for remaining
provider, human-approval, deployment, and recovery acceptance work.

## NVIDIA Docker setup

On the target Linux host, first install Docker and the NVIDIA Container Toolkit.
Run `scripts/preflight.py` to inspect GPU model, compute capability, driver, VRAM
usage, and disk capacity. Never stop another GPU workload automatically to make
room. Model weights, Python packages, and container images require internet access
during provisioning; inference then uses local assets offline.

```bash
./scripts/setup.sh
.venv/bin/python scripts/preflight.py
.venv/bin/python scripts/download_model.py nvfp4
./scripts/start_cuda.sh nvfp4
```

For an older NVIDIA GPU, use the pinned AWQ checkpoint by changing both `nvfp4`
arguments to `awq`. NVFP4 has native acceleration on Blackwell; current vLLM also
offers a Marlin weight-only path on compatible older GPUs. Exact hardware still
requires a cold-start and task-quality test. The launch script requires an 8 GiB
allocation plus 1 GiB unused reserve and refuses insufficient free VRAM. These
are conservative setup checks, not measured minimum VRAM or a throughput promise.

The pinned image defaults to `vllm/vllm-openai:v0.31.0-cu129`; validate it against
the host driver. Compose reads quantization from the downloaded checkpoint,
uses FP16 activations/KV, `hermes` tool parsing, `qwen3` reasoning parsing,
8192 context, 2048 batch tokens, and one sequence. `hermes` is a parser here;
the Hermes agent harness is not installed. The worker disables thinking per
request. No `trust_remote_code`, host networking, Docker socket, or public model
port is enabled. Caches and JIT workspaces use writable temporary storage.

After configuring real sources, enable the collector and Beat:

```bash
python3 scripts/manage.py compose -- --profile cuda --profile monitor up -d
python3 scripts/manage.py compose -- --profile cuda exec worker apartmint status
python3 scripts/manage.py stop --dry-run
python3 scripts/manage.py stop --yes
```

When using AWQ, retain `APARTMINT_MODEL_DIR=./models/qwen3-8b-awq` in `.env` or the
shell for subsequent Compose commands; `start_cuda.sh awq` exports it only for its
own invocation. Volumes persist across `down`; do not use `down -v` on real data.

## Private configuration and isolation

The CUDA model, inference worker, Redis, and scheduler attach only to a Docker
`internal` network. The collector additionally has internet access, and receives
neither the private environment file nor the result database. A network-disabled
initialization container copies `.env` into a worker-owned `0600` file at
`/run/private/runtime.env`; the worker mounts that volume read-only. This avoids
loosening permissions on the original `.env`. Recreate that initialization
container when changing private configuration.

This gives deterministic worker code private configuration access. The current
extraction task does not need credentials and therefore never reads that file or
inserts its contents into model prompts. Add a destination-specific credential
adapter when a real authenticated source requires one. Do not give the model a
general shell or expose secret-bearing results to a cloud agent. This development
session used only synthetic fixtures and never inspected real secret values.

The Mac sandbox was tested to deny direct external connections while allowing
local Redis and local inference. It still permits loopback services and Unix
sockets and does not isolate files from the same user or administrator. Docker
isolation was also tested to deny external connections from the worker network.
These are network restrictions, **not a physically air-gapped machine or proof of
isolation from a host administrator**. The current Codex development environment
has host filesystem access; true separation from a cloud-backed developer agent
requires a separate account/VM/host with private storage inaccessible to it.

## Product limits and packaging

This is a working development foundation, not a release advertised as a finished
apartment-search service. It supports static HTML only. Pages exceeding the local
model's 4000-byte document budget are rejected before overwriting a snapshot;
larger pages and JavaScript-only sites need focused source adapters or a tested
chunking implementation. Snapshot age defaults to 1800 seconds; stale observations
are not current availability. Authentication adapters, real-area evaluation,
notification destinations, installer/signing/update flows, and CUDA hardware
acceptance remain product work. Model extraction remains probabilistic despite
schema and evidence checks.

```bash
.venv/bin/python scripts/package.py
```

This creates `dist/apartmint-0.4.0.tar.gz` and a SHA-256 file. The allowlisted source
archive excludes `.env`, source URLs, model weights, local runtimes, logs, and data.
It includes pinned model download references and application dependency locks.
It is **not a fully offline installer**: provision dependencies/images/models
before disconnecting. The CUDA image tags should be pinned to tested image digests
for a customer release. Application distribution licensing has not been chosen;
retain the bundled model licenses if redistributing weights. See
[third-party notes](docs/THIRD-PARTY.md).

Deleting the archive cannot uninstall an extracted application or stop serving.
Run the explicit cleanup from the installed directory before deleting it:

```bash
python3 scripts/manage.py uninstall --dry-run
python3 scripts/manage.py uninstall --yes
```

This removes owned weights, private model runtime/caches, application files, and
managed services. Private data/configuration and shared resources are retained;
use `--purge-data` on both commands if also removing installation-local private
data. See [ownership and uninstall](docs/UNINSTALL.md) for the exact scope.
