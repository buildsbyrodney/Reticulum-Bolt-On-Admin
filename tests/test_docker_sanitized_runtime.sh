#!/usr/bin/env bash
set -euo pipefail

IMAGE=${1:?image tag required}
PLATFORM=${2:?platform required}
root=$(mktemp -d)
suffix=${RANDOM}-$$
production=bolt-on-rnsd-$suffix
observer=bolt-on-observer-$suffix
failure=bolt-on-failure-$suffix
cleanup() {
  docker rm -f "$failure" "$observer" "$production" >/dev/null 2>&1 || true
  case "$root" in /tmp/*|"${RUNNER_TEMP:-/nonexistent}"/*) sudo rm -rf -- "$root";; esac
}
trap cleanup EXIT

mkdir -p "$root/production" "$root/observer-data" "$root/gui-state" "$root/failure-state"
printf '%s\n' '[reticulum]' '  share_instance = Yes' '  instance_name = bolt-on-ci' '  enable_transport = No' '' '[[Protected]]' '  type = AutoInterface' '  enabled = No' '  network_name = ci-private-network' '  passphrase = ci-never-copy-secret' > "$root/production/config"
sudo chown -R root:root "$root/production" "$root/observer-data"
sudo chmod 0755 "$root/production" "$root/observer-data"
sudo chmod 0600 "$root/production/config"
sudo chown 10001:10001 "$root/gui-state" "$root/failure-state"
sudo chmod 0700 "$root/gui-state" "$root/failure-state"

docker run -d --name "$production" --platform "$PLATFORM" --network host --read-only --cap-drop ALL --security-opt no-new-privileges --tmpfs /tmp:rw,noexec,nosuid -v "$root/production:/production" --user 0:0 "$IMAGE" rnsd --config /production --service >/dev/null
for attempt in $(seq 1 30); do
  docker exec "$production" rnstatus --config /production --json >/dev/null 2>&1 && break
  test "$attempt" -lt 30
  sleep 1
done

config_before=$(sudo sha256sum "$root/production/config" | cut -d' ' -f1)
storage_before=$(sudo find "$root/production/storage" -type f -print0 | sudo sort -z | sudo xargs -0 sha256sum | sha256sum)
started_before=$(docker inspect "$production" --format '{{.State.StartedAt}}')
restart_before=$(docker inspect "$production" --format '{{.RestartCount}}')

docker run --rm --platform "$PLATFORM" --network none --read-only --cap-drop ALL --security-opt no-new-privileges --tmpfs /tmp:rw,noexec,nosuid --user 0:0 -v "$root/production:/source:ro" -v "$root/observer-data:/observer-data" "$IMAGE" reticulum-bolt-on-sanitize --source /source/config --output-dir /observer-data --source-display-path /root/.reticulum/config --storage-display-path /root/.reticulum/storage --deployment-type docker

test "$(sudo stat -c '%a:%u:%g' "$root/production/config")" = "600:0:0"
test "$(sudo sha256sum "$root/production/config" | cut -d' ' -f1)" = "$config_before"
sudo grep -R -F 'ci-never-copy-secret' "$root/observer-data" && exit 1 || true
sudo grep -R -F 'ci-private-network' "$root/observer-data" && exit 1 || true
sudo grep -F 'instance_name = bolt-on-ci' "$root/observer-data/rns/config"
sudo test "$(find "$root/observer-data" -type f -perm /022 | wc -l)" = 0

docker run -d --name "$observer" --platform "$PLATFORM" --network host --read-only --cap-drop ALL --security-opt no-new-privileges --tmpfs /tmp:rw,noexec,nosuid -e BOLT_ON_SECRET_KEY=ci-secret -e BOLT_ON_DATABASE=/state/observer.db -e RETICULUM_CONFIG=/observer-data/rns/config -e BOLT_ON_CONFIG_SNAPSHOT=/observer-data/config-view.json -v "$root/observer-data:/observer-data:ro" -v "$root/gui-state:/state" "$IMAGE" >/dev/null
for attempt in $(seq 1 30); do
  docker exec "$observer" rnstatus --config /observer-data/rns --json >/dev/null 2>&1 && break
  test "$attempt" -lt 30
  sleep 1
done
docker exec "$observer" rnpath --config /observer-data/rns --table --json >/dev/null
docker exec "$observer" test -r /observer-data/config-view.json
test "$(docker inspect "$observer" --format '{{.Config.User}}')" = observer
docker exec "$observer" python -c 'import pathlib; processes=[p.read_bytes().replace(b"\0",b" ").decode("utf-8","ignore") for p in pathlib.Path("/proc").glob("[0-9]*/cmdline")]; assert not [p for p in processes if any(pathlib.Path(t).name == "rnsd" for t in p.split())]'

test "$(sudo sha256sum "$root/production/config" | cut -d' ' -f1)" = "$config_before"
test "$(sudo find "$root/production/storage" -type f -print0 | sudo sort -z | sudo xargs -0 sha256sum | sha256sum)" = "$storage_before"
test "$(docker inspect "$production" --format '{{.State.StartedAt}}')" = "$started_before"
test "$(docker inspect "$production" --format '{{.RestartCount}}')" = "$restart_before"

docker rm -f "$observer" >/dev/null
docker exec "$production" rnstatus --config /production --json >/dev/null

docker run -d --name "$failure" --platform "$PLATFORM" --network none --read-only --cap-drop ALL --security-opt no-new-privileges --tmpfs /tmp:rw,noexec,nosuid -e BOLT_ON_SECRET_KEY=ci-secret -e BOLT_ON_DATABASE=/state/observer.db -e RETICULUM_CONFIG=/observer-data/rns/config -e BOLT_ON_CONFIG_SNAPSHOT=/observer-data/config-view.json -v "$root/observer-data:/observer-data:ro" -v "$root/failure-state:/state" "$IMAGE" >/dev/null
for attempt in $(seq 1 20); do
  docker exec "$failure" python -c 'from pathlib import Path; from reticulum_bolt_on.backend import SharedInstanceBackend; s=SharedInstanceBackend(Path("/observer-data/rns/config")).runtime_status(); assert not s.available and s.running is None and "unavailable" in s.error.lower()' && break
  test "$attempt" -lt 20
  sleep 1
done
docker exec "$failure" python -c 'import pathlib; processes=[p.read_bytes().replace(b"\0",b" ").decode("utf-8","ignore") for p in pathlib.Path("/proc").glob("[0-9]*/cmdline")]; assert not [p for p in processes if any(pathlib.Path(t).name == "rnsd" for t in p.split())]'
docker rm -f "$failure" >/dev/null
docker exec "$production" rnstatus --config /production --json >/dev/null
test "$(docker inspect "$production" --format '{{.State.StartedAt}}')" = "$started_before"
test "$(docker inspect "$production" --format '{{.RestartCount}}')" = "$restart_before"
test "$(sudo sha256sum "$root/production/config" | cut -d' ' -f1)" = "$config_before"
test "$(sudo find "$root/production/storage" -type f -print0 | sudo sort -z | sudo xargs -0 sha256sum | sha256sum)" = "$storage_before"
