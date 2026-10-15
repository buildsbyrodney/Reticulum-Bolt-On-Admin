#!/usr/bin/env bash
set -euo pipefail

CONFIG=""
APPROVE=0
PREPARE_DOCKER=0
while (($#)); do case "$1" in --config) CONFIG="$2"; shift 2;; --yes) APPROVE=1; shift;; --prepare-docker) PREPARE_DOCKER=1; shift;; *) echo "Unknown option: $1" >&2; exit 2;; esac; done

if ((EUID == 0)); then
  DEFAULT_PREFIX=/opt/reticulum-bolt-on-admin
  DEFAULT_STATE_DIR=/var/lib/reticulum-bolt-on-admin
else
  DEFAULT_PREFIX="$HOME/.local/reticulum-bolt-on-admin"
  DEFAULT_STATE_DIR="$HOME/.local/share/reticulum-bolt-on-admin"
fi
PREFIX="${PREFIX:-$DEFAULT_PREFIX}"
STATE_DIR="${STATE_DIR:-$DEFAULT_STATE_DIR}"

PYTHON_BIN="${PYTHON_BIN:-python3}"
command -v "$PYTHON_BIN" >/dev/null || { echo "Python 3.10+ is required. Nothing was changed." >&2; exit 1; }
"$PYTHON_BIN" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' \
  || { echo "Python 3.10+ is required; the detected interpreter is too old. Nothing was changed." >&2; exit 1; }

if ((EUID == 0 && PREPARE_DOCKER == 0)); then
  echo "Refusing to install or run the native web GUI as root." >&2
  echo "Run this installer as the normal user who owns the Reticulum installation." >&2
  echo "Root is reserved only for --prepare-docker, where it reads a protected production config and prepares helper-owned snapshot state." >&2
  echo "Nothing was changed." >&2
  exit 1
fi

RNSTATUS_BIN=""
RNPATH_BIN=""
RNS_TOOL_PATH=""
if ((PREPARE_DOCKER == 0)); then
  RNSTATUS_BIN="$(command -v rnstatus || true)"
  RNPATH_BIN="$(command -v rnpath || true)"
  if [[ -z "$RNSTATUS_BIN" || -z "$RNPATH_BIN" ]]; then
    echo "Native Observer Mode requires rnstatus and rnpath on the current user's PATH. Nothing was changed." >&2
    echo "Install Reticulum for this user first; do not use sudo to run the GUI installer." >&2
    exit 1
  fi
  RNS_TOOL_PATH="$(dirname "$RNSTATUS_BIN")"
  if [[ "$(dirname "$RNPATH_BIN")" != "$RNS_TOOL_PATH" ]]; then
    RNS_TOOL_PATH="$RNS_TOOL_PATH:$(dirname "$RNPATH_BIN")"
  fi
fi
mapfile -t FOUND < <({ [[ -n "${RNS_CONFIG_DIR:-}" ]] && printf '%s\n' "$RNS_CONFIG_DIR/config"; printf '%s\n' "$HOME/.reticulum/config" "$HOME/.config/reticulum/config" /etc/reticulum/config /root/.reticulum/config; find /home -maxdepth 3 -type f -path '*/.reticulum/config' -print 2>/dev/null || true; } | awk '!seen[$0]++' | while read -r p; do [[ -r "$p" ]] && printf '%s\n' "$p"; done)

if [[ -n "$CONFIG" ]]; then
  if [[ ! -r "$CONFIG" ]]; then
    echo "Selected config is not readable by the current user: $CONFIG. Nothing was changed." >&2
    echo "Do not run the native web GUI with sudo." >&2
    echo "For a root-owned Docker/system installation, use the isolated --prepare-docker helper setup; elevated access is needed only to read that protected config and create helper-owned snapshot state." >&2
    exit 1
  fi
elif ((${#FOUND[@]} == 1)); then CONFIG="${FOUND[0]}"
elif ((${#FOUND[@]} == 0)); then echo "No existing Reticulum config detected. Nothing was created." >&2; exit 1
else printf 'Multiple Reticulum configs detected:\n'; printf '  %s\n' "${FOUND[@]}"; echo "Re-run with --config PATH. Nothing was changed." >&2; exit 1
fi

echo "Existing Reticulum configuration: $CONFIG"
echo "Storage candidate: $(dirname "$CONFIG")/storage"
if [[ -n "$RNSTATUS_BIN" ]] && "$RNSTATUS_BIN" --config "$(dirname "$CONFIG")" --json >/dev/null 2>&1; then echo "Shared instance: available"; else echo "Shared instance: unavailable (the GUI will report this; it will not start rnsd)"; fi
command -v docker >/dev/null && echo "Docker: detected (not required and not modified)" || echo "Docker: not detected (native install will be used)"
echo "Reticulum config/storage will not be written. rnsd will not be started, stopped, or restarted."
if ((APPROVE == 0)); then read -r -p "Install only the GUI and its private state directory? [y/N] " answer; [[ "$answer" =~ ^[Yy]$ ]] || { echo "Cancelled. Nothing was changed."; exit 0; }; fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OBSERVER_DATA="$STATE_DIR/observer-input"
GUI_STATE="$STATE_DIR/gui"
if ((PREPARE_DOCKER == 1)); then
  if ((EUID != 0)); then
    echo "Elevated access is required only for Docker/system observation preparation." >&2
    echo "Reason: the helper must read the selected protected config once and create snapshot state owned by the capability-free UID 0 helper; the web GUI will still run unprivileged as UID 10001." >&2
    printf "Run exactly:\n  sudo STATE_DIR=%q %q --config %q --prepare-docker\n" "$STATE_DIR" "$SCRIPT_DIR/install.sh" "$CONFIG" >&2
    echo "Nothing was changed." >&2
    exit 1
  fi
  install -d -m 0755 "$STATE_DIR"
  # The capability-free UID 0 helper must own only its GUI-managed snapshot
  # directory so it can atomically replace JSON files. Production mounts are
  # separate and read-only, and are never passed to install/chown/chmod.
  install -d -o 0 -g 0 -m 0755 "$OBSERVER_DATA"
  PYTHONPATH="$SCRIPT_DIR/src" "$PYTHON_BIN" -m reticulum_bolt_on.sanitizer --source "$CONFIG" --output-dir "$OBSERVER_DATA" --source-display-path "$CONFIG" --storage-display-path "$(dirname "$CONFIG")/storage" --deployment-type docker --config-only
  install -d -o 10001 -g 10001 -m 0700 "$GUI_STATE"
  echo "Sanitized config view prepared at $OBSERVER_DATA; the read-only observation helper will collect runtime state."
  echo "GUI state prepared at $GUI_STATE for container UID 10001."
  echo "The production Reticulum configuration was read but not modified."
  exit 0
fi

install -d -m 0755 "$PREFIX"
install -d -m 0700 "$GUI_STATE"
"$PYTHON_BIN" -m venv "$PREFIX/venv"
"$PREFIX/venv/bin/pip" install "$SCRIPT_DIR"
SECRET_FILE="$GUI_STATE/secret"
if [[ ! -e "$SECRET_FILE" ]]; then umask 077; "$PYTHON_BIN" -c 'import secrets; print(secrets.token_hex(32))' > "$SECRET_FILE"; fi
printf -v SECRET_FILE_Q %q "$SECRET_FILE"
printf -v DATABASE_Q %q "$GUI_STATE/observer.db"
printf -v CONFIG_Q %q "$CONFIG"
printf -v GUNICORN_Q %q "$PREFIX/venv/bin/gunicorn"
printf -v RNS_TOOL_PATH_Q %q "$RNS_TOOL_PATH"
cat > "$PREFIX/run" <<EOF
#!/usr/bin/env bash
if ((EUID == 0)); then
  echo "Refusing to run the Reticulum Bolt-On Admin web GUI as root." >&2
  exit 1
fi
export BOLT_ON_SECRET_KEY="\$(cat $SECRET_FILE_Q)"
export BOLT_ON_DATABASE=$DATABASE_Q
export RETICULUM_CONFIG=$CONFIG_Q
export PATH=$RNS_TOOL_PATH_Q:"\$PATH"
exec $GUNICORN_Q -b 127.0.0.1:8080 'reticulum_bolt_on.app:create_app()'
EOF
chmod 0755 "$PREFIX/run"

SERVICE_NAME=reticulum-bolt-on-admin.service
USER_UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
SERVICE_STARTED=0
if command -v systemctl >/dev/null && systemctl --user show-environment >/dev/null 2>&1; then
  install -d -m 0755 "$USER_UNIT_DIR"
  ESCAPED_RUN=${PREFIX//\\/\\\\}
  ESCAPED_RUN=${ESCAPED_RUN//\"/\\\"}
  cat > "$USER_UNIT_DIR/$SERVICE_NAME" <<EOF
[Unit]
Description=Reticulum Bolt-On Admin (unprivileged Observer Mode)
After=default.target

[Service]
Type=simple
ExecStart=/bin/bash "$ESCAPED_RUN/run"
Restart=on-failure
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=default.target
EOF
  if systemctl --user daemon-reload && systemctl --user enable --now "$SERVICE_NAME"; then
    SERVICE_STARTED=1
    echo "Installed and started user service: $SERVICE_NAME"
  else
    echo "User systemd is present but the service could not be started; use the launcher command below." >&2
  fi
else
  echo "A user systemd session is not available; use the launcher command below. No sudo is required."
fi

echo "GUI installed for user $(id -un) at $PREFIX."
echo "Exact launcher command: $PREFIX/run"
echo "Local URL: http://127.0.0.1:8080/"
if ((SERVICE_STARTED == 1)); then echo "User service status: systemctl --user status $SERVICE_NAME"; fi
echo "No Reticulum files or services were modified."

