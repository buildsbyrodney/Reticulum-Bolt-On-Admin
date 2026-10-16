from pathlib import Path

from reticulum_bolt_on.backend import RNSManagementClient


def test_management_can_use_installer_supplied_existing_python(monkeypatch, tmp_path):
    interpreter = tmp_path / "python.exe"
    interpreter.write_text("existing user interpreter")
    interpreter.chmod(0o755)
    monkeypatch.setenv("BOLT_ON_RNS_PYTHON", str(interpreter))
    assert RNSManagementClient._discover_interpreter() == interpreter
