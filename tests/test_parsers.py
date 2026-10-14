from pathlib import Path

import pytest

from reticulum_bolt_on.parsers import bounded_paths, find_interfaces, merge_interfaces, normalize_paths, parse_config_interfaces, path_health, proven_clients


FIXTURES = Path(__file__).parent / "fixtures"


@pytest.mark.parametrize(("fixture", "expected"), [
    ("ifac_explicit.ini", True),
    ("ifac_connection_fields.ini", True),
    ("ifac_absent.ini", False),
])
def test_configured_ifac_detection(fixture, expected):
    _, interfaces = parse_config_interfaces((FIXTURES / fixture).read_text())
    assert interfaces[0]["ifac_present"] is expected


def test_interface_and_proven_client_parsing():
    raw = {"interfaces": [
        {"name":"TCPServerInterface[LAN/0.0.0.0:4242]","short_name":"LAN","type":"TCPServerInterface","status":True,"clients":1,"ifac_signature":"hidden"},
        {"name":"TCPInterface[Client on LAN/192.168.1.2:5000]","type":"TCPClientInterface","parent_interface_name":"TCPServerInterface[LAN/0.0.0.0:4242]","status":True,"rxb":2,"txb":3},
        {"name":"not a proven client 10.0.0.1","type":"TCPClientInterface","parent_interface_name":"LAN"},
    ]}
    rows = find_interfaces(raw); clients = proven_clients(rows, [{"name":"LAN","type":"TCPServerInterface"}])
    assert rows[0]["ifac_present"] is True
    assert len(clients) == 1 and clients[0]["remote_ip"] == "192.168.1.2"
    assert clients[0]["identity"] is None


def test_four_local_paths_across_four_vias():
    paths = normalize_paths({"paths":[{"hash":str(i)*32,"via":str(i)*32,"hops":0,"interface":"Local"} for i in range(1,5)]})
    health = path_health(paths)
    assert health["total"] == 4 and health["unique_vias"] == 4
    assert not health["warnings"]


def test_four_thousand_paths_through_one_via_warns_conservatively():
    via = "a" * 32
    paths = [{"destination":f"{i:032x}","via":via,"hops":2,"interface":"TCP"} for i in range(4000)]
    health = path_health(paths)
    assert health["top"]["count"] == 4000
    assert len(health["top"]["samples"]) == 5
    assert "High concentration of learned paths through one transport." in health["warnings"]
    assert any("High path count" in warning for warning in health["warnings"])
    shown, omitted = bounded_paths(paths)
    assert len(shown) == 500 and omitted == 3500


def test_missing_via_never_triggers_concentration_warning():
    health = path_health([{"destination":f"{i:032x}","via":"","hops":1,"interface":"x"} for i in range(4000)])
    assert not any("concentration" in warning.lower() for warning in health["warnings"])


def test_configured_and_runtime_sources_remain_distinct():
    rows = merge_interfaces([{"name":"LAN","type":"TCPServerInterface"}], [{"name":"LocalClientInterface[x]","short_name":"LocalClientInterface[x]","type":"LocalClientInterface"}])
    assert rows[0]["configured"] and not rows[0]["runtime"]
    assert not rows[1]["configured"] and rows[1]["runtime"]

