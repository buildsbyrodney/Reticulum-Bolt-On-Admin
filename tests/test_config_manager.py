import os

import pytest

from reticulum_bolt_on.config_manager import ConfigConflict, ConfigManager, redact_diff, render_section, sections


CONFIG = """[reticulum]\n\n[[Trusted Upstream]]\ntype = TCPClientInterface\ntarget_host = old.example\ntarget_port = 4242\nenabled = yes\nifac_netkey = hidden-value\nunknown_setting = retain-me\n"""


def test_edit_preserves_unknown_and_secret_fields_and_is_atomic(tmp_path):
    path = tmp_path / "config"; path.write_text(CONFIG)
    manager = ConfigManager(path); before, expected = manager.read(); old = sections(before)[0]
    after, diff = manager.preview(before, render_section(old.name, {**old.values(), "target_host": "new.example"}, old), old)
    assert "unknown_setting = retain-me" in after and "ifac_netkey = hidden-value" in after and "-target_host = old.example" in diff
    backup = manager.apply(expected, after)
    assert backup.read_text() == CONFIG and "target_host = new.example" in path.read_text()


def test_external_change_conflict_and_retention(tmp_path):
    path = tmp_path / "config"; path.write_text(CONFIG)
    manager = ConfigManager(path, backup_limit=2); before, expected = manager.read()
    path.write_text(CONFIG + "# external\n")
    with pytest.raises(ConfigConflict): manager.apply(expected, before)
    for index in range(3):
        old, token = manager.read(); manager.apply(token, old + f"# {index}\n")
    assert len(list(tmp_path.glob("config.backup-*"))) == 2


def test_mode_is_preserved_after_atomic_replace(tmp_path):
    path = tmp_path / "config"; path.write_text(CONFIG); os.chmod(path, 0o600)
    manager = ConfigManager(path); content, token = manager.read(); manager.apply(token, content + "# saved\n")
    if os.name != "nt": assert (path.stat().st_mode & 0o777) == 0o600


def test_review_diff_hides_secrets():
    diff = "-passphrase = old-secret\n+ifac_netkey = new-secret\n+target_host = safe.example\n"
    rendered = redact_diff(diff)
    assert "old-secret" not in rendered and "new-secret" not in rendered
    assert "target_host = safe.example" in rendered and rendered.count("[present, hidden]") == 2


def test_render_can_remove_ifac_fields_without_touching_unknown_fields():
    source = "[[LAN]]\ntype = TCPServerInterface\nlisten_port = 4242\npassphrase = secret\nifac_size = 64\nunknown = keep\n"
    section = sections(source)[0]
    rendered = "".join(render_section("LAN", {"type": "TCPServerInterface", "listen_port": "4242"}, section,
                                       {"passphrase", "ifac_size"}))
    assert "passphrase" not in rendered and "ifac_size" not in rendered and "unknown = keep" in rendered
