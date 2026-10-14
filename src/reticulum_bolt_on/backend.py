from __future__ import annotations

import json
import subprocess
from abc import ABC, abstractmethod
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
    def __init__(self, config_path: Path | None = None, runner: Runner = command_runner, detector=detect):
        self.config_path = config_path
        self.runner = runner
        self.detector = detector
        self._snapshot: RuntimeSnapshot | None = None

    def detect(self) -> DetectionResult:
        return self.detector(selected_config=self.config_path)

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
        self._snapshot = RuntimeSnapshot(True, True, version, transport, runtime_interfaces, paths,
                                         proven_clients(runtime_interfaces, configured), path_error)
        return self._snapshot

    def _current(self) -> RuntimeSnapshot:
        return self._snapshot or self.runtime_status()

    def paths(self) -> list[dict[str, Any]]: return self._current().paths
    def interfaces(self) -> list[dict[str, Any]]: return self._current().interfaces
    def clients(self) -> list[dict[str, Any]]: return self._current().clients

    def config_view(self) -> dict[str, Any]:
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

