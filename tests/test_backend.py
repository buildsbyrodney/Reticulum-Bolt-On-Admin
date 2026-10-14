import json
import subprocess

from reticulum_bolt_on.backend import SharedInstanceBackend


def test_shared_instance_attachment_and_no_fallback(tmp_path):
    config = tmp_path / "config"; config.write_text("[reticulum]\n[[LAN]]\ntype = TCPServerInterface\nenabled = yes\n")
    calls = []
    def runner(command):
        calls.append(command)
        if command[0] == "rnstatus": return subprocess.CompletedProcess(command, 0, json.dumps({"rns_version":"1.0","interfaces":[]}), "")
        return subprocess.CompletedProcess(command, 0, '{"paths":[]}', "")
    backend = SharedInstanceBackend(config, runner=runner)
    snap = backend.runtime_status()
    assert snap.available and snap.running
    assert calls == [["rnstatus","--config",str(tmp_path),"--json"],["rnpath","--config",str(tmp_path),"--table","--json"]]
    assert not any("rnsd" in call for command in calls for call in command)


def test_failed_attachment_reports_unavailable_and_starts_nothing(tmp_path):
    calls=[]
    def runner(command): calls.append(command); return subprocess.CompletedProcess(command, 1, "", "no shared instance")
    snap = SharedInstanceBackend(tmp_path/"config", runner=runner).runtime_status()
    assert not snap.available and "unavailable" in snap.error.lower()
    assert calls == [["rnstatus","--config",str(tmp_path),"--json"]]


def test_config_view_redacts_ifac_and_never_writes(tmp_path):
    config=tmp_path/"config"; config.write_text("[reticulum]\n  shared_instance_type = tcp\n[[LAN]]\ntype=TCPServerInterface\nifac_netkey = secret\n")
    before=config.read_bytes(); view=SharedInstanceBackend(config).config_view()
    assert view["available"] and view["interfaces"][0]["ifac_present"]
    assert config.read_bytes()==before
    assert SharedInstanceBackend(config).capabilities()["edit_config"] is False


