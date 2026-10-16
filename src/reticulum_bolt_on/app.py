from __future__ import annotations

import os
import re
import secrets
import time
from functools import wraps
from pathlib import Path
from typing import Any

from flask import Flask, abort, flash, jsonify, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from .backend import ReticulumBackend, SharedInstanceBackend, SnapshotBackend
from .detection import detect
from .models import ManagementResult
from .parsers import bounded_paths, merge_interfaces, path_health
from .storage import ObserverStore
from .config_manager import ConfigConflict, ConfigManager, ConfigValidationError, INTERFACE_TYPES, render_section, sections, safe_values


HEX32 = re.compile(r"^[0-9a-fA-F]{32}$")
MANAGEMENT_CAPABILITIES = ("drop_path", "drop_via", "drop_announces", "request_path", "blackhole_identity", "unblackhole_identity", "edit_config")


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
    snapshot_dir = Path(os.environ["BOLT_ON_SNAPSHOT_DIR"]) if os.environ.get("BOLT_ON_SNAPSHOT_DIR") else None
    adapter = backend or (
        SnapshotBackend(snapshot_dir, snapshot_max_age=int(os.environ.get("BOLT_ON_CONFIG_MAX_AGE", "90")))
        if snapshot_dir else SharedInstanceBackend(selected)
    )
    app.extensions["observer_store"] = store
    app.extensions["reticulum_backend"] = adapter
    app.extensions["pending_config_reviews"] = {}

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
            path["management_target"] = bool(HEX32.fullmatch(str(path.get("destination") or "")))
        health = path_health(paths, labels, total_count=runtime.path_count if runtime else None)
        clients = runtime.clients if runtime else []
        snapshot = {"interfaces": [row["name"] for row in interfaces if row.get("runtime")], "clients": [row["key"] for row in clients], "path_count": len(paths)}
        if runtime: store.record_observation(health, snapshot)
        capabilities = dict(adapter.capabilities(), edit_config=config_write_available())
        return {"detection": detection, "runtime": runtime, "config": config_view, "interfaces": interfaces,
                "paths": paths, "health": health, "clients": clients, "labels": labels, "capabilities": capabilities}

    def config_manager() -> ConfigManager | None:
        # Snapshot/Docker configurations are deliberately read-only. Only a selected,
        # directly writable native config gets this local writer.
        path = getattr(adapter, "config_path", None)
        if not path or getattr(adapter, "snapshot_path", None): return None
        return ConfigManager(Path(path))

    def config_write_available() -> bool:
        manager = config_manager()
        return bool(manager and manager.path.is_file() and os.access(manager.path, os.W_OK))

    @app.context_processor
    def common():
        capabilities = adapter.capabilities()
        capabilities = dict(capabilities, edit_config=config_write_available())
        return {"observer_mode": True, "capabilities": capabilities,
                "management_available": any(capabilities.get(name, False) for name in MANAGEMENT_CAPABILITIES)}

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

    @app.get("/interfaces/config")
    @login_required
    def interface_config():
        model = observe()
        return render_template("interface_config.html", **model, writable=config_write_available(), supported_types=sorted(INTERFACE_TYPES))

    @app.route("/deployment", methods=["GET", "POST"])
    @login_required
    def deployment():
        if request.method == "POST":
            require_csrf(); choice = request.form.get("deployment_type", "")
            if choice not in {"linux-systemd", "linux-manual", "windows", "docker-compose", "other"}: abort(400, "Invalid deployment type")
            store.save_setting("deployment_type", choice); flash("Deployment preference saved. Runtime evidence will still be required for lifecycle actions.", "info")
            return redirect(url_for("deployment"))
        model = observe(); selected = model["detection"].selected
        saved = store.setting("deployment_type", "other")
        # No lifecycle command is inferred from preference alone in v0.3.0.
        return render_template("deployment.html", selected=selected, saved=saved, config_writable=config_write_available(),
                               restart_available=False, evidence="Ambiguous — no lifecycle action is enabled until an exact runtime correlation is implemented.")

    @app.get("/paths")
    @login_required
    def paths():
        model = observe(); raw, omitted = bounded_paths(model["paths"])
        if model["runtime"] and model["runtime"].path_count is not None:
            omitted = max(omitted, model["runtime"].path_count - len(raw))
        model.update(raw_paths=raw, omitted_paths=omitted)
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

    @app.get("/management")
    @login_required
    def management():
        return render_template("management.html", **observe())

    @app.get("/incident-recovery")
    @login_required
    def incident_recovery(): return render_template("incident_recovery.html", **observe())

    def require_capability(name: str) -> None:
        if not adapter.capabilities().get(name, False):
            abort(403, "Management capability is disabled for this backend/deployment")

    def pending_review(action: str, target: str, title: str, explanation: str,
                       details: list[tuple[str, str]], metadata: dict[str, Any] | None = None):
        session["pending_management"] = {
            "action": action, "target": target, "issued_at": int(time.time()), "metadata": metadata or {},
        }
        return render_template("management_review.html", action=action, target=target, title=title,
                               explanation=explanation, details=details)

    @app.post("/management/drop-path/review")
    @login_required
    def review_drop_path():
        require_csrf(); require_capability("drop_path")
        destination = request.form.get("destination", "").strip().lower()
        if not HEX32.fullmatch(destination): abort(400, "Destination must be exactly 32 hexadecimal characters")
        model = observe()
        current = next((path for path in model["paths"] if path.get("destination", "").lower() == destination), None)
        details = [("Destination", destination), ("Currently observed", "Yes" if current else "No")]
        if current:
            details.extend((("Via", current.get("via") or "Unavailable"),
                            ("Interface", str(current.get("interface") or "Unavailable"))))
        return pending_review("drop_path", destination, "Review destination path removal",
                              "This removes only the selected destination path from the existing Reticulum instance.", details)

    @app.post("/management/drop-via/review")
    @login_required
    def review_drop_via():
        require_csrf(); require_capability("drop_via")
        via = request.form.get("via", "").strip().lower()
        if not HEX32.fullmatch(via): abort(400, "Via transport must be exactly 32 hexadecimal characters")
        model = observe()
        matching = [path for path in model["paths"] if str(path.get("via") or "").lower() == via]
        if not matching: abort(400, "No current paths have the selected exact via transport")
        samples = [str(path.get("destination")) for path in matching[:10] if path.get("destination")]
        details = [("Transport hash", via), ("Current matching paths", str(len(matching))),
                   ("Bounded destination samples", ", ".join(samples) or "Unavailable")]
        return pending_review("drop_via", via, "Review transport path removal",
                              "This removes all currently known paths learned through this exact transport hash.", details,
                              {"matching_path_count": len(matching), "sample_count": len(samples)})

    @app.post("/management/drop-announces/review")
    @login_required
    def review_drop_announces():
        require_csrf(); require_capability("drop_announces")
        return pending_review("drop_announces", "all interfaces", "Review queued-announce drop",
                              "This drops queued announces on all interfaces. It does not remove destination paths.",
                              [("Scope", "All interface announce queues"), ("Paths", "Not removed")])

    @app.post("/management/request-path/review")
    @login_required
    def review_request_path():
        require_csrf(); require_capability("request_path")
        destination = request.form.get("destination", "").strip().lower()
        if not HEX32.fullmatch(destination): abort(400, "Destination must be exactly 32 hexadecimal characters")
        return pending_review("request_path", destination, "Review path request", "This asks the existing shared Reticulum instance to request one exact destination path. It never starts another Reticulum instance.", [("Destination", destination)])

    @app.post("/management/<identity_action>/review-identity")
    @login_required
    def review_identity(identity_action: str):
        if identity_action not in {"blackhole", "unblackhole"}: abort(404)
        require_csrf(); action = f"{identity_action}_identity"; require_capability(action)
        identity = request.form.get("identity", "").strip().lower()
        if not HEX32.fullmatch(identity): abort(400, "Identity must be exactly 32 hexadecimal characters")
        return pending_review(action, identity, f"Review {identity_action} identity", "This operates only on the exact supplied Reticulum identity hash; no identity is inferred from network addresses.", [("Identity", identity)])

    @app.post("/management/interface/review")
    @login_required
    def review_interface():
        require_csrf()
        operation = request.form.get("operation", "").strip().lower()
        capability = f"{operation}_interface"
        if operation not in {"attach", "detach", "reload"}: abort(400, "Invalid interface operation")
        require_capability(capability)
        name = request.form.get("name", "").strip()
        if not name or len(name) > 128 or any(ord(char) < 32 for char in name): abort(400, "Invalid interface name")
        model = observe()
        interface = next((row for row in model["interfaces"] if str(row.get("name")) == name), None)
        if not interface: abort(400, "Interface is not currently known")
        if not interface.get("configured"): abort(400, "Runtime-only/internal interfaces cannot be managed")
        if operation == "attach" and interface.get("runtime"): abort(400, "Interface is already attached")
        if operation in {"detach", "reload"} and not interface.get("runtime"): abort(400, "Interface is not currently attached")
        return pending_review(capability, name, f"Review interface {operation}",
                              f"This asks the existing Reticulum shared instance to {operation} only this interface. It does not restart rnsd.",
                              [("Interface", name), ("Operation", operation)], {"interface_operation": operation})

    @app.post("/management/<action>/execute")
    @login_required
    def execute_management(action: str):
        require_csrf()
        pending = session.get("pending_management") or {}
        if pending.get("action") != action or int(time.time()) - int(pending.get("issued_at") or 0) > 300:
            abort(400, "Management review is missing or expired")
        if request.form.get("confirm") != "yes": abort(400, "Explicit confirmation is required")
        require_capability(action)
        target = str(pending.get("target") or "")
        if action in {"drop_path", "drop_via"} and not HEX32.fullmatch(target):
            abort(400, "Reviewed management target is invalid")
        try:
            if action == "drop_path": result = adapter.drop_path(target)
            elif action == "drop_via": result = adapter.drop_paths_via(target)
            elif action == "drop_announces": result = adapter.drop_announce_queues()
            elif action == "request_path": result = adapter.request_path(target)
            elif action in {"blackhole_identity", "unblackhole_identity"}: result = adapter.manage_identity(action.replace("_", "-"), target)
            elif action in {"attach_interface", "detach_interface", "reload_interface"}:
                result = adapter.manage_interface(action.removesuffix("_interface"), target)
            else: abort(400, "Unsupported management action")
        except Exception:
            result = ManagementResult(False, "The management backend failed safely; no unrelated action was attempted")
        metadata = dict(pending.get("metadata") or {})
        metadata.update(result.metadata)
        store.record_audit(action, target, result.success, metadata)
        session.pop("pending_management", None)
        return render_template("management_result.html", action=action, target=target, result=result)

    def _interface_evidence(model: dict[str, Any], name: str) -> dict[str, bool]:
        row = next((item for item in model["interfaces"] if item.get("name") == name), {})
        traffic = bool((row.get("rx_bytes") or 0) + (row.get("tx_bytes") or 0))
        up = row.get("runtime_status") == "Up" or bool(row.get("runtime"))
        associated = any(str(path.get("interface") or "") == name for path in model["paths"])
        return {"up": up, "traffic": traffic, "path_association": associated}

    def _posted_values(existing: dict[str, str] | None = None) -> dict[str, str]:
        values = dict(existing or {})
        for key in ("type", "enabled", "mode", "target_host", "target_port", "listen_host", "listen_ip", "listen_port", "port", "device", "group_id"):
            if key in request.form and request.form.get(key, "").strip(): values[key] = request.form[key].strip()
        if "enabled" in request.form: values["enabled"] = "yes" if request.form.get("enabled") in {"yes", "true", "1", "on"} else "no"
        return values

    def _store_config_review(action: str, target: str, expected: str, content: str, metadata: dict[str, Any]) -> str:
        token = secrets.token_urlsafe(24)
        app.extensions["pending_config_reviews"][token] = {"action": action, "target": target, "expected": expected,
            "content": content, "metadata": metadata, "issued_at": time.time()}
        session["pending_config_review"] = token
        return token

    @app.post("/interfaces/config/review")
    @login_required
    def review_interface_config():
        require_csrf(); manager = config_manager()
        if not manager or not config_write_available(): abort(403, "Persistent configuration is read-only for this deployment")
        operation = request.form.get("operation", "").strip().lower()
        if operation not in {"create", "edit", "clone", "enable", "disable", "delete"}: abort(400, "Invalid interface operation")
        before, expected = manager.read(); available = {item.name: item for item in sections(before)}
        name = request.form.get("name", "").strip(); original = available.get(name)
        if operation == "create":
            name = request.form.get("new_name", "").strip()
            if name in available: abort(400, "Interface section name already exists")
            original = None
        elif not original: abort(400, "Configured interface was not found")
        if operation == "clone":
            new_name = request.form.get("new_name", "").strip()
            if new_name in available: abort(400, "Interface section name already exists")
            values = _posted_values(original.values()); values["enabled"] = "no"
            replacement = render_section(new_name, values, original)
            after, diff = manager.preview(before, replacement, None)
            target, action = new_name, "clone_interface"
        elif operation == "delete":
            after, diff = manager.delete_preview(before, original); target, action = name, "delete_interface"
        else:
            values = _posted_values(original.values() if original else {})
            if operation in {"enable", "disable"}: values["enabled"] = "yes" if operation == "enable" else "no"
            if operation == "edit" and values.get("type") == "TCPClientInterface" and values.get("target_host") != original.values().get("target_host"):
                evidence = _interface_evidence(observe(), name)
                if any(evidence.values()): abort(400, "This interface has observed Reticulum connectivity. Create or clone a replacement instead of changing target_host.")
            replacement = render_section(name, values, original)
            after, diff = manager.preview(before, replacement, original)
            target, action = name, f"{operation}_interface"
        backup_clone = operation == "edit" and request.form.get("backup_clone") == "yes"
        if backup_clone:
            clone_name = f"{name} - Backup {time.strftime('%Y-%m-%d %H%M')}"
            if clone_name in available: clone_name += " 2"
            clone_values = original.values(); clone_values["enabled"] = "no"
            clone_lines = render_section(clone_name, clone_values, original)
            # Insert backup before the edited original, preserving its full section including hidden credentials.
            lines = after.splitlines(keepends=True); replacement_at = next(item.start for item in sections(after) if item.name == name)
            lines[replacement_at:replacement_at] = clone_lines + ["\n"]
            after = "".join(lines)
            diff = "".join(__import__("difflib").unified_diff(before.splitlines(True), after.splitlines(True), "config (current)", "config (proposed)"))
        _store_config_review(action, target, expected, after, {"backup_clone": backup_clone, "secret_fields_present": any("[present, hidden]" in value for value in safe_values((original.values() if original else {})).values())})
        details = [("Action", action.replace("_", " ")), ("Interface", target), ("Backup clone", "A disabled backup clone will copy hidden credentials." if backup_clone else "Not selected"),
                   ("Restart", "Save only — lifecycle evidence is not positively identified")]
        return render_template("config_review.html", action=action, target=target, details=details, diff=diff, restart_available=False)

    @app.post("/interfaces/config/execute")
    @login_required
    def execute_interface_config():
        require_csrf(); token = session.get("pending_config_review"); pending = app.extensions["pending_config_reviews"].pop(token, None)
        session.pop("pending_config_review", None)
        if not pending or time.time() - pending["issued_at"] > 300: abort(400, "Configuration review is missing or expired")
        if request.form.get("confirm") != "yes": abort(400, "Explicit confirmation is required")
        manager = config_manager()
        if not manager: abort(403, "Persistent configuration is read-only for this deployment")
        try: backup = manager.apply(pending["expected"], pending["content"])
        except ConfigConflict as error: abort(409, str(error))
        except (ConfigValidationError, OSError) as error: abort(400, f"Configuration was not written: {error}")
        metadata = dict(pending["metadata"]); metadata["backup_created"] = True; metadata["restart_requested"] = False
        store.record_audit(pending["action"], pending["target"], True, metadata)
        flash(f"Configuration saved atomically. Backup created at {backup.name}. Restart Reticulum manually to apply changes.", "info")
        return redirect(url_for("interfaces"))

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
