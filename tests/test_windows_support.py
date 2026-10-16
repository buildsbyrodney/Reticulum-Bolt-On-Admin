from pathlib import Path


ROOT = Path(__file__).parents[1]
INSTALL = (ROOT / "install.ps1").read_text(encoding="utf-8")
UNINSTALL = (ROOT / "uninstall.ps1").read_text(encoding="utf-8")


def test_windows_installer_is_user_scoped_and_preflighted():
    for required in ("$env:LOCALAPPDATA", "Reticulum-Bolt-On-Admin-State", "rnstatus", "rnpath",
                     "RNS.__version__", "1.5.7", "Get-NetTCPConnection", "Register-ScheduledTask",
                     "Administrator      Not required", "rnsd will not be started", "BOLT_ON_RNS_PYTHON", "Find-Python"):
        assert required in INSTALL
    for forbidden in ("Start-Process rnsd", "rnsd.exe", "RunAs", "-Verb RunAs", "choco", "winget"):
        assert forbidden not in INSTALL


def test_windows_powershell_51_platform_guard_does_not_require_iswindows():
    # Windows PowerShell 5.1 has no $IsWindows automatic variable. Keep this
    # assertion literal so a future refactor cannot silently reintroduce it.
    for script in (INSTALL, UNINSTALL):
        assert "$IsWindows" not in script
        assert "function Test-WindowsPlatform" in script
        assert "$env:OS -eq 'Windows_NT'" in script
        assert "[System.PlatformID]::Win32NT" in script


def test_windows_python_preflight_avoids_embedded_quoted_literals():
    assert "sep=chr(46)" in INSTALL
    assert 'print(".".join' not in INSTALL
    # The remaining direct Python snippets contain only identifiers and
    # punctuation, so Windows PowerShell's legacy native argument handling
    # cannot remove an embedded string literal from them.
    assert "-c 'import venv'" in INSTALL
    assert "-c 'import RNS; print(RNS.__version__)'" in INSTALL


def test_windows_launcher_uses_waitress_factory_mode():
    assert "waitress-serve.exe' --listen=127.0.0.1:8080 --call 'reticulum_bolt_on.app:create_app'" in INSTALL
    assert "reticulum_bolt_on.app:create_app()" not in INSTALL


def test_windows_uninstall_only_targets_named_bolt_on_resources():
    assert "Reticulum-Bolt-On-Admin" in UNINSTALL
    assert "Reticulum-Bolt-On-Admin-State" in UNINSTALL
    assert "Unregister-ScheduledTask -TaskName $TaskName" in UNINSTALL
    assert "Port 8080 remains in use" in UNINSTALL
    for forbidden in ("Get-Process rnsd", "Stop-Process -Name rnsd", "Remove-NetFirewallRule", "netsh", "sc delete", "Stop-Service"):
        assert forbidden not in UNINSTALL
