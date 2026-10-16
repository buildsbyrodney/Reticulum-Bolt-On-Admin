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


def test_windows_uninstall_only_targets_named_bolt_on_resources():
    assert "Reticulum-Bolt-On-Admin" in UNINSTALL
    assert "Reticulum-Bolt-On-Admin-State" in UNINSTALL
    assert "Unregister-ScheduledTask -TaskName $TaskName" in UNINSTALL
    assert "Port 8080 remains in use" in UNINSTALL
    for forbidden in ("Get-Process rnsd", "Stop-Process -Name rnsd", "Remove-NetFirewallRule", "netsh", "sc delete", "Stop-Service"):
        assert forbidden not in UNINSTALL
