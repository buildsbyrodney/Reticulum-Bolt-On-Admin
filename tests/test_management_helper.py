import json
import sys
from types import SimpleNamespace

from reticulum_bolt_on.management_helper import execute, probe_capabilities, read_paths, read_runtime


class FakeReticulum:
    instances = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.calls = []
        self.__class__.instances.append(self)

    def drop_path(self, target): self.calls.append(("drop_path", target)); return True
    def drop_all_via(self, target): self.calls.append(("drop_all_via", target)); return True
    def drop_announce_queues(self): self.calls.append(("drop_announce_queues", None))
    def get_path_table(self):
        self.calls.append(("get_path_table", None))
        return [{"hash": bytes.fromhex("a" * 32), "via": bytes.fromhex("b" * 32), "hops": 1}]


def _existing_state_tree(root):
    (root / "config").write_text("[reticulum]\n")
    for relative in ("storage/cache/announces", "storage/resources", "storage/identities",
                     "storage/blackhole", "interfaces"):
        (root / relative).mkdir(parents=True, exist_ok=True)


def test_helper_requires_shared_instance_for_every_supported_action(monkeypatch, tmp_path):
    FakeReticulum.instances.clear()
    _existing_state_tree(tmp_path)
    monkeypatch.setitem(sys.modules, "RNS", SimpleNamespace(Reticulum=FakeReticulum))
    actions = [
        ("drop-path", "a" * 32, "drop_path"),
        ("drop-via", "b" * 32, "drop_all_via"),
        ("drop-announces", None, "drop_announce_queues"),
    ]

    for action, target, expected_method in actions:
        success, _ = execute(tmp_path, action, target)
        instance = FakeReticulum.instances[-1]
        assert success
        assert instance.kwargs == {"configdir": str(tmp_path), "require_shared_instance": True}
        assert instance.calls[0][0] == expected_method


def test_failed_shared_attachment_has_no_fallback_or_rnsd(monkeypatch, tmp_path):
    class UnavailableReticulum:
        def __init__(self, **kwargs):
            assert kwargs["require_shared_instance"] is True
            raise RuntimeError("unavailable")

    monkeypatch.setitem(sys.modules, "RNS", SimpleNamespace(Reticulum=UnavailableReticulum))
    _existing_state_tree(tmp_path)
    success, message = execute(tmp_path, "drop-path", "a" * 32)
    assert not success and "unavailable" in message.lower()


def test_path_reader_requires_shared_instance_and_serializes_hashes(monkeypatch, tmp_path):
    FakeReticulum.instances.clear()
    _existing_state_tree(tmp_path)
    monkeypatch.setitem(sys.modules, "RNS", SimpleNamespace(Reticulum=FakeReticulum))

    success, _, paths = read_paths(tmp_path)

    assert success
    assert FakeReticulum.instances[-1].kwargs == {"configdir": str(tmp_path), "require_shared_instance": True}
    assert paths == [{"hash": "a" * 32, "via": "b" * 32, "hops": 1}]


def test_runtime_reader_requires_shared_instance_and_returns_bounded_safe_stats(monkeypatch, tmp_path):
    class RuntimeReticulum(FakeReticulum):
        def get_interface_stats(self):
            self.calls.append(("get_interface_stats", None))
            return {"transport_id": b"x" * 16, "interfaces": [{
                "name": "WSL Test Client", "short_name": "WSL Test Client",
                "type": "TCPClientInterface", "status": True, "mode": 1,
                "rxb": 12, "txb": 34, "clients": {"private-client": object()}, "ifac_signature": b"secret-signature",
                "ifac_netname": b"secret-network-name", "ifac_size": 64,
                "passphrase": "never-return-this",
            }]}

    RuntimeReticulum.instances.clear(); _existing_state_tree(tmp_path)
    monkeypatch.setitem(sys.modules, "RNS", SimpleNamespace(__version__="1.5.7", Reticulum=RuntimeReticulum))
    success, _, runtime = read_runtime(tmp_path)
    assert success and runtime["shared_instance_available"] and runtime["transport_enabled"] is True
    assert RuntimeReticulum.instances[-1].kwargs == {"configdir": str(tmp_path), "require_shared_instance": True}
    row = runtime["interfaces"][0]
    assert row["rxb"] == 12 and row["clients"] == 1 and row["ifac_present"] is True
    assert "ifac_signature" not in row and "ifac_netname" not in row and "passphrase" not in row
    encoded = json.dumps(runtime)
    assert "secret-signature" not in encoded and "secret-network-name" not in encoded and "private-client" not in encoded


def test_runtime_reader_failure_does_not_fall_back_to_independent_rns(monkeypatch, tmp_path):
    class UnavailableReticulum:
        def __init__(self, **kwargs):
            assert kwargs == {"configdir": str(tmp_path), "require_shared_instance": True}
            raise RuntimeError("no shared instance")

    _existing_state_tree(tmp_path)
    monkeypatch.setitem(sys.modules, "RNS", SimpleNamespace(__version__="1.5.7", Reticulum=UnavailableReticulum))
    success, message, runtime = read_runtime(tmp_path)
    assert not success and "attachment is unavailable" in message and runtime == {}


def test_rns_157_probe_disables_unsafe_per_interface_controls(tmp_path):
    _existing_state_tree(tmp_path)
    rns = SimpleNamespace(__version__="1.5.7", Reticulum=FakeReticulum)

    assert probe_capabilities(rns, tmp_path) == ["drop-announces", "drop-path", "drop-via"]


def test_new_incident_actions_use_transport_only_after_shared_attachment(monkeypatch, tmp_path):
    class FakeTransport:
        calls = []
        @classmethod
        def request_path(cls, target): cls.calls.append(("request_path", target))
        @classmethod
        def blackhole_identity(cls, target): cls.calls.append(("blackhole_identity", target)); return True
        @classmethod
        def unblackhole_identity(cls, target): cls.calls.append(("unblackhole_identity", target)); return True

    FakeReticulum.instances.clear(); FakeTransport.calls.clear(); _existing_state_tree(tmp_path)
    monkeypatch.setitem(sys.modules, "RNS", SimpleNamespace(Reticulum=FakeReticulum, Transport=FakeTransport))
    for action, expected in (("request-path", "request_path"), ("blackhole-identity", "blackhole_identity"), ("unblackhole-identity", "unblackhole_identity")):
        success, _ = execute(tmp_path, action, "a" * 32)
        assert success and FakeReticulum.instances[-1].kwargs["require_shared_instance"] is True
        assert FakeTransport.calls[-1] == (expected, bytes.fromhex("a" * 32))


def test_helper_no_longer_accepts_live_interface_operations(monkeypatch, tmp_path):
    _existing_state_tree(tmp_path)
    monkeypatch.setitem(sys.modules, "RNS", SimpleNamespace(Reticulum=FakeReticulum))
    for action in ("attach-interface", "detach-interface", "reload-interface"):
        assert execute(tmp_path, action, "LAN")[0] is False


def test_helper_rejects_invalid_targets_before_constructing_rns(monkeypatch, tmp_path):
    class MustNotConstruct:
        def __init__(self, **kwargs):
            raise AssertionError("must not construct")

    monkeypatch.setitem(sys.modules, "RNS", SimpleNamespace(Reticulum=MustNotConstruct))
    assert execute(tmp_path, "drop-path", "bad")[0] is False
    assert execute(tmp_path, "drop-via", "")[0] is False


def test_helper_refuses_to_create_missing_reticulum_state(monkeypatch, tmp_path):
    class MustNotConstruct:
        def __init__(self, **kwargs):
            raise AssertionError("must not construct")

    config = tmp_path / "config"
    config.write_text("[reticulum]\n")
    before = sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*"))
    monkeypatch.setitem(sys.modules, "RNS", SimpleNamespace(Reticulum=MustNotConstruct))
    success, message = execute(tmp_path, "drop-announces", None)
    assert not success and "refusing to create" in message.lower()
    assert sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*")) == before
