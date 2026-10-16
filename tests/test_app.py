from pathlib import Path
import json

from reticulum_bolt_on.app import create_app
from reticulum_bolt_on.models import DetectionResult, InstallationCandidate, ManagementResult, RuntimeSnapshot


class FakeBackend:
    def __init__(self, config): self.config=config
    def detect(self): return DetectionResult((InstallationCandidate(self.config,self.config.parent/"storage","host-native",shared_instance_available=True),), InstallationCandidate(self.config,self.config.parent/"storage","host-native",shared_instance_available=True), False, "selected")
    def runtime_status(self):
        return RuntimeSnapshot(True,True,"1.0",False,[{"name":"LAN","short_name":"LAN","type":"TCPServerInterface","runtime_status":"Up","source":"runtime"}], [{"destination":f"{i:032x}","via":"a"*32,"hops":1,"interface":"LAN","expires":None} for i in range(600)], [], None)
    def config_view(self): return {"available":True,"interfaces":[{"name":"LAN","type":"TCPServerInterface","enabled":True,"mode":"gateway","ifac_present":False,"source":"configured"}],"sections":{}}
    def capabilities(self): return {"observe":True,"labels":True,"drop_path":False,"drop_via":False,"drop_announces":False,"attach_interface":False,"detach_interface":False,"reload_interface":False,"edit_config":False,"restart_service":False,"transport_enable":False}
    def paths(self): return self.runtime_status().paths
    def interfaces(self): return self.runtime_status().interfaces
    def clients(self): return []


def logged_client(tmp_path, backend=None):
    tmp_path.mkdir(parents=True, exist_ok=True)
    config=tmp_path/"config"; config.write_text("[reticulum]\n")
    app=create_app({"TESTING":True,"SECRET_KEY":"test","DATABASE":str(tmp_path/"gui.db")},backend or FakeBackend(config))
    store=app.extensions["observer_store"]
    from werkzeug.security import generate_password_hash
    with store.connect() as con: con.execute("INSERT INTO admins VALUES(?,?,1)",("admin",generate_password_hash("long-test-password")))
    client=app.test_client()
    with client.session_transaction() as session: session["admin"]="admin"; session["csrf"]="token"
    return app,client


def test_observer_pages_have_no_management_controls_and_paths_are_bounded(tmp_path):
    app,client=logged_client(tmp_path)
    for url in ("/","/interfaces","/paths","/clients","/diagnostics","/timeline","/map"):
        response=client.get(url); assert response.status_code==200
        text=response.get_data(as_text=True).lower()
        assert 'action="/drop' not in text and 'action="/restart' not in text and '>edit<' not in text
    paths=client.get("/paths").get_data(as_text=True)
    assert "100 additional paths are omitted" in paths


def test_labels_stay_in_gui_database_only(tmp_path):
    app,client=logged_client(tmp_path); config=tmp_path/"config"; before=config.read_bytes()
    response=client.post("/labels",data={"csrf_token":"token","kind":"transport","value":"a"*32,"label":"Backbone"})
    assert response.status_code==302 and config.read_bytes()==before
    assert app.extensions["observer_store"].labels()[("transport","a"*32)]=="Backbone"
    assert "Backbone" in client.get("/paths").get_data(as_text=True)
    assert 'name="kind" value="interface"' in client.get("/interfaces").get_data(as_text=True)


def test_map_is_bounded_and_settling_is_not_continuous(tmp_path):
    _,client=logged_client(tmp_path); graph=client.get("/api/map").get_json()
    assert graph["bounded"] is True and graph["settle_frames"]==180
    assert len([node for node in graph["nodes"] if node["kind"]=="path"])==250
    js=(Path(__file__).parents[1]/"src/reticulum_bolt_on/static/map.js").read_text()
    assert "frames++>=graph.settle_frames" in js


def test_auth_cookie_and_csrf(tmp_path):
    app,client=logged_client(tmp_path)
    assert app.config["SESSION_COOKIE_HTTPONLY"] and app.config["SESSION_COOKIE_SAMESITE"]=="Lax"
    assert client.post("/labels",data={"kind":"interface","value":"LAN","label":"x"}).status_code==400


class ManagementBackend(FakeBackend):
    def __init__(self, config, fail=False):
        super().__init__(config)
        self.fail = fail
        self.calls = []
        self.current_paths = [
            {"destination": f"{index:032x}", "via": "a" * 32 if index < 4 else "b" * 32,
             "hops": 1, "interface": "LAN", "expires": None}
            for index in range(1, 6)
        ]

    def runtime_status(self):
        return RuntimeSnapshot(True, True, "1.5.7", False,
            [{"name":"LAN","short_name":"LAN","type":"TCPServerInterface","runtime_status":"Up","source":"runtime"}],
            list(self.current_paths), [], None, path_count=len(self.current_paths))

    def capabilities(self):
        value = super().capabilities()
        value.update({"drop_path": True, "drop_via": True, "drop_announces": True,
                      "attach_interface": True, "detach_interface": True, "reload_interface": True})
        return value

    def _result(self, action, target):
        self.calls.append((action, target))
        return ManagementResult(not self.fail, "completed" if not self.fail else "backend rejected",
                                {"path_state_refreshed": not self.fail})

    def drop_path(self, destination):
        result = self._result("drop_path", destination)
        if result.success: self.current_paths = [row for row in self.current_paths if row["destination"] != destination]
        return result

    def drop_paths_via(self, via):
        result = self._result("drop_via", via)
        if result.success: self.current_paths = [row for row in self.current_paths if row["via"] != via]
        return result

    def drop_announce_queues(self): return self._result("drop_announces", "all interfaces")
    def manage_interface(self, operation, name): return self._result(f"{operation}_interface", name)


def _audit_rows(app):
    with app.extensions["observer_store"].connect() as con:
        return [dict(row) for row in con.execute("SELECT * FROM events WHERE kind='management_action' ORDER BY id")]


def test_management_drop_one_path_requires_review_csrf_and_post(tmp_path):
    config = tmp_path / "production-config"
    config.write_text("unchanged")
    backend = ManagementBackend(config)
    app, client = logged_client(tmp_path, backend)
    destination = backend.current_paths[0]["destination"]

    assert client.get("/management/drop-path/review").status_code == 405
    assert client.get("/management/drop_path/execute").status_code == 405
    assert backend.calls == []
    assert client.post("/management/drop-path/review", data={"destination": destination}).status_code == 400
    invalid = client.post("/management/drop-path/review", data={"csrf_token": "token", "destination": "bad"})
    assert invalid.status_code == 400 and backend.calls == []

    review = client.post("/management/drop-path/review", data={"csrf_token": "token", "destination": destination})
    assert review.status_code == 200 and destination in review.get_data(as_text=True)
    assert client.post("/management/drop_path/execute", data={"csrf_token": "token"}).status_code == 400
    result = client.post("/management/drop_path/execute", data={"csrf_token": "token", "confirm": "yes"})

    assert result.status_code == 200 and "Success" in result.get_data(as_text=True)
    assert backend.calls == [("drop_path", destination)]
    assert destination not in {row["destination"] for row in backend.current_paths}
    audit = _audit_rows(app)[-1]
    detail = json.loads(audit["detail_json"])
    assert detail["action"] == "drop_path" and detail["target"] == destination and detail["result"] == "success"
    assert config.read_text() == "unchanged"


def test_management_requires_authenticated_admin(tmp_path):
    backend = ManagementBackend(tmp_path / "config")
    _, client = logged_client(tmp_path, backend)
    with client.session_transaction() as session:
        session.pop("admin", None)
    response = client.post("/management/drop-path/review",
                           data={"csrf_token":"token", "destination":backend.current_paths[0]["destination"]})
    assert response.status_code == 302 and "/login" in response.headers["Location"]
    assert backend.calls == []


def test_drop_via_review_is_exact_bounded_and_refreshes_visible_paths(tmp_path):
    backend = ManagementBackend(tmp_path / "config")
    backend.current_paths = [
        {"destination": f"{index:032x}", "via": "a" * 32, "hops": 1, "interface": "LAN", "expires": None}
        for index in range(15)
    ] + [{"destination": "f" * 32, "via": "b" * 32, "hops": 1, "interface": "LAN", "expires": None}]
    app, client = logged_client(tmp_path, backend)
    via = "a" * 32

    assert client.post("/management/drop-via/review", data={"csrf_token":"token", "via":""}).status_code == 400
    assert client.post("/management/drop-via/review", data={"csrf_token":"token", "via":"c" * 32}).status_code == 400
    review = client.post("/management/drop-via/review", data={"csrf_token":"token", "via":via})
    text = review.get_data(as_text=True)
    assert review.status_code == 200 and "Current matching paths" in text and ">15<" in text
    assert f"{9:032x}" in text and f"{10:032x}" not in text

    result = client.post("/management/drop_via/execute", data={"csrf_token":"token", "confirm":"yes"})
    assert result.status_code == 200 and backend.calls == [("drop_via", via)]
    assert all(row["via"] != via for row in backend.current_paths)
    assert via not in client.get("/paths").get_data(as_text=True)
    detail = json.loads(_audit_rows(app)[-1]["detail_json"])
    assert detail["metadata"]["matching_path_count"] == 15
    assert detail["metadata"]["sample_count"] == 10


def test_queued_announces_are_confirmed_and_audited(tmp_path):
    backend = ManagementBackend(tmp_path / "config")
    app, client = logged_client(tmp_path, backend)
    review = client.post("/management/drop-announces/review", data={"csrf_token":"token"})
    assert review.status_code == 200 and "does not remove destination paths" in review.get_data(as_text=True)
    assert client.post("/management/drop_announces/execute", data={"csrf_token":"token", "confirm":"yes"}).status_code == 200

    assert backend.calls == [("drop_announces", "all interfaces")]
    assert len(_audit_rows(app)) == 1


def test_unsupported_management_is_disabled_and_failure_changes_nothing_unrelated(tmp_path):
    observer_app, observer_client = logged_client(tmp_path / "observer")
    page = observer_client.get("/management").get_data(as_text=True)
    assert "Disabled for this deployment" in page
    assert "Management capability available" not in observer_client.get("/").get_data(as_text=True)
    assert observer_client.post("/management/drop-announces/review", data={"csrf_token":"token"}).status_code == 403

    protected = tmp_path / "protected"
    protected.mkdir()
    config = protected / "config"
    storage = protected / "storage"
    config.write_text("production-config")
    storage.write_text("production-storage")
    before = (config.read_bytes(), storage.read_bytes())
    backend = ManagementBackend(config, fail=True)
    app, client = logged_client(tmp_path / "failure", backend)
    destination = backend.current_paths[0]["destination"]
    client.post("/management/drop-path/review", data={"csrf_token":"token", "destination":destination})
    result = client.post("/management/drop_path/execute", data={"csrf_token":"token", "confirm":"yes"})

    assert result.status_code == 200 and "Failed" in result.get_data(as_text=True)
    assert destination in {row["destination"] for row in backend.current_paths}
    assert (config.read_bytes(), storage.read_bytes()) == before
    assert json.loads(_audit_rows(app)[-1]["detail_json"])["result"] == "failure"

