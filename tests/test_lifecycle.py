from reticulum_bolt_on import lifecycle


def _windows_environment(monkeypatch, tmp_path):
    monkeypatch.setattr(lifecycle.sys, "platform", "win32")
    monkeypatch.setenv("USERNAME", "rodney")
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    config = tmp_path / ".reticulum" / "config"
    config.parent.mkdir(); config.write_text("[reticulum]\n")
    executable = tmp_path / "Scripts" / "rnsd.exe"
    executable.parent.mkdir(); executable.write_text("stub")
    return config, executable


def test_windows_lifecycle_identifies_only_one_exact_current_user_chain(monkeypatch, tmp_path):
    config, executable = _windows_environment(monkeypatch, tmp_path)
    records = [
        lifecycle.ProcessRecord(41, 1, str(executable), f'"{executable}"', "rodney"),
        lifecycle.ProcessRecord(42, 41, str(tmp_path / "python.exe"), "python.exe", "rodney"),
        lifecycle.ProcessRecord(99, 1, str(tmp_path / "other-python.exe"), "python.exe job.py", "rodney"),
    ]
    status = lifecycle.WindowsNativeLifecycle(config, "1.5.7", True, lambda: records).inspect()
    assert status.available and status.executable == str(executable)
    assert status.pids == (41, 42)
    assert 99 not in status.pids


def test_windows_lifecycle_accepts_only_the_exact_supported_version(monkeypatch, tmp_path):
    config, executable = _windows_environment(monkeypatch, tmp_path)
    records = [lifecycle.ProcessRecord(41, 1, str(executable), f'"{executable}"', "rodney")]
    assert lifecycle.WindowsNativeLifecycle(config, "rnstatus 1.5.7", True, lambda: records).inspect().available
    assert not lifecycle.WindowsNativeLifecycle(config, "1.5.70", True, lambda: records).inspect().available


def test_windows_lifecycle_refuses_ambiguous_or_unrelated_processes(monkeypatch, tmp_path):
    config, executable = _windows_environment(monkeypatch, tmp_path)
    roots = [
        lifecycle.ProcessRecord(41, 1, str(executable), f'"{executable}"', "rodney"),
        lifecycle.ProcessRecord(51, 1, str(executable), f'"{executable}"', "rodney"),
    ]
    assert not lifecycle.WindowsNativeLifecycle(config, "1.5.7", True, lambda: roots).inspect().available
    foreign = [lifecycle.ProcessRecord(41, 1, str(executable), f'"{executable}"', "someone-else")]
    assert not lifecycle.WindowsNativeLifecycle(config, "1.5.7", True, lambda: foreign).inspect().available


def test_windows_restart_stops_exact_chain_once_and_launches_exact_executable(monkeypatch, tmp_path):
    config, executable = _windows_environment(monkeypatch, tmp_path)
    state = {"rows": [lifecycle.ProcessRecord(41, 1, str(executable), f'"{executable}"', "rodney")]}
    adapter = lifecycle.WindowsNativeLifecycle(config, "1.5.7", True, lambda: state["rows"], lambda _: None)
    status = adapter.inspect(); calls = []

    def stop(pids):
        calls.append(("stop", pids)); state["rows"] = []; return True

    def launch(found):
        calls.append(("launch", found.executable, found.arguments))
        state["rows"] = [lifecycle.ProcessRecord(55, 1, str(executable), f'"{executable}"', "rodney")]
        return True

    monkeypatch.setattr(adapter, "_terminate", stop)
    monkeypatch.setattr(adapter, "_launch", launch)
    result = adapter.restart(status, lambda: True, timeout=1)
    assert result.success and result.old_stopped and result.replacement_started and result.shared_returned
    assert calls == [("stop", (41,)), ("launch", str(executable), ())]


def test_windows_restart_does_not_retry_after_failed_launch(monkeypatch, tmp_path):
    config, executable = _windows_environment(monkeypatch, tmp_path)
    state = {"rows": [lifecycle.ProcessRecord(41, 1, str(executable), f'"{executable}"', "rodney")]}
    adapter = lifecycle.WindowsNativeLifecycle(config, "1.5.7", True, lambda: state["rows"], lambda _: None)
    status = adapter.inspect(); attempts = []
    monkeypatch.setattr(adapter, "_terminate", lambda pids: state.update(rows=[]) or True)
    monkeypatch.setattr(adapter, "_launch", lambda found: attempts.append(found.executable) or False)
    result = adapter.restart(status, lambda: False, timeout=1)
    assert not result.success and result.old_stopped and not result.replacement_started
    assert attempts == [str(executable)]


def test_windows_launch_uses_no_console_flag(monkeypatch, tmp_path):
    config, executable = _windows_environment(monkeypatch, tmp_path)
    adapter = lifecycle.WindowsNativeLifecycle(config, "1.5.7", True)
    status = lifecycle.LifecycleStatus(True, "Native Windows", "verified", str(executable))
    seen = {}
    monkeypatch.setattr(lifecycle.subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    monkeypatch.setattr(lifecycle.subprocess, "Popen", lambda command, **kwargs: seen.update(command=command, **kwargs))
    assert adapter._launch(status)
    assert seen["command"] == [str(executable)]
    assert seen["creationflags"] == 0x08000000
