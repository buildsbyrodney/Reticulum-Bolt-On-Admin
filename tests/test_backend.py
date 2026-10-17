import json
import subprocess

from reticulum_bolt_on import backend as backend_module
from reticulum_bolt_on.backend import RNSManagementClient, SharedInstanceBackend
from reticulum_bolt_on.models import ManagementResult


def test_shared_instance_attachment_and_no_fallback(monkeypatch, tmp_path):
    monkeypatch.setattr(backend_module.sys, "platform", "linux")
    config = tmp_path / "config"; config.write_text("[reticulum]\n[[LAN]]\ntype = TCPServerInterface\nenabled = yes\n")
    calls = []
    def runner(command):
        calls.append(command)
        if command[0] == "rnstatus": return subprocess.CompletedProcess(command, 0, json.dumps({"rns_version":"1.0","interfaces":[]}), "")
        return subprocess.CompletedProcess(command, 0, '{"paths":[]}', "")
    manager = FakeManager(paths=[])
    backend = SharedInstanceBackend(config, runner=runner, manager=manager)
    snap = backend.runtime_status()
    assert snap.available and snap.running
    assert calls == [["rnstatus","--config",str(tmp_path),"--json"]]
    assert manager.path_reads == 1
    assert not any("rnsd" in call for command in calls for call in command)


def test_failed_attachment_reports_unavailable_and_starts_nothing(monkeypatch, tmp_path):
    monkeypatch.setattr(backend_module.sys, "platform", "linux")
    calls=[]
    def runner(command): calls.append(command); return subprocess.CompletedProcess(command, 1, "", "no shared instance")
    snap = SharedInstanceBackend(tmp_path/"config", runner=runner).runtime_status()
    assert not snap.available and snap.running is None and "unavailable" in snap.error.lower()
    assert calls == [["rnstatus","--config",str(tmp_path),"--json"]]
    assert not any("rnsd" in argument for command in calls for argument in command)


def test_config_view_redacts_ifac_and_never_writes(tmp_path):
    config=tmp_path/"config"; config.write_text("[reticulum]\n  shared_instance_type = tcp\n  network_name = private-name\n  passphrase = global-secret\n  ifac_netkey = key-secret\n[[LAN]]\ntype=TCPServerInterface\nifac_netkey = interface-secret\n")
    before=config.read_bytes(); view=SharedInstanceBackend(config).config_view()
    assert view["available"] and view["interfaces"][0]["ifac_present"]
    assert config.read_bytes()==before
    rendered=json.dumps(view)
    for secret in ("private-name", "global-secret", "key-secret", "interface-secret"):
        assert secret not in rendered
    assert rendered.count("[present, hidden]") == 3
    assert SharedInstanceBackend(config).capabilities()["edit_config"] is False


class FakeManager:
    available = True

    def __init__(self, success=True, paths=None):
        self.success = success
        self.calls = []
        self.paths = paths or []
        self.path_reads = 0

    def execute(self, action, target=None):
        self.calls.append((action, target))
        return ManagementResult(self.success, "completed" if self.success else "rejected")

    def read_paths(self):
        self.path_reads += 1
        return {"paths": self.paths}, None

    def read_runtime(self):
        return {"rns_version": "1.5.7", "shared_instance_available": True,
                "transport_enabled": None, "interfaces": []}, None


def test_native_management_is_capability_gated_and_refreshes_paths(monkeypatch, tmp_path):
    monkeypatch.setattr(backend_module.sys, "platform", "linux")
    config = tmp_path / "config"
    config.write_text("[reticulum]\n")
    calls = []

    def runner(command):
        calls.append(command)
        if command[0] == "rnstatus":
            return subprocess.CompletedProcess(command, 0, '{"interfaces":[]}', "")
        return subprocess.CompletedProcess(command, 0, '{"paths":[]}', "")

    manager = FakeManager()
    backend = SharedInstanceBackend(config, runner=runner, manager=manager)
    assert backend.capabilities()["drop_path"] is True
    result = backend.drop_path("a" * 32)

    assert result.success and result.metadata["path_state_refreshed"] is True
    assert manager.calls == [("drop-path", "a" * 32)]
    assert calls == [
        ["rnstatus", "--config", str(tmp_path), "--json"],
    ]
    assert not any("rnsd" in argument for command in calls for argument in command)


def test_windows_runtime_uses_shared_helper_and_never_invokes_rnstatus(monkeypatch, tmp_path):
    monkeypatch.setattr(backend_module.sys, "platform", "win32")
    config = tmp_path / "config"
    config.write_text("[reticulum]\n[[WSL Test Client]]\ntype = TCPClientInterface\n")
    manager = FakeManager()
    manager.read_runtime = lambda: ({
        "rns_version": "1.5.7", "shared_instance_available": True,
        "transport_enabled": None,
        "interfaces": [{"name": "WSL Test Client", "short_name": "WSL Test Client",
                        "type": "TCPClientInterface", "status": True, "mode": 1,
                        "rxb": 12, "txb": 34, "ifac_present": True}],
    }, None)

    def runner(command):
        raise AssertionError(f"Windows runtime observation must not invoke {command}")

    snap = SharedInstanceBackend(config, runner=runner, manager=manager).runtime_status()
    assert snap.available and snap.rns_version == "1.5.7" and snap.transport_enabled is None
    assert snap.interfaces[0]["name"] == "WSL Test Client"
    assert snap.interfaces[0]["rx_bytes"] == 12 and snap.interfaces[0]["ifac_present"] is True
    assert manager.path_reads == 1


def test_windows_runtime_helper_failure_disables_shared_instance_and_lifecycle(monkeypatch, tmp_path):
    monkeypatch.setattr(backend_module.sys, "platform", "win32")
    config = tmp_path / "config"; config.write_text("[reticulum]\n")
    manager = FakeManager()
    manager.read_runtime = lambda: (None, "Existing Reticulum shared-instance attachment is unavailable")
    backend = SharedInstanceBackend(config, runner=lambda command: (_ for _ in ()).throw(AssertionError(command)), manager=manager)
    assert not backend.runtime_status().available
    assert not backend.lifecycle_status().available


def test_management_backend_rejects_invalid_hash_before_execution(tmp_path):
    config = tmp_path / "config"
    config.write_text("[reticulum]\n")
    manager = FakeManager()
    backend = SharedInstanceBackend(config, manager=manager)

    assert not backend.drop_path("not-a-hash").success
    assert not backend.drop_paths_via("").success
    assert manager.calls == []


def test_management_capability_probe_requires_exact_supported_runtime(tmp_path):
    interpreter = tmp_path / "python"
    interpreter.write_text("")
    interpreter.chmod(0o755)
    calls = []

    def runner(command):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0,
            '{"success":true,"message":"probe","rns_version":"1.5.7","capabilities":["drop-path"]}\n', "")

    client = RNSManagementClient(tmp_path, interpreter=interpreter, runner=runner)
    assert client.supports("drop-path") is True
    assert client.supports("drop-via") is False
    assert len(calls) == 1 and calls[0][-1] == "probe"


def test_runtime_helper_client_accepts_only_valid_shared_instance_payload(tmp_path):
    interpreter = tmp_path / "python"; interpreter.write_text(""); interpreter.chmod(0o755)

    def runner(command):
        assert command[-1] == "read-runtime"
        return subprocess.CompletedProcess(command, 0,
            '{"success":true,"runtime":{"rns_version":"1.5.7","shared_instance_available":true,"transport_enabled":null,"interfaces":[]}}\n', "")

    runtime, error = RNSManagementClient(tmp_path, interpreter=interpreter, runner=runner).read_runtime()
    assert error is None and runtime["shared_instance_available"] is True


def test_management_interpreter_discovery_supports_env_python_shebang(tmp_path, monkeypatch):
    utility = tmp_path / "rnstatus"
    interpreter = tmp_path / "python3"
    utility.write_text("#!/usr/bin/env python3\n")
    interpreter.write_text("")
    utility.chmod(0o755)
    interpreter.chmod(0o755)

    def which(command):
        return str(utility if command == "rnstatus" else interpreter) if command in {"rnstatus", "python3"} else None

    monkeypatch.setattr("reticulum_bolt_on.backend.shutil.which", which)
    assert RNSManagementClient._discover_interpreter() == interpreter

