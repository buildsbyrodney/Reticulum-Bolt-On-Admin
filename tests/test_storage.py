import json

from reticulum_bolt_on.storage import ObserverStore


def test_management_audit_is_bounded_and_secret_metadata_is_discarded(tmp_path):
    store = ObserverStore(tmp_path / "observer.db")
    store.record_audit("drop_path", "a" * 32, True, {
        "matching_path_count": 4,
        "path_state_refreshed": True,
        "rpc_key": "must-never-be-stored",
        "passphrase": "also-secret",
    })
    event = store.events(1)[0]
    detail = json.loads(event["detail_json"])
    assert detail == {
        "action": "drop_path",
        "target": "a" * 32,
        "result": "success",
        "metadata": {"matching_path_count": 4, "path_state_refreshed": True},
    }
    assert "secret" not in event["detail_json"]
