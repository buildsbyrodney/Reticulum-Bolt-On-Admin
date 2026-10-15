"""Root, read-only observation boundary for protected Reticulum installs."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

from .parsers import find_interfaces, normalize_paths, parse_config_interfaces, proven_clients

Runner = Callable[[list[str]], subprocess.CompletedProcess[str]]
MAX_PATH_ROWS = 10_000
SAFE_INTERFACE_FIELDS = (
    "name", "short_name", "type", "enabled", "mode", "runtime_status",
    "rx_bytes", "tx_bytes", "client_count", "announces", "path_requests",
    "parent_interface_name", "parent_interface_hash", "interface_hash",
    "ifac_present", "source",
)
SAFE_CLIENT_FIELDS = (
    "key", "remote_ip", "remote_port", "parent_interface", "rx_bytes",
    "tx_bytes", "announces", "path_requests", "ifac_present",
)


class ObservationUnavailable(RuntimeError):
    pass


def _boolean(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() in {"yes", "true", "1", "on"}


def _atomic_json(path: Path, value: Any, mode: int = 0o644) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(temporary, mode)
    os.replace(temporary, path)


def _command_runner(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, capture_output=True, text=True, timeout=20, check=False)


def _run_json(runner: Runner, command: list[str], label: str) -> Any:
    try:
        result = runner(command)
    except (OSError, subprocess.SubprocessError) as error:
        raise ObservationUnavailable(f"{label} unavailable") from error
    if result.returncode != 0:
        # Utility output can include local paths and library exception details.
        # Do not cross the privileged boundary with it.
        raise ObservationUnavailable(f"{label} failed with exit code {result.returncode}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as initial_error:
        # RNS 1.5.7 can emit harmless startup notices before its requested JSON.
        # Accept one complete trailing JSON value, but never publish the notices.
        decoder = json.JSONDecoder()
        for index, character in enumerate(result.stdout):
            if character not in "[{":
                continue
            try:
                value, end = decoder.raw_decode(result.stdout[index:])
            except json.JSONDecodeError:
                continue
            if not result.stdout[index + end:].strip():
                return value
        raise ObservationUnavailable(f"{label} returned invalid JSON") from initial_error


def _rns_version(runner: Runner, status: Any) -> str | None:
    if isinstance(status, dict):
        reported = status.get("rns_version") or status.get("version")
        if reported:
            return str(reported)
    try:
        result = runner(["rnstatus", "--version"])
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return value.removeprefix("rnstatus ").strip() or None


def _safe_interfaces(status: Any) -> list[dict[str, Any]]:
    return [{key: interface.get(key) for key in SAFE_INTERFACE_FIELDS} for interface in find_interfaces(status)]


def _safe_clients(interfaces: list[dict[str, Any]], configured: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output = []
    for client in proven_clients(interfaces, configured):
        row = {key: client.get(key) for key in SAFE_CLIENT_FIELDS}
        row["identity"] = None
        output.append(row)
    return output


def _config_snapshot(source: Path, *, source_display_path: str | None = None,
                     storage_display_path: str | None = None, deployment_type: str = "docker") -> tuple[dict[str, Any], bytes]:
    source = source.resolve()
    raw = source.read_bytes()
    sections, configured = parse_config_interfaces(raw.decode("utf-8", errors="replace"))
    source_stat = source.stat()
    now = int(time.time())
    snapshot = {
        "schema_version": 2,
        "refreshed_at": now,
        "source": {
            "config_path": source_display_path or str(source),
            "storage_path": storage_display_path or str(source.parent / "storage"),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "mode": stat.S_IMODE(source_stat.st_mode),
            "uid": source_stat.st_uid,
            "gid": source_stat.st_gid,
        },
        "deployment_type": deployment_type,
        "transport_enabled": _boolean(sections.get("reticulum", {}).get("enable_transport")),
        "interfaces": configured,
        "storage_readable_at_refresh": (source.parent / "storage").is_dir(),
    }
    return snapshot, raw


def sanitize_config(source: Path, output_dir: Path, *, source_display_path: str | None = None,
                    storage_display_path: str | None = None, deployment_type: str = "docker") -> dict[str, Any]:
    """Publish a config-only snapshot without copying any Reticulum secrets."""
    snapshot, _ = _config_snapshot(
        source,
        source_display_path=source_display_path,
        storage_display_path=storage_display_path,
        deployment_type=deployment_type,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(output_dir, 0o755)
    _atomic_json(output_dir / "config-view.json", snapshot)
    return snapshot


def collect_observation(source: Path, output_dir: Path, *, source_display_path: str | None = None,
                        storage_display_path: str | None = None, deployment_type: str = "docker",
                        runner: Runner = _command_runner, max_paths: int = MAX_PATH_ROWS) -> dict[str, Any]:
    """Query the existing shared instance and publish only sanitized snapshots.

    The source directory is passed directly to the RNS utilities, so they use
    the existing identity-derived or explicit RPC authentication in place.
    Neither credential is copied into observer output.
    """
    source = source.resolve()
    config_snapshot, raw = _config_snapshot(
        source,
        source_display_path=source_display_path,
        storage_display_path=storage_display_path,
        deployment_type=deployment_type,
    )
    configured = config_snapshot["interfaces"]
    config_dir = str(source.parent)
    status = _run_json(runner, ["rnstatus", "--config", config_dir, "--json"], "rnstatus shared-instance query")
    path_data = _run_json(runner, ["rnpath", "--config", config_dir, "--table", "--json"], "rnpath shared-instance query")
    runtime_interfaces = _safe_interfaces(status)
    paths = normalize_paths(path_data)
    collected_at = int(time.time())
    version = _rns_version(runner, status)
    transport = status.get("transport_enabled") if isinstance(status, dict) else None
    if isinstance(status, dict) and transport is None and "transport_id" in status:
        transport = bool(status.get("transport_id"))
    digest = hashlib.sha256(raw).hexdigest()
    config_snapshot["refreshed_at"] = collected_at
    runtime_snapshot = {
        "schema_version": 1,
        "collected_at": collected_at,
        "available": True,
        "running": True,
        "rns_version": version,
        "transport_enabled": transport,
        "interfaces": runtime_interfaces,
        "clients": _safe_clients(runtime_interfaces, configured),
        "path_count": len(paths),
    }
    paths_snapshot = {
        "schema_version": 1,
        "collected_at": collected_at,
        "total_count": len(paths),
        "truncated": len(paths) > max_paths,
        "rows": paths[:max_paths],
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(output_dir, 0o755)
    _atomic_json(output_dir / "config-view.json", config_snapshot)
    _atomic_json(output_dir / "runtime-status.json", runtime_snapshot)
    _atomic_json(output_dir / "paths.json", paths_snapshot)
    _atomic_json(output_dir / "refresh-status.json", {
        "ok": True,
        "checked_at": collected_at,
        "source_sha256": digest,
        "error": None,
    })
    return {"config": config_snapshot, "runtime": runtime_snapshot, "paths": paths_snapshot}


def _write_failure(output_dir: Path, source: Path, error: Exception) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    digest = None
    try:
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
    except OSError:
        pass
    _atomic_json(output_dir / "refresh-status.json", {
        "ok": False,
        "checked_at": int(time.time()),
        "source_sha256": digest,
        "error": str(error) if isinstance(error, ObservationUnavailable) else "Observation helper failed",
    })


def main() -> None:
    parser = argparse.ArgumentParser(description="Publish credential-free snapshots from an existing Reticulum instance")
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--source-display-path")
    parser.add_argument("--storage-display-path")
    parser.add_argument("--deployment-type", default="docker")
    parser.add_argument("--watch-seconds", type=int, default=0)
    parser.add_argument("--max-paths", type=int, default=MAX_PATH_ROWS)
    parser.add_argument("--config-only", action="store_true")
    args = parser.parse_args()
    if args.config_only:
        sanitize_config(
            args.source, args.output_dir,
            source_display_path=args.source_display_path,
            storage_display_path=args.storage_display_path,
            deployment_type=args.deployment_type,
        )
        _write_failure(args.output_dir, args.source, ObservationUnavailable("Runtime observation helper has not collected yet"))
        return
    while True:
        try:
            collect_observation(
                args.source, args.output_dir,
                source_display_path=args.source_display_path,
                storage_display_path=args.storage_display_path,
                deployment_type=args.deployment_type,
                max_paths=max(1, args.max_paths),
            )
        except Exception as error:
            _write_failure(args.output_dir, args.source, error)
            if not args.watch_seconds:
                raise SystemExit(str(error))
        if not args.watch_seconds:
            return
        time.sleep(max(5, args.watch_seconds))


if __name__ == "__main__":
    main()
