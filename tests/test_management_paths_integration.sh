#!/usr/bin/env bash
set -euo pipefail

IMAGE=${1:?image tag required}
PLATFORM=${2:?platform required}
root=$(mktemp -d)
suffix=${RANDOM}-$$
upstream_a=bolt-on-path-upstream-a-$suffix
upstream_b=bolt-on-path-upstream-b-$suffix
production=bolt-on-path-production-$suffix
peer_a=bolt-on-path-peer-a-$suffix
peer_a2=bolt-on-path-peer-a2-$suffix
peer_a3=bolt-on-path-peer-a3-$suffix
peer_b=bolt-on-path-peer-b-$suffix
manager=bolt-on-path-manager-$suffix
containers=("$manager" "$peer_a" "$peer_a2" "$peer_a3" "$peer_b" "$production" "$upstream_a" "$upstream_b")

cleanup() {
  docker rm -f "${containers[@]}" >/dev/null 2>&1 || true
  case "$root" in /tmp/*|"${RUNNER_TEMP:-/nonexistent}"/*) sudo rm -rf -- "$root";; esac
}
trap cleanup EXIT
diagnose() {
  local status=$?
  for container in "${containers[@]}"; do
    if docker inspect "$container" >/dev/null 2>&1; then
      echo "--- diagnostic log: $container ---" >&2
      docker inspect --format 'status={{.State.Status}} exit={{.State.ExitCode}} error={{.State.Error}}' "$container" >&2 || true
      docker logs --tail 80 "$container" >&2 || true
    fi
  done
  exit "$status"
}
trap diagnose ERR

mkdir -p "$root/upstream-a" "$root/upstream-b" "$root/production" \
  "$root/output-a" "$root/output-a2" "$root/output-a3" "$root/output-b"
printf '%s\n' \
  '[reticulum]' '  share_instance = Yes' '  instance_name = bolt-on-path-upstream-a' '  enable_transport = Yes' \
  '[interfaces]' '  [[CI TCP Client A]]' '    type = TCPClientInterface' '    enabled = Yes' \
  '    target_host = 127.0.0.1' '    target_port = 43241' > "$root/upstream-a/config"
printf '%s\n' \
  '[reticulum]' '  share_instance = Yes' '  instance_name = bolt-on-path-upstream-b' '  enable_transport = Yes' \
  '[interfaces]' '  [[CI TCP Client B]]' '    type = TCPClientInterface' '    enabled = Yes' \
  '    target_host = 127.0.0.1' '    target_port = 43241' > "$root/upstream-b/config"
printf '%s\n' \
  '[reticulum]' '  share_instance = Yes' '  instance_name = bolt-on-path-production' '  enable_transport = No' \
  '[interfaces]' '  [[CI TCP Server]]' '    type = TCPServerInterface' '    enabled = Yes' \
  '    listen_ip = 127.0.0.1' '    listen_port = 43241' > "$root/production/config"
chmod 0600 "$root/upstream-a/config" "$root/upstream-b/config" "$root/production/config"
# The hardened rnsd containers run as uid 0 without DAC-bypass capabilities, so
# only their private configuration trees are root-owned. Destination hashes are
# non-secret test coordination data and remain readable by the CI controller.
chmod 0711 "$root"
chmod 0777 "$root/output-a" "$root/output-a2" "$root/output-a3" "$root/output-b"
sudo chown -R root:root "$root/upstream-a" "$root/upstream-b" "$root/production"

run_rnsd() {
  local name=$1 directory=$2
  docker run -d --name "$name" --platform "$PLATFORM" --network host --read-only --cap-drop ALL \
    --security-opt no-new-privileges --tmpfs /tmp:rw,noexec,nosuid -v "$directory:/rns" \
    --user 0:0 "$IMAGE" rnsd --config /rns --service >/dev/null
}
wait_socket() {
  local instance=$1
  for attempt in $(seq 1 60); do
    python3 -c 'import socket,sys; s=socket.socket(socket.AF_UNIX); s.connect(("\0rns/"+sys.argv[1]).encode())' "$instance" >/dev/null 2>&1 && return
    if test "$attempt" -ge 60; then
      echo "Timed out waiting for shared instance: $instance" >&2
      return 1
    fi
    sleep 1
  done
}
run_rnsd "$production" "$root/production"
wait_socket bolt-on-path-production
run_rnsd "$upstream_a" "$root/upstream-a"
run_rnsd "$upstream_b" "$root/upstream-b"
wait_socket bolt-on-path-upstream-a
wait_socket bolt-on-path-upstream-b

docker run -d --name "$manager" --platform "$PLATFORM" --network host --read-only --cap-drop ALL \
  --security-opt no-new-privileges --tmpfs /tmp:rw,noexec,nosuid -v "$root/production:/management:ro" \
  --user 0:0 "$IMAGE" sleep infinity >/dev/null

fixture_path="$PWD/tests/rns_path_fixture.py"
docker run -d --name "$peer_a" --platform "$PLATFORM" --network host --read-only --cap-drop ALL \
  --security-opt no-new-privileges --tmpfs /tmp:rw,noexec,nosuid -v "$root/upstream-a:/upstream:ro" \
  -v "$root/output-a:/output" -v "$fixture_path:/fixture.py:ro" --user 0:0 "$IMAGE" \
  python /fixture.py --config /upstream --output /output/destinations.json --count 1 --prefix via-a-1 >/dev/null
docker run -d --name "$peer_a2" --platform "$PLATFORM" --network host --read-only --cap-drop ALL \
  --security-opt no-new-privileges --tmpfs /tmp:rw,noexec,nosuid -v "$root/upstream-a:/upstream:ro" \
  -v "$root/output-a2:/output" -v "$fixture_path:/fixture.py:ro" --user 0:0 "$IMAGE" \
  python /fixture.py --config /upstream --output /output/destinations.json --count 1 --prefix via-a-2 >/dev/null
docker run -d --name "$peer_a3" --platform "$PLATFORM" --network host --read-only --cap-drop ALL \
  --security-opt no-new-privileges --tmpfs /tmp:rw,noexec,nosuid -v "$root/upstream-a:/upstream:ro" \
  -v "$root/output-a3:/output" -v "$fixture_path:/fixture.py:ro" --user 0:0 "$IMAGE" \
  python /fixture.py --config /upstream --output /output/destinations.json --count 1 --prefix via-a-3 >/dev/null
docker run -d --name "$peer_b" --platform "$PLATFORM" --network host --read-only --cap-drop ALL \
  --security-opt no-new-privileges --tmpfs /tmp:rw,noexec,nosuid -v "$root/upstream-b:/upstream:ro" \
  -v "$root/output-b:/output" -v "$fixture_path:/fixture.py:ro" --user 0:0 "$IMAGE" \
  python /fixture.py --config /upstream --output /output/destinations.json --count 1 --prefix via-b >/dev/null

for attempt in $(seq 1 60); do
  test -s "$root/output-a/destinations.json" && test -s "$root/output-a2/destinations.json" && \
    test -s "$root/output-a3/destinations.json" && test -s "$root/output-b/destinations.json" && break
  if test "$attempt" -ge 60; then
    echo "Timed out waiting for destination fixtures" >&2
    exit 1
  fi
  sleep 1
done
a1=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["destinations"][0])' "$root/output-a/destinations.json")
a2=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["destinations"][0])' "$root/output-a2/destinations.json")
a3=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["destinations"][0])' "$root/output-a3/destinations.json")
b1=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["destinations"][0])' "$root/output-b/destinations.json")

read_paths() {
  docker exec "$manager" python /app/src/reticulum_bolt_on/management_helper.py --config /management read-paths | tail -n 1
}
wait_for_state() {
  local mode=$1; shift
  local payload
  for attempt in $(seq 1 90); do
    payload=$(read_paths || true)
    if python3 -c 'import json,sys
d=json.loads(sys.argv[1]); present={row.get("hash") for row in d.get("paths",[])}; mode=sys.argv[2]; targets=set(sys.argv[3:]);
raise SystemExit(0 if ((targets <= present) if mode == "present" else targets.isdisjoint(present)) else 1)' "$payload" "$mode" "$@"; then
      return
    fi
    if test "$attempt" -ge 90; then
      echo "Timed out waiting for paths to become $mode: $*" >&2
      echo "Last bounded path snapshot: $payload" >&2
      return 1
    fi
    sleep 1
  done
}

wait_for_state present "$a1" "$a2" "$a3" "$b1"
initial=$(read_paths)
via_a=$(python3 -c 'import json,sys; d=json.loads(sys.argv[1]); targets=set(sys.argv[2:5]); vias={r.get("via") for r in d["paths"] if r.get("hash") in targets}; assert len(vias)==1; print(vias.pop())' "$initial" "$a1" "$a2" "$a3")
via_b=$(python3 -c 'import json,sys; d=json.loads(sys.argv[1]); target=sys.argv[2]; print(next(r["via"] for r in d["paths"] if r.get("hash")==target))' "$initial" "$b1")
python3 -c 'import re,sys; assert re.fullmatch(r"[0-9a-f]{32}",sys.argv[1]); assert re.fullmatch(r"[0-9a-f]{32}",sys.argv[2]); assert sys.argv[1] != sys.argv[2]' "$via_a" "$via_b"

config_before=$(sudo sha256sum "$root/production/config" | cut -d' ' -f1)
identity_before=$(sudo sha256sum "$root/production/storage/transport_identity" | cut -d' ' -f1)
pid_before=$(docker inspect "$production" --format '{{.State.Pid}}')
started_before=$(docker inspect "$production" --format '{{.State.StartedAt}}')
restart_before=$(docker inspect "$production" --format '{{.RestartCount}}')

docker exec "$manager" python /app/src/reticulum_bolt_on/management_helper.py --config /management drop-path "$a1"
wait_for_state absent "$a1"
after_one=$(read_paths)
python3 -c 'import json,sys; p={r.get("hash") for r in json.loads(sys.argv[1])["paths"]}; assert sys.argv[2] not in p; assert set(sys.argv[3:]) <= p' "$after_one" "$a1" "$a2" "$a3" "$b1"

printf '%s\n' "$a1" > "$root/output-a/reannounce"
wait_for_state present "$a1" "$a2" "$a3" "$b1"

docker exec "$manager" python /app/src/reticulum_bolt_on/management_helper.py --config /management drop-via "$via_a"
wait_for_state absent "$a1" "$a2" "$a3"
after_via=$(read_paths)
python3 -c 'import json,sys; p={r.get("hash") for r in json.loads(sys.argv[1])["paths"]}; assert set(sys.argv[2:5]).isdisjoint(p); assert sys.argv[5] in p' "$after_via" "$a1" "$a2" "$a3" "$b1"

test "$(sudo sha256sum "$root/production/config" | cut -d' ' -f1)" = "$config_before"
# RNS itself rewrites volatile destination-table, packet-hash and announce-cache
# files while this test intentionally learns, drops and relearns routes. The
# management boundary must still preserve the long-lived transport identity.
test "$(sudo sha256sum "$root/production/storage/transport_identity" | cut -d' ' -f1)" = "$identity_before"
test "$(docker inspect "$production" --format '{{.State.Pid}}')" = "$pid_before"
test "$(docker inspect "$production" --format '{{.State.StartedAt}}')" = "$started_before"
test "$(docker inspect "$production" --format '{{.RestartCount}}')" = "$restart_before"
