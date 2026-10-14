from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any


class ObserverStore:
    def __init__(self, path: Path): self.path = path

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(self.path)
        con.row_factory = sqlite3.Row
        con.executescript("""
        CREATE TABLE IF NOT EXISTS admins(username TEXT PRIMARY KEY,password_hash TEXT NOT NULL,created_at INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS labels(kind TEXT NOT NULL,value TEXT NOT NULL,label TEXT NOT NULL,notes TEXT,updated_at INTEGER NOT NULL,PRIMARY KEY(kind,value));
        CREATE TABLE IF NOT EXISTS path_history(id INTEGER PRIMARY KEY,observed_at INTEGER NOT NULL,total_paths INTEGER NOT NULL,unique_vias INTEGER NOT NULL,top_via TEXT,top_count INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,occurred_at INTEGER NOT NULL,kind TEXT NOT NULL,severity TEXT NOT NULL,message TEXT NOT NULL,detail_json TEXT);
        CREATE TABLE IF NOT EXISTS observer_state(state_key TEXT PRIMARY KEY,value_json TEXT NOT NULL,updated_at INTEGER NOT NULL);
        """)
        return con

    def labels(self) -> dict[tuple[str, str], str]:
        with self.connect() as con: return {(row["kind"], row["value"]): row["label"] for row in con.execute("SELECT * FROM labels")}

    def save_label(self, kind: str, value: str, label: str, notes: str = "") -> None:
        if kind not in {"destination", "transport", "interface"}: raise ValueError("unsupported label kind")
        with self.connect() as con:
            con.execute("INSERT INTO labels(kind,value,label,notes,updated_at) VALUES(?,?,?,?,?) ON CONFLICT(kind,value) DO UPDATE SET label=excluded.label,notes=excluded.notes,updated_at=excluded.updated_at", (kind, value, label, notes, int(time.time())))

    def record_observation(self, health: dict[str, Any], snapshot: dict[str, Any]) -> None:
        now = int(time.time()); top = health.get("top") or {}
        with self.connect() as con:
            con.execute("INSERT INTO path_history(observed_at,total_paths,unique_vias,top_via,top_count) VALUES(?,?,?,?,?)", (now, health["total"], health["unique_vias"], top.get("via"), top.get("count", 0)))
            prior = con.execute("SELECT value_json FROM observer_state WHERE state_key='latest'").fetchone()
            old = json.loads(prior[0]) if prior else {}
            old_names, names = set(old.get("interfaces", [])), set(snapshot.get("interfaces", []))
            for name in sorted(names - old_names): self._event(con, now, "interface_appeared", "info", f"Interface appeared: {name}")
            for name in sorted(old_names - names): self._event(con, now, "interface_disappeared", "warning", f"Interface disappeared: {name}")
            old_clients, clients = set(old.get("clients", [])), set(snapshot.get("clients", []))
            for item in sorted(clients - old_clients): self._event(con, now, "client_connected", "info", f"TCP client connected: {item}")
            for item in sorted(old_clients - clients): self._event(con, now, "client_disconnected", "info", f"TCP client disconnected: {item}")
            if health["total"] >= 100 and health["total"] > max(100, int(old.get("path_count", 0)) * 2): self._event(con, now, "path_growth", "warning", f"Path count grew to {health['total']}")
            if any("concentration" in warning.lower() for warning in health["warnings"]): self._event(con, now, "path_concentration", "warning", "High concentration of learned paths through one transport")
            con.execute("INSERT INTO observer_state(state_key,value_json,updated_at) VALUES('latest',?,?) ON CONFLICT(state_key) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at", (json.dumps(snapshot), now))
            con.execute("DELETE FROM path_history WHERE id NOT IN (SELECT id FROM path_history ORDER BY id DESC LIMIT 500)")
            con.execute("DELETE FROM events WHERE id NOT IN (SELECT id FROM events ORDER BY id DESC LIMIT 1000)")

    @staticmethod
    def _event(con: sqlite3.Connection, at: int, kind: str, severity: str, message: str) -> None:
        con.execute("INSERT INTO events(occurred_at,kind,severity,message) VALUES(?,?,?,?)", (at, kind, severity, message))

    def history(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.connect() as con: return [dict(row) for row in con.execute("SELECT * FROM path_history ORDER BY id DESC LIMIT ?", (limit,))]

    def events(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.connect() as con: return [dict(row) for row in con.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))]

