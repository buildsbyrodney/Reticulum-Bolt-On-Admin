from pathlib import Path

from reticulum_bolt_on.app import create_app
from reticulum_bolt_on.models import DetectionResult, InstallationCandidate, RuntimeSnapshot


class FakeBackend:
    def __init__(self, config): self.config=config
    def detect(self): return DetectionResult((InstallationCandidate(self.config,self.config.parent/"storage","host-native",shared_instance_available=True),), InstallationCandidate(self.config,self.config.parent/"storage","host-native",shared_instance_available=True), False, "selected")
    def runtime_status(self):
        return RuntimeSnapshot(True,True,"1.0",False,[{"name":"LAN","short_name":"LAN","type":"TCPServerInterface","runtime_status":"Up","source":"runtime"}], [{"destination":f"{i:032x}","via":"a"*32,"hops":1,"interface":"LAN","expires":None} for i in range(600)], [], None)
    def config_view(self): return {"available":True,"interfaces":[{"name":"LAN","type":"TCPServerInterface","enabled":True,"mode":"gateway","ifac_present":False,"source":"configured"}],"sections":{}}
    def capabilities(self): return {"observe":True,"labels":True,"drop_path":False,"drop_via":False,"drop_announces":False,"reload_interface":False,"edit_config":False,"restart_service":False,"transport_enable":False}
    def paths(self): return self.runtime_status().paths
    def interfaces(self): return self.runtime_status().interfaces
    def clients(self): return []


def logged_client(tmp_path):
    config=tmp_path/"config"; config.write_text("[reticulum]\n")
    app=create_app({"TESTING":True,"SECRET_KEY":"test","DATABASE":str(tmp_path/"gui.db")},FakeBackend(config))
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


