# Reticulum Bolt-On Admin

Reticulum Bolt-On Admin is a lightweight, observer-first web GUI for administrators who **already have Reticulum/rnsd installed and configured**. It is a separate project from [`buildsbyrodney/Reticulum-Admin-GUI`](https://github.com/buildsbyrodney/Reticulum-Admin-GUI), which remains the managed-appliance project.

Version 0.2.0 keeps Observer Mode as the default and adds a deliberately narrow, capability-gated Management Mode for reviewed recovery actions. It does not take ownership of Reticulum.

## Safety contract

Observer Mode never:

- creates or replaces Reticulum configuration;
- starts a second `rnsd` or independent Reticulum instance;
- replaces identities or changes Reticulum storage;
- restarts Reticulum or edits systemd/Compose units;
- edits interfaces or enables Transport mode;
- silently chooses between multiple configurations.

Observer runtime reads use supported `rnstatus --json` and `rnpath --table --json` utility attachment. The observer deliberately does not construct `RNS.Reticulum` as a fallback. Native management uses the selected existing RNS 1.5.7 runtime with `require_shared_instance=True`; failed attachment is fatal and never falls back to an independent instance. If shared-instance attachment is unavailable, the UI says **Unavailable**.

## v0.2.0 Observer Mode

- Conservative detection of host-native Python, systemd `rnsd`, manually launched/shared instances, and detectable Docker deployments.
- Explicit ambiguity failure when more than one readable config is found.
- Dashboard status, RNS version, selected config/storage paths, deployment type, Transport status, interface/client/path counts, top exact-via transport, and health warnings.
- Configured and runtime-only interfaces, traffic, supported client counts, and IFAC presence without secret display.
- Bounded raw path rendering (500) and exact-`via` grouping with five destination samples.
- Bounded path history and events rather than historical full path dumps.
- Positively proven inbound TCP clients only; IP addresses are never promoted to Reticulum identities.
- Conservative read-only Network Map with search, filters, pan/zoom, at most 250 path nodes, and at most 180 force-settling frames.
- GUI-local labels for destination, transport, and interface identifiers.
- Single-admin first-run authentication, secure cookie settings, password hashing, and CSRF protection.
- Responsive collapsed navigation for mobile browsers.

Observer Mode remains active even when a backend exposes management capabilities. The header distinguishes **Observer Mode** from **Management capability available**.

## v0.2.0 Management Mode

Management is capability-gated per backend and deployment. A control is not shown as available merely because another deployment supports it. Every execution requires the authenticated local administrator, a CSRF-protected POST, a separate review page, and an explicit confirmation. GET requests never execute management actions.

The first native shared-instance backend supports the following when the exact RNS 1.5.7 capability probe succeeds and the complete existing Reticulum state tree is already present:

- dropping one exact 32-hex destination path;
- dropping all current paths through one exact, non-empty 32-hex `via` transport, with current count and at most ten destination samples on the review page;
- dropping all queued announces, clearly distinguished from dropping paths.

RNS 1.5.7 per-interface attach/detach/reload is deliberately reported as disabled. In particular, TCP client runtime names can differ from their configured section names, so a reload could detach successfully but fail to reattach. No interface control is attempted and `rnsd` is never restarted as a substitute.

Each attempt records a bounded GUI-local audit event containing timestamp, action, target, success/failure, and allow-listed non-secret metadata. No credentials or production configuration are copied into audit records.

The current Docker/root-owned snapshot backend reports all management capabilities disabled. Its web container still receives no production config, identity, RPC credentials, IFAC secrets, or Docker socket. Extending the privileged helper is deferred until a narrowly scoped, authenticated request boundary is separately designed and tested.

Management Mode does not provide arbitrary configuration editing, Transport enable/disable, `rnsd`/systemd/Docker restart, IFAC or identity changes, or firewall/network changes.

## Detection and supported deployments

Detection checks explicit `RETICULUM_CONFIG` / `RNS_CONFIG_DIR`, the invoking user's documented `~/.reticulum/config`, XDG-style config, `/etc/reticulum/config`, bounded `/home/*/.reticulum/config`, `/root/.reticulum/config`, and `/var/lib/reticulum/.reticulum/config`. It does not crawl the whole filesystem.

Supported observation targets:

- host-native Python Reticulum;
- `rnsd` managed by systemd;
- manually launched `rnsd` / shared instance;
- Docker-hosted Reticulum when the isolated observation helper can read its config/storage through a read-only mount and its shared instance is reachable. The web container never receives that mount.

Process-manager and Docker detection are evidence only. The backend abstraction is lifecycle-neutral: `ReticulumBackend` exposes `detect()`, `runtime_status()`, `paths()`, `interfaces()`, `clients()`, `config_view()`, and `capabilities()`.

## Native install and upgrade

Requirements: Linux, Python 3.10+, and an existing Reticulum installation. For native installs, `rnstatus` and `rnpath` must be available to the current user on `PATH` or in the standard pipx/user location at `$HOME/.local/bin`. Ubuntu/Debian, Raspberry Pi OS 64-bit, ARM64, and x86_64 are targets.

```sh
./install.sh --config "$HOME/.reticulum/config"
```

Run the native installer as the normal user who owns the Reticulum installation. Do not use `sudo`. The defaults are:

- application: `$HOME/.local/reticulum-bolt-on-admin`;
- GUI state: `$HOME/.local/share/reticulum-bolt-on-admin`.

`PREFIX` and `STATE_DIR` remain available as explicit overrides. The installer displays the detected config, storage candidate, shared-instance result, and Docker availability before asking approval. With multiple candidates it stops and requires `--config PATH`. `--yes` is suitable only after an administrator has reviewed the displayed/selected path.

When a user systemd session is available, the installer creates and starts `reticulum-bolt-on-admin.service` under the user's systemd configuration. Otherwise it prints the exact unprivileged launcher command. In both cases it prints the local URL, `http://127.0.0.1:8080/`. The generated launcher refuses to run the web GUI as root.

If the selected Reticulum config is not readable by the normal user, do not elevate the entire native GUI. Use the isolated Docker/system observation-helper preparation described below; root is needed only to read that protected config and create helper-owned snapshot state.

To upgrade a native installation, update the working copy and run the same installer again as the same normal user. The separate GUI state directory is reused, preserving the admin login, labels, and local history.

```sh
git pull --ff-only
./install.sh --config "$HOME/.reticulum/config"
```

Custom locations remain explicit on both installation and removal:

```sh
PREFIX="$HOME/apps/reticulum-bolt-on-admin" \
STATE_DIR="$HOME/.local/share/my-bolt-on-state" \
./install.sh --config "$HOME/.reticulum/config"
```

## Native uninstall and clean reinstall

Run the native uninstaller as the same normal user. Do not use `sudo`. It prints its exact removal/preservation plan and asks for confirmation. The default removes only the Bolt-On user service and application while preserving the GUI database, secret, login, labels, and local history.

```sh
./uninstall.sh
```

Reinstalling afterward restores the existing login, labels, and local history:

```sh
./install.sh --config "$HOME/.reticulum/config"
```

For a completely fresh GUI, explicitly purge Bolt-On state and then reinstall:

```sh
./uninstall.sh --purge
./install.sh --config "$HOME/.reticulum/config"
```

`--purge` removes only the Bolt-On GUI state directory. It still does not touch Reticulum configuration, storage, identities, `rnsd`, Reticulum services, or Reticulum containers. Use the same overrides when uninstalling a custom installation:

```sh
PREFIX="$HOME/apps/reticulum-bolt-on-admin" \
STATE_DIR="$HOME/.local/share/my-bolt-on-state" \
./uninstall.sh
```

Use `--yes` only for a removal plan that has already been reviewed. After stopping the exact Bolt-On user service, the uninstaller verifies that no confirmed Bolt-On process remains. It reports any remaining listener on port 8080 but never kills an unrelated process merely for using that port.

## Docker deployment

```sh
export RETICULUM_DIR="$HOME/.reticulum"
export BOLT_ON_SECRET_KEY="$(openssl rand -hex 32)"
sudo STATE_DIR="$(pwd)/bolt-on-data" ./install.sh --config "$RETICULUM_DIR/config" --prepare-docker
export BOLT_ON_STATE="$(pwd)/bolt-on-data/gui"
export BOLT_ON_OBSERVER_DATA="$(pwd)/bolt-on-data/observer-input"
docker compose up --build
```

The single `sudo` command is required only when preparing the isolated observation boundary: it may need to read a `0600 root:root` production config and must create state owned separately by the capability-free root helper and container UID 10001. It does not install or run the web GUI as root. If the production config is user-readable and this privileged boundary is not needed, use the native installation instead.

The web container runs as UID 10001, drops all capabilities, has a read-only root filesystem, and has no Docker socket, production Reticulum mount, identity, or RPC key. It reads only sanitized JSON snapshots from the helper plus its separate writable GUI database. Reticulum itself does not need to be containerized.

A separate observation-helper boundary runs as container UID 0 solely because the selected production config may be `0600 root:root`. The production directory is mounted read-only. The helper uses host networking to run supported read-only `rnstatus --json` and `rnpath --table --json` queries directly against that directory, allowing Reticulum 1.5.7 to use the existing identity-derived or explicit RPC authentication in place. The helper drops all Linux capabilities, enables `no-new-privileges`, has a read-only root filesystem, is not privileged, and has no Docker socket.

Every 30 seconds the helper atomically replaces `config-view.json`, `runtime-status.json`, `paths.json`, and `refresh-status.json`. Output is normalized and bounded, and contains presentation fields, counters, path rows, IFAC presence booleans, source metadata, and collection times only. It never publishes the raw config, `rpc_key`, identities/private keys, IFAC/network keys, passphrases, or other credentials. Failed authentication or collection marks the refresh failed while preserving the last complete snapshots; the UI reports **Unavailable** or **Stale** instead of treating old data as current.

The image intentionally includes `rns==1.5.7` for compatible utilities. Only the observation helper invokes them in the protected Docker architecture; Docker management remains disabled. The web process never invokes `rnstatus`, `rnpath`, or `rnsd`. CI exercises identity-derived RPC authentication on AMD64 and ARM64, explicit `rpc_key` authentication on AMD64, confirms snapshot management is disabled, and verifies that neither helper nor web starts `rnsd`.

### Docker/helper cleanup

Run Docker cleanup from this repository with the same Bolt-On state paths used during setup. The fixed Compose project name targets only the Bolt-On helper and web resources. By default, snapshots and GUI state are preserved:

```sh
export STATE_DIR="$(pwd)/bolt-on-data"
export BOLT_ON_STATE="$STATE_DIR/gui"
export BOLT_ON_OBSERVER_DATA="$STATE_DIR/observer-input"
./uninstall.sh --docker
```

For a fully clean Docker/helper reinstall, `--purge` also removes the two Bolt-On state directories. Elevated access is needed only when the isolated helper created root-owned snapshot state:

```sh
sudo STATE_DIR="$(pwd)/bolt-on-data" \
  BOLT_ON_STATE="$(pwd)/bolt-on-data/gui" \
  BOLT_ON_OBSERVER_DATA="$(pwd)/bolt-on-data/observer-input" \
  ./uninstall.sh --docker --purge
```

Docker cleanup never removes volumes generically and never discovers, stops, or edits the production Reticulum container, service, Compose project, config, storage, or identities.

## Path-flood visibility

Paths are grouped strictly by the exact non-empty Reticulum `via` hash. Defaults are informational warnings, never automated mitigation:

- 100+ total paths: elevated;
- 1,000+ total paths: high;
- one valid non-empty 32-hex via with 100+ paths and at least 50% share: “High concentration of learned paths through one transport.”

Missing/unknown `via` values never produce a concentration accusation. The observer does not say “attack detected.”

## Development

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -e '.[test]'
pytest
```

Release work begins on a verification branch. Do not merge, tag, or release before review. CI runs Python regression tests, builds and executes the container for `linux/amd64` and `linux/arm64`, validates both Reticulum utilities, and confirms that the observer starts without an `rnsd` process.

## Known v0.2.0 limitations

- Exact runtime fields vary by Reticulum version; unsupported values are shown as **Unavailable**.
- Shared-instance reachability from the helper depends on the host deployment and IPC/network arrangement; failure is reported without fallback.
- Docker detection is best-effort and read-only; the web container itself intentionally has no Docker socket.
- systemd/process ownership is evidence, not authority to manage the service.
- Management actions currently require a readable native installation using the existing user-authorized RNS 1.5.7 runtime. Docker/root-owned snapshot deployments remain Observer-only.
- Per-interface control is disabled for RNS 1.5.7 because its runtime/configured interface naming does not provide a safe, tested reload contract; it never falls back to restarting `rnsd`.
- Configuration writes and all service/container restart operations remain intentionally unavailable.

## Provenance

The defensive runtime normalization, exact-via grouping concepts, conservative TCP-child proof rules, mobile navigation pattern, and bounded map-settling concepts were adapted from the managed Admin GUI. Messaging, bot, KJV, Nomad hosting, managed `rnsd`, lifecycle controls, and ownership assumptions were not copied.
