from __future__ import annotations

import json
import time
import subprocess
from abc import ABC, abstractmethod
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable

from .detection import detect
from .models import DetectionResult, RuntimeSnapshot
from .parsers import find_interfaces, normalize_paths, parse_config_interfaces, proven_clients

Runner = Callable[[list[str]], subprocess.CompletedProcess[str]]
SENSITIVE_CONFIG_FIELDS = frozenset({
    "ifac_key", "ifac_netkey", "ifac_netname", "ifac_passphrase",
    "ifac_signature", "network_key", "network_name", "passphrase",
})


class ReticulumBackend(ABC):
    @abstractmethod
    def detect(self) -> DetectionResult: ...
    @abstractmethod
    def runtime_status(self) -> RuntimeSnapshot: ...
    @abstractmethod
    def paths(self) -> list[dict[str, Any]]: ...
    @abstractmethod
    def interfaces(self) -> list[dict[str, Any]]: ...
    @abstractmethod
    def clients(self) -> list[dict[str, Any]]: ...
    @abstractmethod
    def config_view(self) -> dict[str, Any]: ...
    @abstractmethod
    def capabilities(self) -> dict[str, bool]: ...


def command_runner(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, capture_output=True, text=True, timeout=8, check=False)


class SharedInstanceBackend(ReticulumBackend):
    """Observer adapter that only invokes read-only RNS utilities.

    It never imports/constructs ``RNS.Reticulum`` because construction can
    initialise a local instance. A failed shared-instance query stays failed.
    """
    def __init__(self, config_path: Path | None = None, runner: Runner = command_runner, detector=detect,
                 snapshot_path: Path | None = None, snapshot_max_age: int = 90):
        self.config_path = config_path
        self.runner = runner
        self.detector = detector
        self.snapshot_path = snapshot_path
        self.snapshot_max_age = snapshot_max_age
        self._snapshot: RuntimeSnapshot | None = None

    def detect(self) -> DetectionResult:
        result = self.detector(selected_config=self.config_path)
        snapshot = self._sanitized_snapshot()
        if not snapshot or not result.selected:
            return result
        source = snapshot.get("source") or {}
        selected = replace(result.selected,
            config_path=Path(source.get("config_path") or result.selected.config_path),
            storage_path=Path(source["storage_path"]) if source.get("storage_path") else None,
            deployment_type=snapshot.get("deployment_type") or result.selected.deployment_type,
            service_manager="external/docker" if snapshot.get("deployment_type") == "docker" else result.selected.service_manager)
        return replace(result, candidates=(selected,), selected=selected)

    def _sanitized_snapshot(self) -> dict[str, Any] | None:
        if not self.snapshot_path:
            return None
        try:
            return json.loads(self.snapshot_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def _run_json(self, command: list[str]) -> tuple[Any | None, str | None]:
        try: result = self.runner(command)
        except (OSError, subprocess.SubprocessError) as error: return None, str(error)
        if result.returncode != 0: return None, (result.stderr or result.stdout or "command unavailable").strip()
        try: return json.loads(result.stdout), None
        except json.JSONDecodeError: return None, "Reticulum utility returned invalid JSON"

    def runtime_status(self) -> RuntimeSnapshot:
        prefix = ["--config", str(self.config_path.parent)] if self.config_path else []
        status, error = self._run_json(["rnstatus", *prefix, "--json"])
        if error:
            self._snapshot = RuntimeSnapshot(available=False, running=None, error="Shared-instance attachment unavailable: " + error)
            return self._snapshot
        path_data, path_error = self._run_json(["rnpath", *prefix, "--table", "--json"])
        runtime_interfaces = find_interfaces(status)
        _, configured = parse_config_interfaces(self.config_path.read_text(encoding="utf-8", errors="replace")) if self.config_path and self.config_path.is_file() else ({}, [])
        paths = normalize_paths(path_data) if path_data is not None else []
        version = status.get("rns_version") or status.get("version") if isinstance(status, dict) else None
        transport = status.get("transport_enabled") if isinstance(status, dict) else None
        if isinstance(status, dict) and transport is None and "transport_id" in status:
            transport = bool(status.get("transport_id"))
        self._snapshot = RuntimeSnapshot(True, True, version, transport, runtime_interfaces, paths,
                                         proven_clients(runtime_interfaces, configured), path_error)
        return self._snapshot

    def _current(self) -> RuntimeSnapshot:
        return self._snapshot or self.runtime_status()

    def paths(self) -> list[dict[str, Any]]: return self._current().paths
    def interfaces(self) -> list[dict[str, Any]]: return self._current().interfaces
    def clients(self) -> list[dict[str, Any]]: return self._current().clients

    def config_view(self) -> dict[str, Any]:
        snapshot = self._sanitized_snapshot()
        if snapshot is not None:
            status_path = self.snapshot_path.with_name("refresh-status.json") if self.snapshot_path else None
            try: refresh = json.loads(status_path.read_text(encoding="utf-8")) if status_path else {}
            except (OSError, json.JSONDecodeError): refresh = {"ok": False, "error": "Sanitized config refresh status is unavailable"}
            checked_at = int(refresh.get("checked_at") or 0)
            stale = not refresh.get("ok") or refresh.get("source_sha256") != (snapshot.get("source") or {}).get("sha256")
            if checked_at and time.time() - checked_at > self.snapshot_max_age: stale = True
            return {"available": True, "path": (snapshot.get("source") or {}).get("config_path"),
                    "sections": {"reticulum": {"enable_transport": snapshot.get("transport_enabled")}},
                    "interfaces": snapshot.get("interfaces") or [], "sanitized": True,
                    "storage_readable": snapshot.get("storage_readable_at_refresh"), "stale": stale,
                    "refresh_error": refresh.get("error"), "refreshed_at": snapshot.get("refreshed_at"), "checked_at": checked_at}
        if not self.config_path: return {"available": False, "error": "No configuration selected", "sections": {}, "interfaces": []}
        try:
            sections, interfaces = parse_config_interfaces(self.config_path.read_text(encoding="utf-8", errors="replace"))
            # Values are exposed for viewing, but IFAC credentials are redacted.
            for settings in sections.values():
                for key in list(settings):
                    lowered = key.lower()
                    if lowered in SENSITIVE_CONFIG_FIELDS or "pass" in lowered or "secret" in lowered:
                        settings[key] = "[present, hidden]"
            return {"available": True, "path": str(self.config_path), "sections": sections, "interfaces": interfaces}
        except OSError as error:
            return {"available": False, "error": str(error), "sections": {}, "interfaces": []}

    def capabilities(self) -> dict[str, bool]:
        return {"observe": True, "labels": True, "drop_path": False, "drop_via": False, "drop_announces": False,
                "reload_interface": False, "edit_config": False, "restart_service": False, "transport_enable": False}

