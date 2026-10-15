import sys
from types import SimpleNamespace

from reticulum_bolt_on.management_helper import execute, read_paths


class FakeReticulum:
    instances = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.calls = []
        self.__class__.instances.append(self)

    def drop_path(self, target): self.calls.append(("drop_path", target)); return True
    def drop_all_via(self, target): self.calls.append(("drop_all_via", target)); return True
    def drop_announce_queues(self): self.calls.append(("drop_announce_queues", None))
    def attach_interface(self, name): self.calls.append(("attach_interface", name)); return True
    def detach_interface(self, name): self.calls.append(("detach_interface", name)); return True
    def reload_interface(self, name): self.calls.append(("reload_interface", name)); return True
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
        ("attach-interface", "LAN", "attach_interface"),
        ("detach-interface", "LAN", "detach_interface"),
        ("reload-interface", "LAN", "reload_interface"),
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
