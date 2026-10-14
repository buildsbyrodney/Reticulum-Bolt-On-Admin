import hashlib
import json
import stat
import subprocess

from reticulum_bolt_on.backend import SnapshotBackend
from reticulum_bolt_on.sanitizer import ObservationUnavailable, _write_failure, collect_observation, sanitize_config


PRODUCTION_CONFIG = """[reticulum]
  share_instance = Yes
  instance_name = production-node
  enable_transport = Yes
  rpc_key = explicit-never-copy-rpc-secret

[[Protected TCP]]
  type = TCPServerInterface
  enabled = Yes
  mode = gateway
  network_name = private-network
  passphrase = never-copy-this
  ifac_netkey = also-never-copy-this
"""


def fake_runner(command):
    if command[0] == "rnstatus":
        payload = {
            "rns_version": "1.5.7",
            "transport_id": "a" * 32,
            "interfaces": [{
                "name": "Protected TCP", "type": "TCPServerInterface", "status": True,
                "rxb": 12, "txb": 34, "ifac_netname": "runtime-private-name",
            }],
        }
    else:
        payload = {"paths": [{"hash": "b" * 32, "hops": 2, "via": "c" * 32, "interface": "Protected TCP"}]}
    return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")


def test_root_owned_style_observation_is_sanitized_without_source_changes(tmp_path):
    source = tmp_path / "production" / "config"
    source.parent.mkdir()
    source.write_text(PRODUCTION_CONFIG)
    source.chmod(0o600)
    before = (hashlib.sha256(source.read_bytes()).hexdigest(), stat.S_IMODE(source.stat().st_mode), source.stat().st_uid, source.stat().st_gid)
    output = tmp_path / "observer"

    snapshots = collect_observation(
        source, output, source_display_path="/root/.reticulum/config",
        storage_display_path="/root/.reticulum/storage", deployment_type="docker", runner=fake_runner,
    )

    after = (hashlib.sha256(source.read_bytes()).hexdigest(), stat.S_IMODE(source.stat().st_mode), source.stat().st_uid, source.stat().st_gid)
    assert before == after
    assert snapshots["config"]["transport_enabled"] is True
    assert snapshots["config"]["interfaces"][0]["ifac_present"] is True
    assert snapshots["runtime"]["rns_version"] == "1.5.7"
    assert snapshots["runtime"]["transport_enabled"] is True
    assert snapshots["paths"]["total_count"] == 1
    rendered = "\n".join(path.read_text(errors="replace") for path in output.rglob("*") if path.is_file())
    for secret in (
        "explicit-never-copy-rpc-secret", "private-network", "never-copy-this",
        "also-never-copy-this", "runtime-private-name",
    ):
        assert secret not in rendered


def test_config_only_snapshot_omits_explicit_rpc_key(tmp_path):
    source = tmp_path / "config"
    source.write_text(PRODUCTION_CONFIG)
    snapshot = sanitize_config(source, tmp_path / "observer")
    assert snapshot["interfaces"][0]["ifac_present"] is True
    assert "explicit-never-copy-rpc-secret" not in (tmp_path / "observer" / "config-view.json").read_text()


def test_snapshot_backend_never_invokes_reticulum_utilities(tmp_path):
    source = tmp_path / "production" / "config"
    source.parent.mkdir()
    source.write_text(PRODUCTION_CONFIG)
    output = tmp_path / "observer"
    collect_observation(source, output, source_display_path="/root/.reticulum/config", runner=fake_runner)

    backend = SnapshotBackend(output)
    runtime = backend.runtime_status()
    view = backend.config_view()
    assert runtime.available and runtime.running and runtime.rns_version == "1.5.7"
    assert runtime.paths[0]["via"] == "c" * 32
    assert view["sanitized"] and not view["stale"]
    assert str(backend.detect().selected.config_path).replace("\\", "/") == "/root/.reticulum/config"


def test_helper_failure_marks_old_snapshot_unavailable_without_leaking_error(tmp_path):
    source = tmp_path / "production" / "config"
    source.parent.mkdir()
    source.write_text(PRODUCTION_CONFIG)
    output = tmp_path / "observer"
    collect_observation(source, output, runner=fake_runner)
    _write_failure(output, source, ObservationUnavailable("rnstatus shared-instance query failed with exit code 1"))

    runtime = SnapshotBackend(output).runtime_status()
    assert not runtime.available and runtime.running is None
    assert "rnstatus" in runtime.error
    rendered = (output / "refresh-status.json").read_text()
    assert "explicit-never-copy-rpc-secret" not in rendered
