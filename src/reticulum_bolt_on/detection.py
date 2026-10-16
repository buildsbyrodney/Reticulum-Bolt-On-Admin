"""Conservative discovery of existing Reticulum installations.

Discovery reads a bounded set of documented/common locations. It never creates
configuration or storage and never launches rnsd.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path
from typing import Callable, Iterable

from .models import DetectionResult, InstallationCandidate

Runner = Callable[[list[str]], subprocess.CompletedProcess[str]]


def default_runner(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, capture_output=True, text=True, timeout=4, check=False)


def common_config_paths(env: dict[str, str] | None = None, homes: Iterable[Path] | None = None) -> list[Path]:
    env = env or os.environ
    candidates: list[Path] = []
    if env.get("RNS_CONFIG_DIR"):
        candidates.append(Path(env["RNS_CONFIG_DIR"]) / "config")
    if env.get("RETICULUM_CONFIG"):
        supplied = Path(env["RETICULUM_CONFIG"])
        candidates.append(supplied / "config" if supplied.is_dir() else supplied)
    home = Path(env.get("HOME") or env.get("USERPROFILE") or Path.home())
    # Reticulum's normal per-user location remains ~/.reticulum on Windows.
    # AppData is also checked because it is a common explicit configdir choice.
    if os.name == "nt":
        for variable in ("APPDATA", "LOCALAPPDATA"):
            if env.get(variable):
                candidates.append(Path(env[variable]) / "Reticulum" / "config")
    discovered_homes = list(homes) if homes is not None else [home, Path("/root")]
    home_parent = Path("/home")
    if homes is None and home_parent.is_dir():
        try: discovered_homes.extend(item for item in home_parent.iterdir() if item.is_dir())
        except OSError: pass
    for item in discovered_homes:
        candidates.extend((item / ".reticulum" / "config", item / ".config" / "reticulum" / "config"))
    candidates.extend((Path("/etc/reticulum/config"), Path("/var/lib/reticulum/.reticulum/config")))
    output: list[Path] = []
    for item in candidates:
        resolved = item.expanduser()
        if resolved not in output: output.append(resolved)
    return output


def _read_storage(config_path: Path) -> Path | None:
    # Reticulum keeps storage next to the selected config by default. A literal
    # storagepath setting is accepted, but never expanded by executing config.
    try: text = config_path.read_text(encoding="utf-8", errors="replace")
    except OSError: return None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.lower().startswith("storagepath") and "=" in stripped:
            raw = stripped.split("=", 1)[1].strip()
            if raw:
                path = Path(raw).expanduser()
                return path if path.is_absolute() else config_path.parent / path
    return config_path.parent / "storage"


def _probe(command: list[str], runner: Runner) -> subprocess.CompletedProcess[str] | None:
    try: return runner(command)
    except (OSError, subprocess.SubprocessError): return None


def detect(*, env: dict[str, str] | None = None, paths: Iterable[Path] | None = None, runner: Runner = default_runner,
           selected_config: Path | None = None) -> DetectionResult:
    env = env or os.environ
    existing = []
    for item in paths or common_config_paths(env):
        try:
            if item.is_file() and os.access(item, os.R_OK): existing.append(item.resolve())
        except OSError: continue
    existing = list(dict.fromkeys(existing))

    version = _probe(["rnstatus", "--version"], runner)
    systemd = _probe(["systemctl", "show", "rnsd", "--property=ActiveState,MainPID,User", "--no-pager"], runner)
    processes = _probe(["ps", "-eo", "user=,args="], runner)
    docker = _probe(["docker", "ps", "--format", "{{.Names}} {{.Image}} {{.Command}}"], runner)
    rns_version = (version.stdout or version.stderr).strip() if version and version.returncode == 0 else None
    systemd_running = bool(systemd and "ActiveState=active" in systemd.stdout)
    process_lines = [line for line in (processes.stdout.splitlines() if processes else []) if "rnsd" in line and "grep" not in line]
    process_running = bool(process_lines)
    docker_lines = [line for line in (docker.stdout.splitlines() if docker and docker.returncode == 0 else []) if "reticulum" in line.lower() or "rnsd" in line.lower()]

    candidates = []
    for config in existing:
        status = _probe(["rnstatus", "--config", str(config.parent), "--json"], runner)
        shared = bool(status and status.returncode == 0 and (status.stdout or "").strip())
        deployment = "docker" if docker_lines else "systemd" if systemd_running else "host-native"
        if process_running and not systemd_running and not docker_lines: deployment = "manual/shared-instance"
        owner = process_lines[0].split(maxsplit=1)[0] if process_lines else None
        evidence = ["readable config"]
        if shared: evidence.append("rnstatus attached to shared instance")
        if systemd_running: evidence.append("active rnsd systemd unit")
        if process_running: evidence.append("rnsd process observed")
        if docker_lines: evidence.append("Reticulum-like Docker container observed")
        candidates.append(InstallationCandidate(config, _read_storage(config), deployment,
            "systemd" if systemd_running else "docker" if docker_lines else "external/manual",
            owner, rns_version, systemd_running or process_running or bool(docker_lines), shared, tuple(evidence)))

    selected = None
    if selected_config:
        requested = selected_config.resolve()
        selected = next((item for item in candidates if item.config_path == requested), None)
        if selected is None:
            return DetectionResult(tuple(candidates), None, len(candidates) > 1, "Selected configuration is not one of the detected readable candidates.")
    elif len(candidates) == 1:
        selected = candidates[0]
    ambiguous = len(candidates) > 1 and selected is None
    if ambiguous: message = "Multiple Reticulum installations were detected. Explicit administrator selection is required."
    elif selected: message = "Existing Reticulum installation selected for read-only observation."
    else: message = "No readable existing Reticulum configuration was detected. Nothing was created."
    return DetectionResult(tuple(candidates), selected, ambiguous, message)


def main() -> None:
    parser = argparse.ArgumentParser(description="Detect existing Reticulum without modifying it")
    parser.add_argument("--select", type=Path)
    args = parser.parse_args()
    print(json.dumps(detect(selected_config=args.select).public(), indent=2))

