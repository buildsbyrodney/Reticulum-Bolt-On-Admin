#!/usr/bin/env bash
set -euo pipefail

IMAGE=${1:?image tag required}
PLATFORM=${2:?platform required}
root=$(mktemp -d)
suffix=${RANDOM}-$$
production=bolt-on-rnsd-$suffix
helper=bolt-on-helper-$suffix
observer=bolt-on-observer-$suffix
failure=bolt-on-failure-$suffix
explicit_production=bolt-on-rnsd-explicit-$suffix
explicit_helper=bolt-on-helper-explicit-$suffix
cleanup() {
  docker rm -f "$failure" "$observer" "$helper" "$production" "$explicit_helper" "$explicit_production" >/dev/null 2>&1 || true
  case "$root" in /tmp/*|"${RUNNER_TEMP:-/nonexistent}"/*) sudo rm -rf -- "$root";; esac
}
trap cleanup EXIT

mkdir -p "$root/production" "$root/observer-data" "$root/gui-state" "$root/failure-data"
printf '%s\n' '[reticulum]' '  share_instance = Yes' '  instance_name = bolt-on-ci' '  enable_transport = No' '' '[[Protected]]' '  type = AutoInterface' '  enabled = No' '  network_name = ci-private-network' '  passphrase = ci-never-copy-secret' > "$root/production/config"
sudo chown -R root:root "$root/production" "$root/observer-data" "$root/failure-data"
sudo chmod 0755 "$root/production" "$root/observer-data" "$root/failure-data"
sudo chmod 0600 "$root/production/config"
sudo chown 10001:10001 "$root/gui-state"
sudo chmod 0700 "$root/gui-state"

docker run -d --name "$production" --platform "$PLATFORM" --network host --read-only --cap-drop ALL --security-opt no-new-privileges --tmpfs /tmp:rw,noexec,nosuid -v "$root/production:/production" --user 0:0 "$IMAGE" rnsd --config /production --service >/dev/null
for attempt in $(seq 1 60); do
  python3 -c 'import socket; s=socket.socket(socket.AF_UNIX); s.connect(b"\0rns/bolt-on-ci")' >/dev/null 2>&1 && break
  test "$attempt" -lt 60
  sleep 1
done

config_before=$(sudo sha256sum "$root/production/config" | cut -d' ' -f1)
config_stat_before=$(sudo stat -c '%a:%u:%g' "$root/production/config")
storage_before=$(sudo find "$root/production/storage" -type f -print0 | sudo sort -z | sudo xargs -0 sha256sum | sha256sum)
pid_before=$(docker inspect "$production" --format '{{.State.Pid}}')
started_before=$(docker inspect "$production" --format '{{.State.StartedAt}}')
restart_before=$(docker inspect "$production" --format '{{.RestartCount}}')

# Exercise one supported management operation against this isolated existing
# shared instance. The production tree is mounted read-only, and the helper's
# require_shared_instance=True boundary makes independent fallback impossible.
docker run --rm --platform "$PLATFORM" --user 0:0 --network host --read-only --cap-drop ALL --security-opt no-new-privileges --tmpfs /tmp:rw,noexec,nosuid -v "$root/production:/management:ro" "$IMAGE" python /app/src/reticulum_bolt_on/management_helper.py --config /management drop-announces
test "$(sudo sha256sum "$root/production/config" | cut -d' ' -f1)" = "$config_before"
test "$(sudo find "$root/production/storage" -type f -print0 | sudo sort -z | sudo xargs -0 sha256sum | sha256sum)" = "$storage_before"
test "$(docker inspect "$production" --format '{{.State.Pid}}')" = "$pid_before"
test "$(docker inspect "$production" --format '{{.State.StartedAt}}')" = "$started_before"
test "$(docker inspect "$production" --format '{{.RestartCount}}')" = "$restart_before"

docker run -d --name "$helper" --platform "$PLATFORM" --user 0:0 --network host --read-only --cap-drop ALL --security-opt no-new-privileges --tmpfs /tmp:rw,noexec,nosuid -v "$root/production:/source:ro" -v "$root/observer-data:/observer-data" "$IMAGE" reticulum-bolt-on-sanitize --source /source/config --output-dir /observer-data --source-display-path /root/.reticulum/config --storage-display-path /root/.reticulum/storage --deployment-type docker --watch-seconds 5 >/dev/null
for attempt in $(seq 1 60); do
  sudo grep -q '"ok": true' "$root/observer-data/refresh-status.json" 2>/dev/null && break
  test "$attempt" -lt 60
  sleep 1
done

test "$(sudo stat -c '%a:%u:%g' "$root/production/config")" = "$config_stat_before"
test "$(sudo sha256sum "$root/production/config" | cut -d' ' -f1)" = "$config_before"
for secret in ci-never-copy-secret ci-private-network; do
  if sudo grep -R -F "$secret" "$root/observer-data"; then exit 1; fi
done
for required in runtime-status.json paths.json config-view.json refresh-status.json; do sudo test -r "$root/observer-data/$required"; done
sudo test "$(find "$root/observer-data" -type f -perm /022 | wc -l)" = 0

docker run -d --name "$observer" --platform "$PLATFORM" --network none --read-only --cap-drop ALL --security-opt no-new-privileges --tmpfs /tmp:rw,noexec,nosuid -e BOLT_ON_SECRET_KEY=ci-secret -e BOLT_ON_DATABASE=/state/observer.db -e BOLT_ON_SNAPSHOT_DIR=/observer-data -v "$root/observer-data:/observer-data:ro" -v "$root/gui-state:/state" "$IMAGE" >/dev/null
for attempt in $(seq 1 30); do
  docker exec "$observer" python -c 'from pathlib import Path; from reticulum_bolt_on.backend import SnapshotBackend; s=SnapshotBackend(Path("/observer-data")).runtime_status(); assert s.available and s.running and s.rns_version' && break
  test "$attempt" -lt 30
  sleep 1
done
docker exec "$observer" python -c 'from pathlib import Path; from reticulum_bolt_on.backend import SnapshotBackend; c=SnapshotBackend(Path("/observer-data")).capabilities(); assert not any(c.get(k) for k in ("drop_path","drop_via","drop_announces","attach_interface","detach_interface","reload_interface"))'
test "$(docker inspect "$observer" --format '{{.Config.User}}')" = observer
test "$(docker inspect "$observer" --format '{{range .Mounts}}{{println .Source}}{{end}}' | grep -Fxc "$root/production")" = 0
docker exec "$observer" python -c 'import pathlib; ps=[p.read_bytes().replace(b"\0",b" ").decode("utf-8","ignore") for p in pathlib.Path("/proc").glob("[0-9]*/cmdline")]; assert not [p for p in ps if any(pathlib.Path(t).name in {"rnsd","rnstatus","rnpath"} for t in p.split())]'
docker exec "$helper" python -c 'import pathlib; ps=[p.read_bytes().replace(b"\0",b" ").decode("utf-8","ignore") for p in pathlib.Path("/proc").glob("[0-9]*/cmdline")]; assert not [p for p in ps if any(pathlib.Path(t).name == "rnsd" for t in p.split())]'

test "$(sudo sha256sum "$root/production/config" | cut -d' ' -f1)" = "$config_before"
test "$(sudo find "$root/production/storage" -type f -print0 | sudo sort -z | sudo xargs -0 sha256sum | sha256sum)" = "$storage_before"
test "$(docker inspect "$production" --format '{{.State.Pid}}')" = "$pid_before"
test "$(docker inspect "$production" --format '{{.State.StartedAt}}')" = "$started_before"
test "$(docker inspect "$production" --format '{{.RestartCount}}')" = "$restart_before"

sudo cp -a "$root/observer-data/." "$root/failure-data/"
docker run --rm --platform "$PLATFORM" --user 0:0 --network none --read-only --cap-drop ALL --security-opt no-new-privileges --tmpfs /tmp:rw,noexec,nosuid -v "$root/production:/source:ro" -v "$root/failure-data:/observer-data" "$IMAGE" reticulum-bolt-on-sanitize --source /source/config --output-dir /observer-data --watch-seconds 0 >/dev/null 2>&1 && exit 1 || true
docker run -d --name "$failure" --platform "$PLATFORM" --network none --read-only --cap-drop ALL --security-opt no-new-privileges --tmpfs /tmp:rw,noexec,nosuid -e BOLT_ON_SECRET_KEY=ci-secret -e BOLT_ON_DATABASE=/state/failure.db -e BOLT_ON_SNAPSHOT_DIR=/observer-data -v "$root/failure-data:/observer-data:ro" -v "$root/gui-state:/state" "$IMAGE" >/dev/null
docker exec "$failure" python -c 'from pathlib import Path; from reticulum_bolt_on.backend import SnapshotBackend; s=SnapshotBackend(Path("/observer-data")).runtime_status(); assert not s.available and s.running is None and s.error'
docker exec "$failure" python -c 'import pathlib; ps=[p.read_bytes().replace(b"\0",b" ").decode("utf-8","ignore") for p in pathlib.Path("/proc").glob("[0-9]*/cmdline")]; assert not [p for p in ps if any(pathlib.Path(t).name == "rnsd" for t in p.split())]'

docker rm -f "$failure" "$observer" "$helper" >/dev/null
python3 -c 'import socket; s=socket.socket(socket.AF_UNIX); s.connect(b"\0rns/bolt-on-ci")'
test "$(docker inspect "$production" --format '{{.State.Pid}}')" = "$pid_before"
test "$(docker inspect "$production" --format '{{.State.StartedAt}}')" = "$started_before"
test "$(docker inspect "$production" --format '{{.RestartCount}}')" = "$restart_before"
test "$(sudo sha256sum "$root/production/config" | cut -d' ' -f1)" = "$config_before"
test "$(sudo find "$root/production/storage" -type f -print0 | sudo sort -z | sudo xargs -0 sha256sum | sha256sum)" = "$storage_before"

# An additional AMD64 fixture proves explicit RPC-key authentication remains
# inside the helper boundary and is never copied to observer output.
if [[ "$PLATFORM" == "linux/amd64" ]]; then
  mkdir -p "$root/explicit-production" "$root/explicit-output"
  printf '%s\n' '[reticulum]' '  share_instance = Yes' '  instance_name = bolt-on-ci-explicit' '  rpc_key = explicit-ci-rpc-secret' '  enable_transport = No' > "$root/explicit-production/config"
  sudo chown -R root:root "$root/explicit-production" "$root/explicit-output"
  sudo chmod 0755 "$root/explicit-production" "$root/explicit-output"
  sudo chmod 0600 "$root/explicit-production/config"
  docker run -d --name "$explicit_production" --platform "$PLATFORM" --network host --read-only --cap-drop ALL --security-opt no-new-privileges --tmpfs /tmp:rw,noexec,nosuid -v "$root/explicit-production:/production" --user 0:0 "$IMAGE" rnsd --config /production --service >/dev/null
  for attempt in $(seq 1 30); do
    python3 -c 'import socket; s=socket.socket(socket.AF_UNIX); s.connect(b"\0rns/bolt-on-ci-explicit")' >/dev/null 2>&1 && break
    test "$attempt" -lt 30
    sleep 1
  done
  docker run --rm --platform "$PLATFORM" --user 0:0 --network host --read-only --cap-drop ALL --security-opt no-new-privileges --tmpfs /tmp:rw,noexec,nosuid -v "$root/explicit-production:/source:ro" -v "$root/explicit-output:/observer-data" "$IMAGE" reticulum-bolt-on-sanitize --source /source/config --output-dir /observer-data
  sudo grep -q '"ok": true' "$root/explicit-output/refresh-status.json"
  if sudo grep -R -F explicit-ci-rpc-secret "$root/explicit-output"; then exit 1; fi
fi
