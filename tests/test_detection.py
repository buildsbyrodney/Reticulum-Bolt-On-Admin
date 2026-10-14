import subprocess
from pathlib import Path

from reticulum_bolt_on.detection import detect


def runner_factory(status=True, systemd=False, process=False, docker=False):
    calls = []
    def run(command):
        calls.append(command)
        if command[0] == "rnstatus" and "--json" in command: return subprocess.CompletedProcess(command, 0 if status else 1, '{"interfaces":[]}' if status else "", "")
        if command[:2] == ["rnstatus", "--version"]: return subprocess.CompletedProcess(command, 0, "1.0.0", "")
        if command[0] == "systemctl": return subprocess.CompletedProcess(command, 0, "ActiveState=active\nMainPID=42\nUser=rns\n" if systemd else "ActiveState=inactive\n", "")
        if command[0] == "ps": return subprocess.CompletedProcess(command, 0, "rns /usr/bin/rnsd\n" if process else "", "")
        if command[0] == "docker": return subprocess.CompletedProcess(command, 0, "rns reticulum rnsd\n" if docker else "", "")
        raise AssertionError(command)
    return run, calls


def test_detects_existing_host_native_without_modifying(tmp_path):
    config = tmp_path / ".reticulum" / "config"; config.parent.mkdir(); config.write_text("[reticulum]\n  instance_control_port = 37428\n")
    before = config.read_bytes(); runner, calls = runner_factory(status=True, process=True)
    result = detect(paths=[config], runner=runner)
    assert result.selected.config_path == config.resolve()
    assert result.selected.storage_path == config.parent / "storage"
    assert result.selected.deployment_type == "manual/shared-instance"
    assert result.selected.shared_instance_available is True
    assert config.read_bytes() == before
    assert not any(call[0] == "rnsd" for call in calls)


def test_ambiguous_candidates_fail_safely(tmp_path):
    configs = []
    for name in ("one", "two"):
        path = tmp_path / name / "config"; path.parent.mkdir(); path.write_text("[reticulum]\n"); configs.append(path)
    runner, calls = runner_factory(status=False)
    result = detect(paths=configs, runner=runner)
    assert result.ambiguous and result.selected is None
    assert "selection" in result.message.lower()
    assert not any(call[0] == "rnsd" for call in calls)


def test_detects_systemd_and_docker_evidence(tmp_path):
    config = tmp_path / "config"; config.write_text("[reticulum]\n")
    runner, _ = runner_factory(systemd=True, process=True, docker=True)
    result = detect(paths=[config], runner=runner)
    assert result.selected.deployment_type == "docker"
    assert result.selected.rnsd_running is True


def test_explicit_storage_path_is_read_only(tmp_path):
    config = tmp_path / "config"; config.write_text("[reticulum]\nstoragepath = existing-data\n")
    runner, _ = runner_factory()
    result = detect(paths=[config], runner=runner)
    assert result.selected.storage_path == tmp_path / "existing-data"
    assert not (tmp_path / "existing-data").exists()


