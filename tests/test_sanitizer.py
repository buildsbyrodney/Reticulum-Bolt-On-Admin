import hashlib
import json
import stat
import subprocess
from pathlib import Path

import pytest

from reticulum_bolt_on.backend import SharedInstanceBackend
from reticulum_bolt_on.sanitizer import UnsafeObserverConfiguration, sanitize_config


PRODUCTION_CONFIG = """[reticulum]
  share_instance = Yes
  instance_name = production-node
  enable_transport = Yes

[[Protected TCP]]
  type = TCPServerInterface
  enabled = Yes
  mode = gateway
  network_name = private-network
  passphrase = never-copy-this
  ifac_netkey = also-never-copy-this
"""


def test_root_owned_style_config_is_sanitized_without_source_changes(tmp_path):
    source = tmp_path / "production" / "config"
    source.parent.mkdir()
    source.write_text(PRODUCTION_CONFIG)
    source.chmod(0o600)
    before = (hashlib.sha256(source.read_bytes()).hexdigest(), stat.S_IMODE(source.stat().st_mode), source.stat().st_uid, source.stat().st_gid)
    output = tmp_path / "observer"

    snapshot = sanitize_config(source, output, source_display_path="/root/.reticulum/config",
                               storage_display_path="/root/.reticulum/storage", deployment_type="docker")

    after = (hashlib.sha256(source.read_bytes()).hexdigest(), stat.S_IMODE(source.stat().st_mode), source.stat().st_uid, source.stat().st_gid)
    assert before == after
    assert snapshot["transport_enabled"] is True
    assert snapshot["interfaces"] == [{"name": "Protected TCP", "type": "TCPServerInterface", "enabled": True,
                                        "mode": "gateway", "ifac_present": True, "source": "configured"}]
    rendered = "\n".join(path.read_text(errors="replace") for path in output.rglob("*") if path.is_file())
    assert "production-node" in rendered
    for secret in ("private-network", "never-copy-this", "also-never-copy-this"):
        assert secret not in rendered
    minimal = (output / "rns" / "config").read_text()
    assert "instance_name = production-node" in minimal and "enable_transport = No" in minimal
    assert "[[" not in minimal


def test_custom_rpc_key_is_rejected_instead_of_copied(tmp_path):
    source = tmp_path / "config"
    source.write_text("[reticulum]\nshare_instance=Yes\nrpc_key=secret-rpc-key\n")
    with pytest.raises(UnsafeObserverConfiguration, match="rpc_key"):
        sanitize_config(source, tmp_path / "observer")
    assert "secret-rpc-key" not in "\n".join(path.read_text() for path in (tmp_path / "observer").rglob("*") if path.is_file())


def test_backend_uses_sanitized_view_and_live_transport_id(tmp_path, monkeypatch):
    source = tmp_path / "production" / "config"; source.parent.mkdir(); source.write_text(PRODUCTION_CONFIG)
    observer = tmp_path / "observer"
    sanitize_config(source, observer, source_display_path="/root/.reticulum/config")
    monkeypatch.setenv("RETICULUM_CONFIG", str(observer / "rns" / "config"))
    calls = []
    def runner(command):
        calls.append(command)
        if command[0] == "rnstatus":
            return subprocess.CompletedProcess(command, 0, json.dumps({"transport_id": "a" * 32, "interfaces": []}), "")
        return subprocess.CompletedProcess(command, 0, "[]", "")
    backend = SharedInstanceBackend(observer / "rns" / "config", runner=runner,
                                    snapshot_path=observer / "config-view.json")
    runtime = backend.runtime_status()
    view = backend.config_view()
    assert runtime.transport_enabled is True
    assert view["sanitized"] and not view["stale"] and view["interfaces"][0]["ifac_present"]
    assert str(backend.detect().selected.config_path).replace("\\", "/").endswith("/root/.reticulum/config")
    assert calls == [["rnstatus", "--config", str(observer / "rns"), "--json"],
                     ["rnpath", "--config", str(observer / "rns"), "--table", "--json"]]
