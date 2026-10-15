from pathlib import Path
import os
import subprocess

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


def test_observer_source_has_no_management_routes():
    source=(ROOT/"src/reticulum_bolt_on/app.py").read_text()
    for route in ("drop_path","drop_via","restart_service","edit_config"):
        assert f'@app.post("/{route}' not in source
