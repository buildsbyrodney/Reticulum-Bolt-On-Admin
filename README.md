# Reticulum Bolt-On Admin

Reticulum Bolt-On Admin is a lightweight, observer-first web GUI for administrators who **already have Reticulum/rnsd installed and configured**. It is a separate project from [`buildsbyrodney/Reticulum-Admin-GUI`](https://github.com/buildsbyrodney/Reticulum-Admin-GUI), which remains the managed-appliance project.

Version 0.1.0 is deliberately small: it observes an existing shared instance and stores only GUI-local authentication, labels, bounded metrics, and events. It does not take ownership of Reticulum.

## Safety contract

Observer Mode never:

- creates or replaces Reticulum configuration;
- starts a second `rnsd` or independent Reticulum instance;
- replaces identities or changes Reticulum storage;
- restarts Reticulum or edits systemd/Compose units;
- edits interfaces or enables Transport mode;
- silently chooses between multiple configurations.

Runtime reads use supported `rnstatus --json` and `rnpath --table --json` utility attachment. The application deliberately does not construct `RNS.Reticulum` as a fallback. If shared-instance attachment is unavailable, the UI says **Unavailable**.

## v0.1.0 Observer Mode

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

All future management capabilities report disabled in v0.1.0. There are no management controls in the UI.

## Detection and supported deployments

Detection checks explicit `RETICULUM_CONFIG` / `RNS_CONFIG_DIR`, the invoking user's documented `~/.reticulum/config`, XDG-style config, `/etc/reticulum/config`, bounded `/home/*/.reticulum/config`, `/root/.reticulum/config`, and `/var/lib/reticulum/.reticulum/config`. It does not crawl the whole filesystem.

Supported observation targets:

- host-native Python Reticulum;
- `rnsd` managed by systemd;
- manually launched `rnsd` / shared instance;
- Docker-hosted Reticulum when the isolated observation helper can read its config/storage through a read-only mount and its shared instance is reachable. The web container never receives that mount.

Process-manager and Docker detection are evidence only. The backend abstraction is lifecycle-neutral: `ReticulumBackend` exposes `detect()`, `runtime_status()`, `paths()`, `interfaces()`, `clients()`, `config_view()`, and `capabilities()`.

## Native installation

Requirements: Linux, Python 3.10+, and an existing Reticulum installation. For native installs, `rnstatus` and `rnpath` must be available to the current user on `PATH` or in the standard pipx/user location at `$HOME/.local/bin`. Ubuntu/Debian, Raspberry Pi OS 64-bit, ARM64, and x86_64 are targets.

```sh
./install.sh
```

Run the native installer as the normal user who owns the Reticulum installation. Do not use `sudo`. The defaults are:

- application: `$HOME/.local/reticulum-bolt-on-admin`;
- GUI state: `$HOME/.local/share/reticulum-bolt-on-admin`.

`PREFIX` and `STATE_DIR` remain available as explicit overrides. The installer displays the detected config, storage candidate, shared-instance result, and Docker availability before asking approval. With multiple candidates it stops and requires `--config PATH`. `--yes` is suitable only after an administrator has reviewed the displayed/selected path.

When a user systemd session is available, the installer creates and starts `reticulum-bolt-on-admin.service` under the user's systemd configuration. Otherwise it prints the exact unprivileged launcher command. In both cases it prints the local URL, `http://127.0.0.1:8080/`. The generated launcher refuses to run the web GUI as root.

If the selected Reticulum config is not readable by the normal user, do not elevate the entire native GUI. Use the isolated Docker/system observation-helper preparation described below; root is needed only to read that protected config and create helper-owned snapshot state.

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

The image intentionally includes `rns==1.5.7` for compatible utilities. Only the helper invokes them in the protected Docker architecture. The web process never invokes `rnstatus`, `rnpath`, or `rnsd`. CI exercises identity-derived RPC authentication on AMD64 and ARM64, explicit `rpc_key` authentication on AMD64, and verifies that neither helper nor web starts `rnsd`.

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

Development begins on `verification/v0.1.0-observer`. Do not merge, tag, or release before review. CI runs Python regression tests, builds and executes the container for `linux/amd64` and `linux/arm64`, validates both Reticulum utilities, and confirms that the observer starts without an `rnsd` process.

## Known v0.1.0 limitations

- Exact runtime fields vary by Reticulum version; unsupported values are shown as **Unavailable**.
- Shared-instance reachability from the helper depends on the host deployment and IPC/network arrangement; failure is reported without fallback.
- Docker detection is best-effort and read-only; the web container itself intentionally has no Docker socket.
- systemd/process ownership is evidence, not authority to manage the service.
- No config writes, recovery actions, path drops, queue drops, reloads, or restarts exist yet.

## Provenance

The defensive runtime normalization, exact-via grouping concepts, conservative TCP-child proof rules, mobile navigation pattern, and bounded map-settling concepts were adapted from the managed Admin GUI. Messaging, bot, KJV, Nomad hosting, managed `rnsd`, lifecycle controls, and ownership assumptions were not copied.
