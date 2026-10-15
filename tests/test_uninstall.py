from pathlib import Path
import hashlib
import os
import subprocess
import time

import pytest


ROOT = Path(__file__).parents[1]
UNINSTALL = ROOT / "uninstall.sh"


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _environment(tmp_path: Path, home: Path) -> tuple[dict[str, str], Path, Path]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    systemctl_log = tmp_path / "systemctl.log"
    systemctl = fake_bin / "systemctl"
    systemctl.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' \"$*\" >> \"$SYSTEMCTL_LOG\"\n"
        "case \"$*\" in\n"
        "  '--user list-unit-files reticulum-bolt-on-admin.service --no-legend') "
        "printf 'reticulum-bolt-on-admin.service enabled\\n' ;;\n"
        "  '--user is-active --quiet reticulum-bolt-on-admin.service') exit 3 ;;\n"
        "esac\n"
        "exit 0\n"
    )
    systemctl.chmod(0o755)
    ss = fake_bin / "ss"
    ss.write_text(
        "#!/bin/sh\n"
        "printf 'LISTEN 0 128 127.0.0.1:8080 0.0.0.0:* users:((other,pid=999,fd=3))\\n'\n"
    )
    ss.chmod(0o755)
    sudo_marker = tmp_path / "sudo-used"
    sudo = fake_bin / "sudo"
    sudo.write_text("#!/bin/sh\ntouch \"$SUDO_MARKER\"\nexit 99\n")
    sudo.chmod(0o755)
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in {"PREFIX", "STATE_DIR", "XDG_CONFIG_HOME", "BOLT_ON_STATE", "BOLT_ON_OBSERVER_DATA"}
    }
    environment.update(
        {
            "HOME": str(home),
            "XDG_CONFIG_HOME": str(home / ".config"),
            "PATH": f"{fake_bin}{os.pathsep}/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "SYSTEMCTL_LOG": str(systemctl_log),
            "SUDO_MARKER": str(sudo_marker),
        }
    )
    return environment, systemctl_log, sudo_marker


def test_uninstall_contract_is_narrow_and_docker_cleanup_is_bolt_on_only():
    text = UNINSTALL.read_text()
    assert "Refusing to uninstall the native GUI as root" in text
    assert 'DEFAULT_PREFIX="$HOME/.local/reticulum-bolt-on-admin"' in text
    assert 'DEFAULT_STATE_DIR="$HOME/.local/share/reticulum-bolt-on-admin"' in text
    assert "systemctl --user disable --now \"$SERVICE_NAME\"" in text
    assert "reticulum-bolt-on-admin.service" in text
    assert "docker compose -f \"$SCRIPT_DIR/compose.yaml\" -p \"$COMPOSE_PROJECT\" down --remove-orphans" in text
    assert "nonexistent-reticulum-cleanup-source" in text
    assert "docker compose down" not in text
    assert "--volumes" not in text
    assert "\nsudo " not in text
    for forbidden in (
        "systemctl stop rnsd",
        "systemctl disable rnsd",
        "pkill rnsd",
        "killall rnsd",
        'rm -rf -- "$HOME/.reticulum"',
        'rm -rf -- "$RETICULUM_DIR"',
    ):
        assert forbidden not in text


@pytest.mark.skipif(
    os.name == "nt" or getattr(os, "geteuid", lambda: 0)() == 0,
    reason="normal-user Linux uninstall integration",
)
def test_native_uninstall_preserves_state_cleans_user_service_and_reticulum(tmp_path):
    home = tmp_path / "home"
    prefix = tmp_path / "custom-app"
    state = tmp_path / "custom-state"
    prefix.mkdir()
    launcher = prefix / "run"
    launcher.write_text(
        "#!/bin/bash\n"
        "sleep 60 &\n"
        "child=$!\n"
        "trap 'kill \"$child\" 2>/dev/null || true' EXIT TERM\n"
        "wait \"$child\"\n"
    )
    launcher.chmod(0o755)
    gui = state / "gui"
    gui.mkdir(parents=True)
    database = gui / "observer.db"
    secret = gui / "secret"
    history = gui / "history.json"
    database.write_bytes(b"existing-admin-and-labels")
    secret.write_text("existing-secret")
    history.write_text("existing-history")
    state_hashes = {path.name: _digest(path) for path in (database, secret, history)}

    unit_dir = home / ".config" / "systemd" / "user"
    wants = unit_dir / "default.target.wants"
    wants.mkdir(parents=True)
    unit = unit_dir / "reticulum-bolt-on-admin.service"
    unit.write_text("[Service]\nExecStart=/custom-app/run\n")
    (wants / unit.name).symlink_to(unit)

    reticulum = home / ".reticulum"
    storage = reticulum / "storage"
    identities = storage / "identities"
    identities.mkdir(parents=True)
    config = reticulum / "config"
    identity = identities / "transport_identity"
    rns_service = home / ".config" / "systemd" / "user" / "rnsd.service"
    compose = tmp_path / "production-compose.yaml"
    config.write_text("production-config")
    identity.write_text("production-identity")
    rns_service.write_text("production-rnsd-unit")
    compose.write_text("production-reticulum-compose")
    protected = (config, identity, rns_service, compose)
    protected_hashes = {path: _digest(path) for path in protected}

    environment, systemctl_log, sudo_marker = _environment(tmp_path, home)
    environment.update({"PREFIX": str(prefix), "STATE_DIR": str(state)})
    manual_process = subprocess.Popen([str(launcher)], env=environment)
    time.sleep(0.1)
    try:
        result = subprocess.run(
            ["bash", str(UNINSTALL), "--yes"],
            text=True,
            capture_output=True,
            env=environment,
        )
        manual_process.wait(timeout=3)
    finally:
        if manual_process.poll() is None:
            manual_process.terminate()
            manual_process.wait(timeout=3)

    assert result.returncode == 0, result.stderr
    assert not prefix.exists()
    assert manual_process.returncode is not None
    assert state.is_dir()
    assert {path.name: _digest(path) for path in (database, secret, history)} == state_hashes
    assert not unit.exists() and not (wants / unit.name).exists()
    log = systemctl_log.read_text()
    assert "--user disable --now reticulum-bolt-on-admin.service" in log
    assert "--user daemon-reload" in log
    assert "--user is-active --quiet reticulum-bolt-on-admin.service" in log
    assert not sudo_marker.exists()
    assert "Preserve GUI state/database/secret (login, labels, and local history)" in result.stdout
    assert "not a confirmed Bolt-On process" in result.stdout
    assert {path: _digest(path) for path in protected} == protected_hashes


@pytest.mark.skipif(
    os.name == "nt" or getattr(os, "geteuid", lambda: 0)() == 0,
    reason="normal-user Linux uninstall integration",
)
def test_native_uninstall_purge_removes_custom_state(tmp_path):
    home = tmp_path / "home"
    prefix = tmp_path / "another-app"
    state = tmp_path / "another-state"
    prefix.mkdir()
    state.mkdir()
    (prefix / "installed").write_text("app")
    (state / "observer.db").write_text("private-gui-state")
    environment, _, sudo_marker = _environment(tmp_path, home)
    environment.update({"PREFIX": str(prefix), "STATE_DIR": str(state)})

    result = subprocess.run(
        ["bash", str(UNINSTALL), "--purge", "--yes"],
        text=True,
        capture_output=True,
        env=environment,
    )

    assert result.returncode == 0, result.stderr
    assert not prefix.exists() and not state.exists()
    assert not sudo_marker.exists()
    assert "Remove GUI state/database/secret" in result.stdout
    assert "GUI state removed (--purge)" in result.stdout


@pytest.mark.skipif(
    os.name == "nt" or getattr(os, "geteuid", lambda: 0)() == 0,
    reason="normal-user Linux uninstall integration",
)
def test_native_uninstall_refuses_reticulum_and_overlapping_preserved_state(tmp_path):
    home = tmp_path / "home"
    reticulum = home / ".reticulum"
    reticulum.mkdir(parents=True)
    config = reticulum / "config"
    config.write_text("production-config")
    before = _digest(config)
    environment, _, sudo_marker = _environment(tmp_path, home)
    environment.update({"PREFIX": str(reticulum), "STATE_DIR": str(tmp_path / "state")})

    unsafe_result = subprocess.run(
        ["bash", str(UNINSTALL), "--yes"],
        text=True,
        capture_output=True,
        env=environment,
    )

    assert unsafe_result.returncode == 1
    assert "Refusing unsafe PREFIX removal target" in unsafe_result.stderr
    assert config.exists() and _digest(config) == before
    assert not sudo_marker.exists()

    prefix = tmp_path / "app"
    nested_state = prefix / "state"
    nested_state.mkdir(parents=True)
    (prefix / "installed").write_text("app")
    (nested_state / "observer.db").write_text("preserved")
    environment.update({"PREFIX": str(prefix), "STATE_DIR": str(nested_state)})

    overlap_result = subprocess.run(
        ["bash", str(UNINSTALL), "--yes"],
        text=True,
        capture_output=True,
        env=environment,
    )

    assert overlap_result.returncode == 1
    assert "preserved STATE_DIR is inside PREFIX" in overlap_result.stderr
    assert prefix.is_dir() and (nested_state / "observer.db").read_text() == "preserved"


@pytest.mark.skipif(
    os.name == "nt" or getattr(os, "geteuid", lambda: 0)() == 0,
    reason="normal-user Linux Docker cleanup integration",
)
def test_docker_cleanup_targets_only_bolt_on_project_and_preserves_state(tmp_path):
    home = tmp_path / "home"
    state = tmp_path / "docker-state"
    web_state = state / "gui"
    observer_data = state / "observer-input"
    web_state.mkdir(parents=True)
    observer_data.mkdir()
    database = web_state / "observer.db"
    snapshot = observer_data / "runtime-status.json"
    database.write_text("admin-labels-history")
    snapshot.write_text("sanitized-observation")
    before = {_digest(database), _digest(snapshot)}

    production = tmp_path / "production-reticulum"
    production.mkdir()
    config = production / "config"
    identity = production / "identity"
    config.write_text("production-config")
    identity.write_text("production-private-identity")
    production_before = {_digest(config), _digest(identity)}

    environment, _, sudo_marker = _environment(tmp_path, home)
    docker_log = tmp_path / "docker.log"
    docker = tmp_path / "bin" / "docker"
    docker.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' \"$*\" >> \"$DOCKER_LOG\"\n"
        "exit 0\n"
    )
    docker.chmod(0o755)
    environment.update(
        {
            "STATE_DIR": str(state),
            "BOLT_ON_STATE": str(web_state),
            "BOLT_ON_OBSERVER_DATA": str(observer_data),
            "DOCKER_LOG": str(docker_log),
            "RETICULUM_DIR": str(production),
        }
    )

    result = subprocess.run(
        ["bash", str(UNINSTALL), "--docker", "--yes"],
        text=True,
        capture_output=True,
        env=environment,
    )

    assert result.returncode == 0, result.stderr
    log = docker_log.read_text()
    assert f"compose -f {ROOT / 'compose.yaml'} -p reticulum-bolt-on-admin down --remove-orphans" in log
    assert "ps -q --filter label=com.docker.compose.project=reticulum-bolt-on-admin" in log
    assert web_state.is_dir() and observer_data.is_dir()
    assert {_digest(database), _digest(snapshot)} == before
    assert {_digest(config), _digest(identity)} == production_before
    assert not sudo_marker.exists()
