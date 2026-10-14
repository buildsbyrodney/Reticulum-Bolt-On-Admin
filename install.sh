#!/usr/bin/env bash
set -euo pipefail

PREFIX="${PREFIX:-/opt/reticulum-bolt-on-admin}"
STATE_DIR="${STATE_DIR:-/var/lib/reticulum-bolt-on-admin}"
CONFIG=""
APPROVE=0
PREPARE_DOCKER=0
while (($#)); do case "$1" in --config) CONFIG="$2"; shift 2;; --yes) APPROVE=1; shift;; --prepare-docker) PREPARE_DOCKER=1; shift;; *) echo "Unknown option: $1" >&2; exit 2;; esac; done

PYTHON_BIN="${PYTHON_BIN:-python3}"
command -v "$PYTHON_BIN" >/dev/null || { echo "Python 3.10+ is required. Nothing was changed." >&2; exit 1; }
"$PYTHON_BIN" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' \
  || { echo "Python 3.10+ is required; the detected interpreter is too old. Nothing was changed." >&2; exit 1; }
mapfile -t FOUND < <({ [[ -n "${RNS_CONFIG_DIR:-}" ]] && printf '%s\n' "$RNS_CONFIG_DIR/config"; printf '%s\n' "$HOME/.reticulum/config" "$HOME/.config/reticulum/config" /etc/reticulum/config /root/.reticulum/config; find /home -maxdepth 3 -type f -path '*/.reticulum/config' -print 2>/dev/null || true; } | awk '!seen[$0]++' | while read -r p; do [[ -r "$p" ]] && printf '%s\n' "$p"; done)

if [[ -n "$CONFIG" ]]; then
  [[ -r "$CONFIG" ]] || { echo "Selected config is not readable: $CONFIG. Nothing was changed." >&2; exit 1; }
elif ((${#FOUND[@]} == 1)); then CONFIG="${FOUND[0]}"
elif ((${#FOUND[@]} == 0)); then echo "No existing Reticulum config detected. Nothing was created." >&2; exit 1
else printf 'Multiple Reticulum configs detected:\n'; printf '  %s\n' "${FOUND[@]}"; echo "Re-run with --config PATH. Nothing was changed." >&2; exit 1
fi

echo "Existing Reticulum configuration: $CONFIG"
echo "Storage candidate: $(dirname "$CONFIG")/storage"
if command -v rnstatus >/dev/null && rnstatus --config "$(dirname "$CONFIG")" --json >/dev/null 2>&1; then echo "Shared instance: available"; else echo "Shared instance: unavailable (the GUI will report this; it will not start rnsd)"; fi
command -v docker >/dev/null && echo "Docker: detected (not required and not modified)" || echo "Docker: not detected (native install will be used)"
echo "Reticulum config/storage will not be written. rnsd will not be started, stopped, or restarted."
if ((APPROVE == 0)); then read -r -p "Install only the GUI and its private state directory? [y/N] " answer; [[ "$answer" =~ ^[Yy]$ ]] || { echo "Cancelled. Nothing was changed."; exit 0; }; fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OBSERVER_DATA="$STATE_DIR/observer-input"
GUI_STATE="$STATE_DIR/gui"
if ((PREPARE_DOCKER == 1)); then
  install -d -m 0755 "$STATE_DIR" "$OBSERVER_DATA"
  PYTHONPATH="$SCRIPT_DIR/src" "$PYTHON_BIN" -m reticulum_bolt_on.sanitizer --source "$CONFIG" --output-dir "$OBSERVER_DATA" --source-display-path "$CONFIG" --storage-display-path "$(dirname "$CONFIG")/storage" --deployment-type docker
  install -d -o 10001 -g 10001 -m 0700 "$GUI_STATE"
  echo "Sanitized observer data prepared at $OBSERVER_DATA."
  echo "GUI state prepared at $GUI_STATE for container UID 10001."
  echo "The production Reticulum configuration was read but not modified."
  exit 0
fi

install -d -m 0755 "$PREFIX" "$GUI_STATE"
"$PYTHON_BIN" -m venv "$PREFIX/venv"
"$PREFIX/venv/bin/pip" install .
SECRET_FILE="$GUI_STATE/secret"
if [[ ! -e "$SECRET_FILE" ]]; then umask 077; "$PYTHON_BIN" -c 'import secrets; print(secrets.token_hex(32))' > "$SECRET_FILE"; fi
cat > "$PREFIX/run" <<EOF
#!/usr/bin/env bash
export BOLT_ON_SECRET_KEY="\$(cat '$SECRET_FILE')"
export BOLT_ON_DATABASE='$GUI_STATE/observer.db'
export RETICULUM_CONFIG='$CONFIG'
exec '$PREFIX/venv/bin/gunicorn' -b 127.0.0.1:8080 'reticulum_bolt_on.app:create_app()'
EOF
chmod 0755 "$PREFIX/run"
echo "GUI installed at $PREFIX. Start $PREFIX/run explicitly or create a GUI-only service after review."
echo "No Reticulum files or services were modified."

