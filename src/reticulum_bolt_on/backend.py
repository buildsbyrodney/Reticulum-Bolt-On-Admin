from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import time
import subprocess
from abc import ABC, abstractmethod
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable

from .detection import detect
from .models import DetectionResult, InstallationCandidate, ManagementResult, RuntimeSnapshot
from .parsers import find_interfaces, normalize_paths, parse_config_interfaces, proven_clients

Runner = Callable[[list[str]], subprocess.CompletedProcess[str]]
HEX32 = re.compile(r"^[0-9a-fA-F]{32}$")
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

    def drop_path(self, destination: str) -> ManagementResult:
        return ManagementResult(False, "Drop path is unavailable for this backend")

    def drop_paths_via(self, via: str) -> ManagementResult:
        return ManagementResult(False, "Drop paths via transport is unavailable for this backend")

    def drop_announce_queues(self) -> ManagementResult:
        return ManagementResult(False, "Drop queued announces is unavailable for this backend")

    def manage_interface(self, operation: str, name: str) -> ManagementResult:
        return ManagementResult(False, "Per-interface management is unavailable for this backend")


def command_runner(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, capture_output=True, text=True, timeout=8, check=False)


class RNSManagementClient:
    """Narrow native boundary that refuses independent RNS fallback."""

    ACTIONS = frozenset({"drop-path", "drop-via", "drop-announces", "attach-interface", "detach-interface", "reload-interface"})

    def __init__(self, config_dir: Path, interpreter: Path | None = None, runner: Runner = command_runner):
        self.config_dir = config_dir
        self.interpreter = interpreter or self._discover_interpreter()
        self.runner = runner
        self._supported: frozenset[str] | None = None

    @staticmethod
    def _discover_interpreter() -> Path | None:
        utility = shutil.which("rnstatus")
        if not utility:
            return None
        try:
            first_line = Path(utility).open(encoding="utf-8", errors="replace").readline().strip()
        except OSError:
            return None
        if not first_line.startswith("#!"):
            return None
        try:
            shebang = shlex.split(first_line[2:].strip())
        except ValueError:
            return None
        if not shebang:
            return None
        candidate = Path(shebang[0])
        if candidate.name == "env":
            arguments = shebang[1:]
            if arguments[:1] == ["-S"]:
                arguments = arguments[1:]
            interpreter = next((item for item in arguments if not item.startswith("-") and "=" not in item), None)
            resolved = shutil.which(interpreter) if interpreter else None
            if not resolved:
                return None
            candidate = Path(resolved)
        return candidate if candidate.is_absolute() and candidate.is_file() and os.access(candidate, os.X_OK) else None

    @property
    def available(self) -> bool:
        return self.interpreter is not None and self.config_dir.is_dir()

    def _payload(self, command: list[str]) -> tuple[dict[str, Any] | None, str | None]:
        try:
            result = self.runner(command)
        except (OSError, subprocess.SubprocessError) as error:
            return None, str(error)
        for line in reversed(result.stdout.splitlines()):
            try:
                candidate = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(candidate, dict) and "success" in candidate:
                return candidate, None
        return None, (result.stderr or result.stdout or "no safe result returned").strip()[:500]

    def supports(self, action: str) -> bool:
        if action not in self.ACTIONS or not self.available:
            return False
        if self._supported is None:
            command = [str(self.interpreter), str(Path(__file__).with_name("management_helper.py")),
                       "--config", str(self.config_dir), "probe"]
            payload, _ = self._payload(command)
            self._supported = frozenset(payload.get("capabilities") or ()) if payload and payload.get("success") else frozenset()
        return action in self._supported

    def execute(self, action: str, target: str | None = None) -> ManagementResult:
        if action not in self.ACTIONS or not self.available:
            return ManagementResult(False, "Native shared-instance management is unavailable")
        command = [str(self.interpreter), str(Path(__file__).with_name("management_helper.py")),
                   "--config", str(self.config_dir), action]
        if target is not None:
            command.append(target)
        payload, error = self._payload(command)
        if payload is None:
            return ManagementResult(False, f"Management helper failed: {error or 'no safe result returned'}")
        return ManagementResult(bool(payload.get("success")), str(payload.get("message") or "No result message")[:500])


class SharedInstanceBackend(ReticulumBackend):
    """Observer adapter with a separately gated native management boundary.

    Observer reads never import or construct ``RNS.Reticulum``. Management is
    delegated to a helper that requires an existing shared instance, so a
    failed attachment can never initialise an independent network instance.
    """
    def __init__(self, config_path: Path | None = None, runner: Runner = command_runner, detector=detect,
                 snapshot_path: Path | None = None, snapshot_max_age: int = 90,
                 manager: RNSManagementClient | None = None):
        self.config_path = config_path
        self.runner = runner
        self.detector = detector
        self.snapshot_path = snapshot_path
        self.snapshot_max_age = snapshot_max_age
        self._snapshot: RuntimeSnapshot | None = None
        self.manager = manager or (RNSManagementClient(config_path.parent) if config_path else None)

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
                                         proven_clients(runtime_interfaces, configured), path_error,
                                         path_count=len(paths), paths_truncated=False)
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
        def supported(action: str) -> bool:
            if not self.config_path or not self.manager or self.snapshot_path or not self.manager.available:
                return False
            probe = getattr(self.manager, "supports", None)
            return bool(probe(action) if probe else True)
        return {"observe": True, "labels": True, "drop_path": supported("drop-path"),
                "drop_via": supported("drop-via"), "drop_announces": supported("drop-announces"),
                "attach_interface": supported("attach-interface"), "detach_interface": supported("detach-interface"),
                "reload_interface": supported("reload-interface"), "edit_config": False, "restart_service": False,
                "transport_enable": False}

    def _managed(self, action: str, target: str | None = None) -> ManagementResult:
        probe = getattr(self.manager, "supports", None) if self.manager else None
        if not self.manager or not self.manager.available or self.snapshot_path or (probe and not probe(action)):
            return ManagementResult(False, "Management capability is disabled for this deployment")
        result = self.manager.execute(action, target)
        if not result.success:
            return result
        self._snapshot = None
        refreshed = self.runtime_status()
        metadata = {"path_state_refreshed": bool(refreshed.available)}
        message = result.message
        if not refreshed.available:
            message += "; action succeeded but refreshed runtime state is unavailable"
        return ManagementResult(True, message, metadata)

    def drop_path(self, destination: str) -> ManagementResult:
        if not HEX32.fullmatch(destination):
            return ManagementResult(False, "Destination must be exactly 32 hexadecimal characters")
        return self._managed("drop-path", destination)

    def drop_paths_via(self, via: str) -> ManagementResult:
        if not HEX32.fullmatch(via):
            return ManagementResult(False, "Via transport must be exactly 32 hexadecimal characters")
        return self._managed("drop-via", via)

    def drop_announce_queues(self) -> ManagementResult:
        return self._managed("drop-announces")

    def manage_interface(self, operation: str, name: str) -> ManagementResult:
        if operation not in {"attach", "detach", "reload"}:
            return ManagementResult(False, "Unsupported interface operation")
        if not name or len(name) > 128 or any(ord(char) < 32 for char in name):
            return ManagementResult(False, "Interface name is invalid")
        return self._managed(f"{operation}-interface", name)


class SnapshotBackend(ReticulumBackend):
    """Credential-free adapter for snapshots published by the root helper."""

    def __init__(self, snapshot_dir: Path, snapshot_max_age: int = 90):
        self.snapshot_dir = snapshot_dir
        self.snapshot_max_age = snapshot_max_age
        self._snapshot: RuntimeSnapshot | None = None

    def _read(self, name: str) -> dict[str, Any] | None:
        try:
            value = json.loads((self.snapshot_dir / name).read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else None
        except (OSError, json.JSONDecodeError):
            return None

    def _refresh(self) -> tuple[dict[str, Any] | None, bool, str | None]:
        refresh = self._read("refresh-status.json")
        if not refresh:
            return None, True, "Observation helper status is unavailable"
        checked_at = int(refresh.get("checked_at") or 0)
        stale = not refresh.get("ok") or not checked_at or time.time() - checked_at > self.snapshot_max_age
        error = refresh.get("error")
        if stale and not error:
            error = "Observation helper has not checked in recently"
        return refresh, stale, error

    def detect(self) -> DetectionResult:
        config = self._read("config-view.json")
        if not config:
            return DetectionResult(message="Sanitized observer configuration is unavailable.")
        source = config.get("source") or {}
        runtime = self._read("runtime-status.json") or {}
        _, stale, _ = self._refresh()
        candidate = InstallationCandidate(
            config_path=Path(source.get("config_path") or "Unavailable"),
            storage_path=Path(source["storage_path"]) if source.get("storage_path") else None,
            deployment_type=config.get("deployment_type") or "external",
            service_manager="external/docker" if config.get("deployment_type") == "docker" else "external",
            rns_version=runtime.get("rns_version"),
            rnsd_running=None if stale else runtime.get("running"),
            shared_instance_available=False if stale else runtime.get("available"),
            evidence=("sanitized observation-helper snapshot",),
        )
        return DetectionResult(candidates=(candidate,), selected=candidate,
                               message="Existing Reticulum installation selected through the read-only observation helper.")

    def runtime_status(self) -> RuntimeSnapshot:
        refresh, stale, error = self._refresh()
        if stale:
            self._snapshot = RuntimeSnapshot(available=False, running=None, error=error or "Observation unavailable")
            return self._snapshot
        runtime = self._read("runtime-status.json")
        paths = self._read("paths.json")
        config = self._read("config-view.json")
        if not runtime or not paths or not config:
            self._snapshot = RuntimeSnapshot(available=False, running=None, error="Sanitized observation snapshot is incomplete")
            return self._snapshot
        generation = int(refresh.get("checked_at") or 0) if refresh else 0
        if any(int(item.get(key) or 0) != generation for item, key in (
            (runtime, "collected_at"), (paths, "collected_at"), (config, "refreshed_at")
        )):
            self._snapshot = RuntimeSnapshot(available=False, running=None, error="Sanitized observation snapshot generation mismatch")
            return self._snapshot
        self._snapshot = RuntimeSnapshot(
            available=bool(runtime.get("available")),
            running=runtime.get("running"),
            rns_version=runtime.get("rns_version"),
            transport_enabled=runtime.get("transport_enabled"),
            interfaces=runtime.get("interfaces") or [],
            paths=paths.get("rows") or [],
            clients=runtime.get("clients") or [],
            error=None,
            path_count=int(paths.get("total_count") or len(paths.get("rows") or [])),
            paths_truncated=bool(paths.get("truncated")),
        )
        return self._snapshot

    def _current(self) -> RuntimeSnapshot:
        return self._snapshot or self.runtime_status()

    def paths(self) -> list[dict[str, Any]]: return self._current().paths
    def interfaces(self) -> list[dict[str, Any]]: return self._current().interfaces
    def clients(self) -> list[dict[str, Any]]: return self._current().clients

    def config_view(self) -> dict[str, Any]:
        config = self._read("config-view.json")
        refresh, stale, error = self._refresh()
        if not config:
            return {"available": False, "error": "Sanitized configuration snapshot is unavailable", "sections": {}, "interfaces": [], "stale": True}
        source = config.get("source") or {}
        return {
            "available": True,
            "path": source.get("config_path"),
            "sections": {"reticulum": {"enable_transport": config.get("transport_enabled")}},
            "interfaces": config.get("interfaces") or [],
            "sanitized": True,
            "storage_readable": config.get("storage_readable_at_refresh"),
            "stale": stale,
            "refresh_error": error,
            "refreshed_at": config.get("refreshed_at"),
            "checked_at": int(refresh.get("checked_at") or 0) if refresh else 0,
        }

    def capabilities(self) -> dict[str, bool]:
        return {"observe": True, "labels": True, "drop_path": False, "drop_via": False, "drop_announces": False,
                "attach_interface": False, "detach_interface": False, "reload_interface": False,
                "edit_config": False, "restart_service": False, "transport_enable": False}

