from pathlib import Path


ROOT=Path(__file__).parents[1]


def test_compose_is_read_only_and_has_no_docker_socket_or_rnsd():
    text=(ROOT/"compose.yaml").read_text()
    assert "read_only: true" in text and ":ro" in text
    assert "/var/run/docker.sock" not in text
    assert "privileged:" not in text
    assert "rnsd:" not in text


def test_installer_stops_on_ambiguity_and_never_manages_reticulum():
    text=(ROOT/"install.sh").read_text()
    assert "Multiple Reticulum configs detected" in text
    forbidden=("systemctl restart rnsd","systemctl stop rnsd","docker compose down","rnsd --")
    assert not any(item in text for item in forbidden)


def test_observer_source_has_no_management_routes():
    source=(ROOT/"src/reticulum_bolt_on/app.py").read_text()
    for route in ("drop_path","drop_via","restart_service","edit_config"):
        assert f'@app.post("/{route}' not in source

