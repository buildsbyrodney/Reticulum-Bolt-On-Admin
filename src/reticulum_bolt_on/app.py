from __future__ import annotations

import os
import secrets
from functools import wraps
from pathlib import Path
from typing import Any

from flask import Flask, abort, flash, jsonify, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from .backend import ReticulumBackend, SharedInstanceBackend
from .detection import detect
from .parsers import bounded_paths, merge_interfaces, path_health
from .storage import ObserverStore


def create_app(config: dict[str, Any] | None = None, backend: ReticulumBackend | None = None) -> Flask:
    app = Flask(__name__)
    app.config.from_mapping(
        SECRET_KEY=os.environ.get("BOLT_ON_SECRET_KEY") or secrets.token_hex(32),
        DATABASE=os.environ.get("BOLT_ON_DATABASE", str(Path.cwd() / "state" / "observer.db")),
        RETICULUM_CONFIG=os.environ.get("RETICULUM_CONFIG"),
        SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_SECURE=os.environ.get("BOLT_ON_HTTPS") == "1",
    )
    if config: app.config.update(config)
    store = ObserverStore(Path(app.config["DATABASE"]))
    selected = Path(app.config["RETICULUM_CONFIG"]) if app.config.get("RETICULUM_CONFIG") else None
    snapshot_path = Path(os.environ["BOLT_ON_CONFIG_SNAPSHOT"]) if os.environ.get("BOLT_ON_CONFIG_SNAPSHOT") else None
    adapter = backend or SharedInstanceBackend(selected, snapshot_path=snapshot_path,
                                               snapshot_max_age=int(os.environ.get("BOLT_ON_CONFIG_MAX_AGE", "90")))
    app.extensions["observer_store"] = store
    app.extensions["reticulum_backend"] = adapter

    def csrf_token() -> str:
        if "csrf" not in session: session["csrf"] = secrets.token_urlsafe(32)
        return session["csrf"]
    app.jinja_env.globals["csrf_token"] = csrf_token

    def configured() -> bool:
        with store.connect() as con: return con.execute("SELECT 1 FROM admins LIMIT 1").fetchone() is not None

    def login_required(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if not configured(): return redirect(url_for("setup"))
            if not session.get("admin"): return redirect(url_for("login"))
            return view(*args, **kwargs)
        return wrapped

    def require_csrf() -> None:
        supplied = request.form.get("csrf_token", "")
        if not session.get("csrf") or not secrets.compare_digest(session["csrf"], supplied): abort(400, "Invalid CSRF token")

    def observe() -> dict[str, Any]:
        detection = adapter.detect()
        runtime = adapter.runtime_status() if detection.selected and not detection.ambiguous else None
        config_view = adapter.config_view() if detection.selected else {"available": False, "interfaces": [], "sections": {}}
        runtime_interfaces = runtime.interfaces if runtime else []
        interfaces = merge_interfaces(config_view.get("interfaces", []), runtime_interfaces)
        paths = runtime.paths if runtime else []
        labels = store.labels()
        for interface in interfaces: interface["admin_label"] = labels.get(("interface", interface["name"]))
        for path in paths:
            path["destination_label"] = labels.get(("destination", path["destination"]))
            path["transport_label"] = labels.get(("transport", path["via"]))
        health = path_health(paths, labels)
        clients = runtime.clients if runtime else []
        snapshot = {"interfaces": [row["name"] for row in interfaces if row.get("runtime")], "clients": [row["key"] for row in clients], "path_count": len(paths)}
        if runtime: store.record_observation(health, snapshot)
        return {"detection": detection, "runtime": runtime, "config": config_view, "interfaces": interfaces,
                "paths": paths, "health": health, "clients": clients, "labels": labels, "capabilities": adapter.capabilities()}

    @app.context_processor
    def common(): return {"observer_mode": True, "capabilities": adapter.capabilities()}

    @app.route("/setup", methods=["GET", "POST"])
    def setup():
        if configured(): return redirect(url_for("login"))
        if request.method == "POST":
            require_csrf(); username = request.form.get("username", "").strip(); password = request.form.get("password", "")
            if len(username) < 3 or len(password) < 12: flash("Username must be 3+ characters and password 12+ characters.", "error")
            elif password != request.form.get("confirmation"): flash("Passwords do not match.", "error")
            else:
                with store.connect() as con: con.execute("INSERT INTO admins VALUES(?,?,strftime('%s','now'))", (username, generate_password_hash(password)))
                session.clear(); session["admin"] = username; return redirect(url_for("dashboard"))
        return render_template("setup.html")

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if not configured(): return redirect(url_for("setup"))
        if request.method == "POST":
            require_csrf()
            with store.connect() as con: row = con.execute("SELECT * FROM admins WHERE username=?", (request.form.get("username", ""),)).fetchone()
            if row and check_password_hash(row["password_hash"], request.form.get("password", "")):
                session.clear(); session["admin"] = row["username"]; return redirect(url_for("dashboard"))
            flash("Invalid credentials.", "error")
        return render_template("login.html")

    @app.post("/logout")
    @login_required
    def logout(): require_csrf(); session.clear(); return redirect(url_for("login"))

    @app.get("/")
    @login_required
    def dashboard(): return render_template("dashboard.html", **observe())

    @app.get("/interfaces")
    @login_required
    def interfaces(): return render_template("interfaces.html", **observe())

    @app.get("/paths")
    @login_required
    def paths():
        model = observe(); raw, omitted = bounded_paths(model["paths"]); model.update(raw_paths=raw, omitted_paths=omitted)
        return render_template("paths.html", **model)

    @app.get("/clients")
    @login_required
    def clients(): return render_template("clients.html", **observe())

    @app.get("/diagnostics")
    @login_required
    def diagnostics(): return render_template("diagnostics.html", **observe())

    @app.get("/timeline")
    @login_required
    def timeline(): return render_template("timeline.html", events=store.events(), history=store.history())

    @app.post("/labels")
    @login_required
    def labels():
        require_csrf(); kind = request.form.get("kind", ""); value = request.form.get("value", "").strip(); label = request.form.get("label", "").strip()
        if not value or not label or len(label) > 80: abort(400, "Invalid label")
        store.save_label(kind, value, label, request.form.get("notes", "")[:1000])
        return redirect(request.form.get("return_to") or url_for("dashboard"))

    def graph_model(model: dict[str, Any]) -> dict[str, Any]:
        nodes = [{"id": "local", "kind": "local", "name": "Existing Reticulum shared instance"}]; edges = []
        for index, interface in enumerate(model["interfaces"]):
            node_id = f"interface:{index}"; nodes.append({"id": node_id, "kind": "interface", "name": interface["name"]}); edges.append({"source": "local", "target": node_id})
        interface_ids = {node["name"]: node["id"] for node in nodes if node["kind"] == "interface"}
        for client in model["clients"]:
            node_id = "client:" + client["key"]; nodes.append({"id": node_id, "kind": "client", "name": f"{client['remote_ip']}:{client['remote_port']}"})
            edges.append({"source": interface_ids.get(client["parent_interface"], "local"), "target": node_id})
        for index, path in enumerate(model["paths"][:250]):
            node_id = f"path:{index}"; nodes.append({"id": node_id, "kind": "path", "name": path["destination"] or "Unknown destination", "via": path["via"] or None})
            edges.append({"source": interface_ids.get(str(path.get("interface")), "local"), "target": node_id})
        return {"nodes": nodes, "edges": edges, "bounded": len(model["paths"]) > 250, "settle_frames": 180}

    @app.get("/map")
    @login_required
    def network_map(): return render_template("map.html")

    @app.get("/api/map")
    @login_required
    def map_api(): return jsonify(graph_model(observe()))

    @app.get("/api/status")
    @login_required
    def status_api():
        model = observe(); runtime = model["runtime"]
        return jsonify({"detection": model["detection"].public(), "running": runtime.running if runtime else None,
                        "available": runtime.available if runtime else False, "error": runtime.error if runtime else model["detection"].message,
                        "capabilities": model["capabilities"]})

    return app


def main() -> None:
    create_app().run(host=os.environ.get("BOLT_ON_LISTEN", "127.0.0.1"), port=int(os.environ.get("BOLT_ON_PORT", "8080")))


if __name__ == "__main__": main()
