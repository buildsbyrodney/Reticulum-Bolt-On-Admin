#!/usr/bin/env bash
set -euo pipefail

APPROVE=0
PURGE=0
DOCKER_MODE=0
while (($#)); do
  case "$1" in
    --yes) APPROVE=1; shift ;;
    --purge) PURGE=1; shift ;;
    --docker) DOCKER_MODE=1; shift ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
done

if ((EUID == 0)); then
  DEFAULT_PREFIX=/opt/reticulum-bolt-on-admin
  DEFAULT_STATE_DIR=/var/lib/reticulum-bolt-on-admin
else
  DEFAULT_PREFIX="$HOME/.local/reticulum-bolt-on-admin"
  DEFAULT_STATE_DIR="$HOME/.local/share/reticulum-bolt-on-admin"
fi
PREFIX="$(realpath -m -- "${PREFIX:-$DEFAULT_PREFIX}")"
STATE_DIR="$(realpath -m -- "${STATE_DIR:-$DEFAULT_STATE_DIR}")"
SERVICE_NAME=reticulum-bolt-on-admin.service
USER_UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
UNIT_FILE="$USER_UNIT_DIR/$SERVICE_NAME"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

safe_removal_target() {
  local target="$1"
  [[ -n "$target" && "$target" = /* ]] || return 1
  case "${target%/}" in
    /|/bin|/boot|/dev|/etc|/home|/lib|/lib64|/media|/mnt|/opt|/proc|/root|/run|/sbin|/srv|/sys|/tmp|/usr|/var|\
    "$HOME"|"$HOME/.config"|"$HOME/.local"|"$HOME/.local/share") return 1 ;;
    "$HOME/.reticulum"|"$HOME/.reticulum"/*|"$HOME/.config/reticulum"|"$HOME/.config/reticulum"/*|\
    /etc/reticulum|/etc/reticulum/*|/root/.reticulum|/root/.reticulum/*|/var/lib/reticulum|/var/lib/reticulum/*) return 1 ;;
  esac
}

paths_overlap() {
  local first="${1%/}" second="${2%/}"
  [[ "$first" == "$second" || "$first" == "$second"/* || "$second" == "$first"/* ]]
}

safe_removal_target "$PREFIX" || { echo "Refusing unsafe PREFIX removal target: $PREFIX" >&2; exit 1; }
safe_removal_target "$STATE_DIR" || { echo "Refusing unsafe STATE_DIR removal target: $STATE_DIR" >&2; exit 1; }
if ((PURGE == 0)) && [[ "$STATE_DIR" == "$PREFIX" || "$STATE_DIR" == "$PREFIX"/* ]]; then
  echo "Refusing uninstall because preserved STATE_DIR is inside PREFIX: $STATE_DIR" >&2
  echo "Choose separate PREFIX and STATE_DIR locations. Nothing was changed." >&2
  exit 1
fi

if ((DOCKER_MODE == 0 && EUID == 0)); then
  echo "Refusing to uninstall the native GUI as root." >&2
  echo "Run this command as the normal user who installed Reticulum Bolt-On Admin. No sudo is required." >&2
  echo "Nothing was changed." >&2
  exit 1
fi

if ((DOCKER_MODE == 1)); then
  COMPOSE_PROJECT=reticulum-bolt-on-admin
  WEB_STATE="$(realpath -m -- "${BOLT_ON_STATE:-$STATE_DIR/gui}")"
  OBSERVER_DATA="$(realpath -m -- "${BOLT_ON_OBSERVER_DATA:-$STATE_DIR/observer-input}")"
  safe_removal_target "$WEB_STATE" || { echo "Refusing unsafe BOLT_ON_STATE removal target: $WEB_STATE" >&2; exit 1; }
  safe_removal_target "$OBSERVER_DATA" || { echo "Refusing unsafe BOLT_ON_OBSERVER_DATA removal target: $OBSERVER_DATA" >&2; exit 1; }
  if [[ -n "${RETICULUM_DIR:-}" ]]; then
    PRODUCTION_RETICULUM_DIR="$(realpath -m -- "$RETICULUM_DIR")"
    if paths_overlap "$WEB_STATE" "$PRODUCTION_RETICULUM_DIR" || paths_overlap "$OBSERVER_DATA" "$PRODUCTION_RETICULUM_DIR"; then
      echo "Refusing cleanup because Bolt-On state overlaps production RETICULUM_DIR: $PRODUCTION_RETICULUM_DIR" >&2
      echo "Nothing was changed." >&2
      exit 1
    fi
  fi

  echo "Docker/helper uninstall plan"
  echo "  Stop/remove Compose project: $COMPOSE_PROJECT"
  echo "  Compose file: $SCRIPT_DIR/compose.yaml"
  if ((PURGE == 1)); then
    echo "  Remove Bolt-On web state: $WEB_STATE"
    echo "  Remove Bolt-On observer snapshots: $OBSERVER_DATA"
  else
    echo "  Preserve Bolt-On web state: $WEB_STATE"
    echo "  Preserve Bolt-On observer snapshots: $OBSERVER_DATA"
  fi
  echo "  Preserve production Reticulum config, storage, identities, services, and containers"
  if ((APPROVE == 0)); then
    read -r -p "Proceed with only the Docker/helper cleanup above? [y/N] " answer
    [[ "$answer" =~ ^[Yy]$ ]] || { echo "Cancelled. Nothing was changed."; exit 0; }
  fi

  command -v docker >/dev/null || { echo "Docker is unavailable. Nothing was changed." >&2; exit 1; }
  RETICULUM_DIR="${RETICULUM_DIR:-/nonexistent-reticulum-cleanup-source}" \
  BOLT_ON_SECRET_KEY="${BOLT_ON_SECRET_KEY:-unused-cleanup-secret}" \
  BOLT_ON_STATE="$WEB_STATE" \
  BOLT_ON_OBSERVER_DATA="$OBSERVER_DATA" \
    docker compose -f "$SCRIPT_DIR/compose.yaml" -p "$COMPOSE_PROJECT" down --remove-orphans
  if ((PURGE == 1)); then
    rm -rf -- "$WEB_STATE" "$OBSERVER_DATA"
    rmdir -- "$STATE_DIR" 2>/dev/null || true
  fi
  if docker ps -q --filter "label=com.docker.compose.project=$COMPOSE_PROJECT" | grep -q .; then
    echo "Bolt-On Docker resources are still running; cleanup is incomplete." >&2
    exit 1
  fi
  echo "Bolt-On helper/web containers are stopped and removed. Production Reticulum was not modified."
  exit 0
fi

echo "Native uninstall plan"
echo "  Stop/disable user service: $SERVICE_NAME"
echo "  Remove user service unit: $UNIT_FILE"
echo "  Remove application directory: $PREFIX"
if ((PURGE == 1)); then
  echo "  Remove GUI state/database/secret: $STATE_DIR"
else
  echo "  Preserve GUI state/database/secret (login, labels, and local history): $STATE_DIR"
fi
echo "  Preserve all Reticulum config, storage, identities, services, and containers"
if ((APPROVE == 0)); then
  read -r -p "Proceed with only the native Bolt-On removal above? [y/N] " answer
  [[ "$answer" =~ ^[Yy]$ ]] || { echo "Cancelled. Nothing was changed."; exit 0; }
fi

SYSTEMD_USER_AVAILABLE=0
if command -v systemctl >/dev/null && systemctl --user show-environment >/dev/null 2>&1; then
  SYSTEMD_USER_AVAILABLE=1
  if [[ -e "$UNIT_FILE" || -L "$USER_UNIT_DIR/default.target.wants/$SERVICE_NAME" ]] \
      || systemctl --user list-unit-files "$SERVICE_NAME" --no-legend 2>/dev/null | grep -q "$SERVICE_NAME"; then
    systemctl --user disable --now "$SERVICE_NAME" >/dev/null 2>&1 || systemctl --user stop "$SERVICE_NAME" >/dev/null 2>&1 || true
  fi
fi

rm -f -- "$UNIT_FILE" "$USER_UNIT_DIR/default.target.wants/$SERVICE_NAME"
if ((SYSTEMD_USER_AVAILABLE == 1)); then
  systemctl --user daemon-reload >/dev/null 2>&1 || true
  systemctl --user reset-failed "$SERVICE_NAME" >/dev/null 2>&1 || true
fi

bolt_on_pids() {
  local proc pid exe owner arg matched
  for proc in /proc/[0-9]*; do
    pid="${proc##*/}"
    [[ "$pid" != "$$" ]] || continue
    owner="$(stat -c %u "$proc" 2>/dev/null || true)"
    [[ "$owner" == "$EUID" ]] || continue
    exe="$(readlink "$proc/exe" 2>/dev/null || true)"
    case "$exe" in
      "$PREFIX"/*) printf '%s\n' "$pid"; continue ;;
    esac
    matched=0
    while IFS= read -r -d '' arg; do
      if [[ "$arg" == "$PREFIX/run" || "$arg" == "$PREFIX/venv/bin/gunicorn" ]]; then
        matched=1
        break
      fi
    done < "$proc/cmdline" 2>/dev/null || true
    ((matched == 1)) && printf '%s\n' "$pid"
  done
}

mapfile -t PIDS < <(bolt_on_pids)
if ((${#PIDS[@]})); then
  kill -TERM "${PIDS[@]}" 2>/dev/null || true
  for _ in 1 2 3 4 5; do
    mapfile -t PIDS < <(bolt_on_pids)
    ((${#PIDS[@]} == 0)) && break
    sleep 1
  done
fi

rm -rf -- "$PREFIX"
if ((PURGE == 1)); then rm -rf -- "$STATE_DIR"; fi

if ((SYSTEMD_USER_AVAILABLE == 1)) && systemctl --user is-active --quiet "$SERVICE_NAME"; then
  echo "Bolt-On user service is still active; uninstall is incomplete." >&2
  exit 1
fi
mapfile -t PIDS < <(bolt_on_pids)
if ((${#PIDS[@]})); then
  echo "A confirmed Bolt-On process is still running (${PIDS[*]}); uninstall is incomplete." >&2
  exit 1
fi

if command -v ss >/dev/null; then
  LISTENER="$(ss -H -ltnp 'sport = :8080' 2>/dev/null || true)"
  if [[ -n "$LISTENER" ]]; then
    echo "Port 127.0.0.1:8080 still has a listener. It was not killed because it is not a confirmed Bolt-On process:"
    printf '%s\n' "$LISTENER"
  else
    echo "No listener remains on 127.0.0.1:8080."
  fi
else
  echo "Port ownership check unavailable because ss is not installed; no unconfirmed process was killed."
fi

echo "Native Bolt-On application and user service removed."
if ((PURGE == 1)); then
  echo "Bolt-On GUI state removed (--purge)."
else
  echo "Bolt-On GUI state preserved at $STATE_DIR."
fi
echo "Reticulum config, storage, identities, rnsd, services, and Docker deployment were not modified."
