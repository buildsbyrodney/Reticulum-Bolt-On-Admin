from pathlib import Path
import os
import subprocess
import textwrap

import pytest


ROOT=Path(__file__).parents[1]


def test_compose_is_read_only_and_has_no_docker_socket_or_rnsd():
    text=(ROOT/"compose.yaml").read_text()
    assert "read_only: true" in text and ":ro" in text
    assert "/var/run/docker.sock" not in text
    assert "privileged:" not in text
    assert "rnsd:" not in text
    observer=text.split("  observer:", 1)[1]
    assert "BOLT_ON_SNAPSHOT_DIR: /observer-data" in observer
    assert "RETICULUM_CONFIG:" not in observer
    assert "/source" not in observer and "/reticulum" not in observer
    helper=text.split("  observation-helper:", 1)[1].split("  observer:", 1)[0]
    assert 'user: "0:0"' in helper and "network_mode: host" in helper
    assert 'cap_drop: ["ALL"]' in helper and "/source:ro" in helper
    assert "no-new-privileges:true" in helper and "read_only: true" in helper


def test_installer_stops_on_ambiguity_and_never_manages_reticulum():
    text=(ROOT/"install.sh").read_text()
    assert "Multiple Reticulum configs detected" in text
    assert "sys.version_info >= (3, 10)" in text
    assert text.index("sys.version_info >= (3, 10)") < text.index("install -d")
    forbidden=("systemctl restart rnsd","systemctl stop rnsd","docker compose down","rnsd --")
    assert not any(item in text for item in forbidden)
    assert 'DEFAULT_PREFIX="$HOME/.local/reticulum-bolt-on-admin"' in text
    assert 'DEFAULT_STATE_DIR="$HOME/.local/share/reticulum-bolt-on-admin"' in text
    assert "Refusing to install or run the native web GUI as root" in text
    assert "systemctl --user enable --now" in text


def test_container_pins_observer_utilities_without_starting_rnsd():
    dockerfile=(ROOT/"Dockerfile").read_text()
    assert "ARG RNS_VERSION=1.5.7" in dockerfile
    assert '"rns==${RNS_VERSION}"' in dockerfile
    assert 'CMD ["gunicorn"' in dockerfile
    assert 'CMD ["rnsd"' not in dockerfile and 'ENTRYPOINT ["rnsd"' not in dockerfile


def test_installer_owns_only_gui_managed_state_for_each_container_user():
    text=(ROOT/"install.sh").read_text()
    assert 'install -d -o 0 -g 0 -m 0755 "$OBSERVER_DATA"' in text
    assert 'install -d -o 10001 -g 10001 -m 0700 "$GUI_STATE"' in text
    assert "\nchown " not in text and "; chown " not in text


@pytest.mark.skipif(os.name == "nt", reason="installer targets Linux")
def test_installer_rejects_old_python_before_writing(tmp_path):
    old_python = tmp_path / "python3-old"
    old_python.write_text("#!/bin/sh\nexit 1\n")
    old_python.chmod(0o755)
    prefix, state = tmp_path / "install", tmp_path / "state"
    result = subprocess.run(
        ["bash", str(ROOT / "install.sh"), "--yes"],
        text=True, capture_output=True,
        env={**os.environ, "PYTHON_BIN": str(old_python), "PREFIX": str(prefix), "STATE_DIR": str(state)},
    )
    assert result.returncode == 1
    assert "too old" in result.stderr
    assert not prefix.exists() and not state.exists()


@pytest.mark.skipif(os.name == "nt" or getattr(os, "geteuid", lambda: 0)() == 0,
                    reason="normal-user Linux installer integration")
def test_native_install_defaults_to_user_paths_without_root_owned_files(tmp_path):
    home = tmp_path / "home"
    config = home / ".reticulum" / "config"
    config.parent.mkdir(parents=True)
    config.write_text("[reticulum]\n  share_instance = Yes\n")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_python = fake_bin / "python3-test"
    fake_python.write_text(textwrap.dedent("""\
        #!/bin/sh
        if [ "$1" = "-c" ]; then
          case "$2" in
            *sys.version_info*) exit 0 ;;
            *secrets*) printf '%064d\\n' 0; exit 0 ;;
          esac
        fi
        if [ "$1" = "-m" ] && [ "$2" = "venv" ]; then
          mkdir -p "$3/bin"
          printf '#!/bin/sh\\nexit 0\\n' > "$3/bin/pip"
          chmod 0755 "$3/bin/pip"
          exit 0
        fi
        exit 1
    """))
    fake_python.chmod(0o755)
    user_bin = home / ".local" / "bin"
    user_bin.mkdir(parents=True)
    for utility in ("rnstatus", "rnpath"):
        executable = user_bin / utility
        executable.write_text("#!/bin/sh\nexit 1\n")
        executable.chmod(0o755)
    systemctl_log = tmp_path / "systemctl.log"
    fake_systemctl = fake_bin / "systemctl"
    fake_systemctl.write_text("#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$SYSTEMCTL_LOG\"\nexit 0\n")
    fake_systemctl.chmod(0o755)
    sudo_marker = tmp_path / "sudo-used"
    fake_sudo = fake_bin / "sudo"
    fake_sudo.write_text("#!/bin/sh\ntouch \"$SUDO_MARKER\"\nexit 99\n")
    fake_sudo.chmod(0o755)
    xdg_config = home / ".config"
    environment = {key: value for key, value in os.environ.items() if key not in {"PREFIX", "STATE_DIR", "RNS_CONFIG_DIR"}}
    environment.update({
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(xdg_config),
        "PYTHON_BIN": str(fake_python),
        # Deliberately omit the real user's ~/.local/bin to reproduce a
        # non-interactive user service environment around a pipx install.
        "PATH": f"{fake_bin}{os.pathsep}/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "SYSTEMCTL_LOG": str(systemctl_log),
        "SUDO_MARKER": str(sudo_marker),
    })
    result = subprocess.run(
        ["bash", str(ROOT / "install.sh"), "--config", str(config), "--yes"],
        text=True, capture_output=True, env=environment,
    )
    assert result.returncode == 0, result.stderr
    prefix = home / ".local" / "reticulum-bolt-on-admin"
    state = home / ".local" / "share" / "reticulum-bolt-on-admin"
    assert (prefix / "run").is_file()
    assert (state / "gui" / "secret").is_file()
    assert (xdg_config / "systemd" / "user" / "reticulum-bolt-on-admin.service").is_file()
    assert "--user enable --now reticulum-bolt-on-admin.service" in systemctl_log.read_text()
    assert f"Exact launcher command: {prefix}/run" in result.stdout
    assert "Local URL: http://127.0.0.1:8080/" in result.stdout
    assert not sudo_marker.exists()
    assert all(path.stat().st_uid == os.geteuid() for path in home.rglob("*"))
    launcher = (prefix / "run").read_text()
    assert "Refusing to run the Reticulum Bolt-On Admin web GUI as root" in launcher
    assert f"export PATH={user_bin}:\"$PATH\"" in launcher


def test_management_source_has_no_forbidden_lifecycle_or_config_routes():
    source=(ROOT/"src/reticulum_bolt_on/app.py").read_text()
    assert '@app.post("/management/' in source
    for forbidden in ("restart_service", "edit_config", "transport_enable", "docker restart", "systemctl restart"):
        assert forbidden not in source
