import subprocess

from reticulum_bolt_on import subprocess_runner


def test_windows_runner_suppresses_console_without_changing_capture_contract(monkeypatch):
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, "ok", "")

    monkeypatch.setattr(subprocess_runner.sys, "platform", "win32")
    monkeypatch.setattr(subprocess_runner.subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    monkeypatch.setattr(subprocess_runner.subprocess, "run", fake_run)

    result = subprocess_runner.run_command(["rnstatus", "--json"], timeout=8)

    assert result.returncode == 0
    assert calls == [(["rnstatus", "--json"], {
        "capture_output": True, "text": True, "timeout": 8, "check": False,
        "creationflags": 0x08000000,
    })]


def test_non_windows_runner_preserves_existing_subprocess_flags(monkeypatch):
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, "ok", "")

    monkeypatch.setattr(subprocess_runner.sys, "platform", "linux")
    monkeypatch.setattr(subprocess_runner.subprocess, "run", fake_run)

    subprocess_runner.run_command(["rnstatus", "--json"], timeout=8)

    assert calls == [(["rnstatus", "--json"], {
        "capture_output": True, "text": True, "timeout": 8, "check": False,
    })]


def test_all_bolt_on_helper_runners_use_the_shared_boundary(monkeypatch):
    calls = []

    def fake_run(command, *, timeout):
        calls.append((command, timeout))
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("reticulum_bolt_on.backend.run_command", fake_run)
    monkeypatch.setattr("reticulum_bolt_on.detection.run_command", fake_run)
    monkeypatch.setattr("reticulum_bolt_on.sanitizer.run_command", fake_run)

    from reticulum_bolt_on.backend import command_runner
    from reticulum_bolt_on.detection import default_runner
    from reticulum_bolt_on.sanitizer import _command_runner

    command_runner(["rnstatus"])
    default_runner(["rnpath"])
    _command_runner(["python", "management_helper.py"])

    assert calls == [
        (["rnstatus"], 8),
        (["rnpath"], 4),
        (["python", "management_helper.py"], 20),
    ]
