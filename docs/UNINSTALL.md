# Installation ownership and removal

Deleting a downloaded `.tar.gz` only deletes that archive. It cannot execute a
cleanup command, stop model serving, remove extracted files, or cancel a tour.
Run the explicit uninstaller before deleting the extracted installation. The
release does not install a hidden file watcher to observe archive deletion.

## Release installations

`scripts/setup.sh` creates an installation manifest bound to this directory. A
new application `.venv` and application files are registered as owned. Fresh
downloads through `scripts/download_model.py` register their model directories,
including partial downloads, for cleanup. Model-serving caches created by managed
launchers are registered too.

On Apple Silicon, use a **private** native runtime under `.runtime/metal`. The
optional `scripts/provision_metal.py` can provision and register one; its pinned
upstream runtime has separate acceptance requirements from the older development
runtime. Review its dry run before installation. If installing a private runtime
manually, register it explicitly:

```bash
python3 scripts/manage.py own .runtime/metal
```

The managed launcher refuses an unregistered private runtime. An external runtime
or symlink, such as the existing development runtime shared with another project,
is recorded as retained. Its target is never removed. The new provisioner refuses
to replace an existing runtime. Do not copy this development installation's
shared-runtime symlink into a customer deployment.

Start services through the supplied launchers. Native launchers track supervised
process groups, including model subprocesses. For a managed Mac Redis service:

```bash
python3 scripts/manage.py compose --file compose.local.yaml -- up -d redis
```

The CUDA launcher uses a unique Compose project for the installation. Use
`manage.py compose` for later Compose operations as well. Manually launched or
older untracked services are outside the ownership record; the uninstaller cannot
claim to stop them. Never use a global process-name kill or Docker prune as a
substitute for ownership.

## Stop or uninstall

The bootstrap uses only Python's standard library, so it works even if the
application `.venv` is unavailable. Run from the extracted installation:

```bash
python3 scripts/manage.py status
python3 scripts/manage.py stop --dry-run
python3 scripts/manage.py stop --yes
```

To remove the installed application, owned weights, private native model runtime,
owned caches, and managed serving processes/containers:

```bash
python3 scripts/manage.py uninstall --dry-run
python3 scripts/manage.py uninstall --yes
```

The dry run lists exact removals, running services, retained resources, and any
ownership conflicts. Execution requires that matching plan from the last 15
minutes. If the plan changed, rerun the dry run; deletion is not silently broadened.

Private history, configuration, `.env`, and Docker data volumes remain by default.
To remove those known installation-local private assets as well:

```bash
python3 scripts/manage.py uninstall --dry-run --purge-data
python3 scripts/manage.py uninstall --yes --purge-data
```

Choose the purge option before uninstalling; the application scripts are removed
by uninstall. Externally configured data paths, shared runtimes, shared images,
Docker itself, `uv`, and system Python are retained. Shared Docker images may still
occupy disk after the owned serving container is removed; they are not running
model services. The ownership report lists retained resources. Existing model
folders or runtimes are not silently adopted; explicitly register an internal
asset only when it belongs exclusively to this installation.

Process start identity, command fingerprint, process group, installation identity,
and asset identities are checked. Stale or conflicting identities and orphaned
live service groups block removal. A moved installation requires reconciliation;
the manifest must not be edited to bypass these checks. Symlinks can be unlinked
without following their targets. After successful cleanup and review of retained
data, remove the remaining empty folders and original downloaded archive normally.

## Test scope

Lifecycle tests use tiny disposable model/runtime files and real test processes,
including a parent/child group, stale records, and an orphaned group. A complete
stdlib CLI uninstall test checks that serving stops and owned models/runtime/app
files disappear. Docker cleanup tests use controlled resource inventories and
exact IDs. These tests do not delete the development model, stop the development
server, reinstall the native GPU runtime, or establish NVIDIA compatibility.
