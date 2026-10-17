"""Narrow, proof-based lifecycle adapters for Reticulum.

This module deliberately has no generic process-name restart path.  Windows
support is limited to one positively identified ``rnsd.exe`` launcher and its
direct launch tree, owned by the current user and associated with the selected
configuration.  Anything else is reported as unavailable.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable


@dataclass(frozen=True)
class ProcessRecord:
    pid: int
    parent_pid: int
    executable: str
    command_line: str
    owner: str


@dataclass(frozen=True)
class ProcessEnumeration:
    """The process snapshot, or a deliberately non-sensitive query failure."""
    records: tuple[ProcessRecord, ...] = ()
    error: str | None = None


@dataclass(frozen=True)
class LifecycleStatus:
    available: bool
    deployment: str
    message: str
    executable: str | None = None
    arguments: tuple[str, ...] = ()
    pids: tuple[int, ...] = ()
    owner: str | None = None
    config_path: str | None = None

    def public(self) -> dict[str, object]:
        return {"available": self.available, "deployment": self.deployment,
                "message": self.message, "executable": self.executable,
                "pids": list(self.pids), "owner": self.owner,
                "config_path": self.config_path}


@dataclass(frozen=True)
class RestartResult:
    success: bool
    message: str
    old_stopped: bool
    replacement_started: bool
    shared_returned: bool
    backup_name: str | None = None
    executable: str | None = None
    metadata: dict[str, object] = field(default_factory=dict)


def _current_owner() -> str | None:
    return os.environ.get("USERNAME") if sys.platform == "win32" else None


def _windows_processes() -> ProcessEnumeration:
    """Read process evidence through CIM without invoking a shell command string."""
    script = r"""
$ErrorActionPreference = 'Stop'
$rows = foreach ($p in Get-CimInstance Win32_Process -ErrorAction Stop) {
  $owner = Invoke-CimMethod -InputObject $p -MethodName GetOwner -ErrorAction SilentlyContinue
  if ($owner -and $owner.ReturnValue -eq 0) { $who = $owner.User } else { $who = '' }
  $executable = ''
  if ($null -ne $p.ExecutablePath) { $executable = [string]$p.ExecutablePath }
  $commandLine = ''
  if ($null -ne $p.CommandLine) { $commandLine = [string]$p.CommandLine }
  [pscustomobject]@{pid=[int]$p.ProcessId;parent_pid=[int]$p.ParentProcessId;executable=$executable;command_line=$commandLine;owner=$who}
}
@($rows) | ConvertTo-Json -Compress
"""
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        result = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                                capture_output=True, text=True, timeout=8, check=False,
                                creationflags=flags)
    except (OSError, subprocess.TimeoutExpired):
        return ProcessEnumeration(error="Windows process enumeration could not be completed.")
    if result.returncode:
        return ProcessEnumeration(error="Windows process enumeration could not be completed.")
    try:
        rows = json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        return ProcessEnumeration(error="Windows process enumeration returned invalid data.")
    if isinstance(rows, dict):
        rows = [rows]
    if not isinstance(rows, list):
        return ProcessEnumeration(error="Windows process enumeration returned invalid data.")
    return ProcessEnumeration(tuple(
        ProcessRecord(int(row.get("pid") or 0), int(row.get("parent_pid") or 0),
                      str(row.get("executable") or ""), str(row.get("command_line") or ""),
                      str(row.get("owner") or ""))
        for row in rows if isinstance(row, dict) and int(row.get("pid") or 0) > 0
    ))


def _process_snapshot(provider: Callable[[], list[ProcessRecord] | ProcessEnumeration]) -> ProcessEnumeration:
    """Keep injected list providers compatible while making real-query failures explicit."""
    try:
        result = provider()
    except (OSError, subprocess.TimeoutExpired):
        return ProcessEnumeration(error="Windows process enumeration could not be completed.")
    if isinstance(result, ProcessEnumeration):
        return result
    return ProcessEnumeration(tuple(result))


def _arguments(record: ProcessRecord, config_path: Path, owner: str) -> tuple[str, ...] | None:
    """Accept only the supported rnsd launch forms, with an exact config association."""
    try:
        tokens = shlex.split(record.command_line, posix=False)
    except ValueError:
        return None
    if not tokens:
        return None
    executable = Path(record.executable or tokens[0])
    if executable.name.lower() != "rnsd.exe" or not executable.is_file():
        return None
    args = tokens[1:]
    default_config = (Path(os.environ.get("USERPROFILE", "")) / ".reticulum" / "config")
    if not args:
        return () if config_path.resolve() == default_config.resolve() and owner.lower() == (_current_owner() or "").lower() else None
    if len(args) == 2 and args[0].lower() == "--config":
        try:
            return tuple(args) if Path(args[1]).resolve() == config_path.parent.resolve() else None
        except OSError:
            return None
    return None


class WindowsNativeLifecycle:
    """Restart only a proven current-user ``rnsd.exe`` launch chain."""
    def __init__(self, config_path: Path, version: str | None, shared_available: bool,
                 process_provider: Callable[[], list[ProcessRecord] | ProcessEnumeration] = _windows_processes,
                 sleeper: Callable[[float], None] = time.sleep):
        self.config_path = config_path
        self.version = version
        self.shared_available = shared_available
        self.process_provider = process_provider
        self.sleeper = sleeper

    def inspect(self) -> LifecycleStatus:
        if sys.platform != "win32":
            return LifecycleStatus(False, "Native Windows", "Restart unavailable — Reticulum lifecycle could not be identified safely.")
        supported_version = bool(self.version and re.search(r"(?<![\d.])1\.5\.7(?![\d.])", self.version))
        if not supported_version or not self.shared_available or not self.config_path.is_file():
            return LifecycleStatus(False, "Native Windows", "Restart unavailable — Reticulum lifecycle could not be identified safely.")
        owner = _current_owner()
        if not owner:
            return LifecycleStatus(False, "Native Windows", "Restart unavailable — Reticulum lifecycle could not be identified safely.")
        snapshot = _process_snapshot(self.process_provider)
        if snapshot.error:
            return LifecycleStatus(False, "Native Windows", "Restart unavailable — Windows process enumeration failed.")
        roots: list[tuple[ProcessRecord, tuple[str, ...]]] = []
        for record in snapshot.records:
            if record.owner.lower() != owner.lower():
                continue
            args = _arguments(record, self.config_path, owner)
            if args is not None:
                roots.append((record, args))
        if not roots:
            return LifecycleStatus(False, "Native Windows", "Restart unavailable — no matching current-user rnsd launch chain was found.")
        if len(roots) != 1:
            return LifecycleStatus(False, "Native Windows", "Restart unavailable — multiple matching rnsd launch chains were found.")
        root, args = roots[0]
        chain_snapshot = _process_snapshot(self.process_provider)
        if chain_snapshot.error:
            return LifecycleStatus(False, "Native Windows", "Restart unavailable — Windows process enumeration failed.")
        records = chain_snapshot.records
        chain = {root.pid}
        changed = True
        while changed:
            changed = False
            for item in records:
                if item.parent_pid in chain and item.owner.lower() == owner.lower() and item.pid not in chain:
                    chain.add(item.pid); changed = True
        if os.getpid() in chain:
            return LifecycleStatus(False, "Native Windows", "Restart unavailable — Reticulum lifecycle could not be identified safely.")
        return LifecycleStatus(True, "Native Windows", "Verified current-user rnsd launch chain.",
                               str(Path(root.executable)), args, tuple(sorted(chain)), owner, str(self.config_path))

    def _terminate(self, pids: tuple[int, ...]) -> bool:
        # Only IDs from the inspected chain are sent to Stop-Process; no name
        # based selection and no arbitrary Python process is ever targeted.
        ids = ",".join(str(pid) for pid in pids)
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        result = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                                 f"Stop-Process -Id {ids} -Force -ErrorAction Stop"], capture_output=True,
                                text=True, timeout=10, check=False, creationflags=flags)
        return result.returncode == 0

    def _launch(self, status: LifecycleStatus) -> bool:
        if not status.executable:
            return False
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            subprocess.Popen([status.executable, *status.arguments], stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             creationflags=flags, close_fds=True)
            return True
        except OSError:
            return False

    def restart(self, status: LifecycleStatus, shared_probe: Callable[[], bool], timeout: float = 30) -> RestartResult:
        if not status.available:
            return RestartResult(False, status.message, False, False, False, executable=status.executable)
        old_stopped = self._terminate(status.pids)
        if not old_stopped:
            return RestartResult(False, "Reticulum stop failed; no replacement was launched.", False, False, False, executable=status.executable)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            snapshot = _process_snapshot(self.process_provider)
            if snapshot.error:
                return RestartResult(False, "Reticulum stopped, but process verification failed; no replacement was launched.",
                                     True, False, False, executable=status.executable)
            if not any(item.pid in status.pids for item in snapshot.records):
                break
            self.sleeper(0.25)
        else:
            return RestartResult(False, "Reticulum did not stop within the safety timeout; no replacement was launched.", False, False, False, executable=status.executable)
        started = self._launch(status)
        if not started:
            return RestartResult(False, "Reticulum stopped but the replacement process could not be launched.", True, False, False, executable=status.executable)
        while time.monotonic() < deadline:
            if shared_probe():
                final = self.inspect()
                if final.available and len(final.pids) >= 1:
                    return RestartResult(True, "Reticulum restarted and the shared instance returned.", True, True, True,
                                         executable=status.executable, metadata={"replacement_pids": list(final.pids)})
            self.sleeper(0.5)
        return RestartResult(False, "Replacement process started but the shared instance did not return before timeout.", True, True, False, executable=status.executable)
