"""Shared, non-interactive subprocess boundary for Bolt-On helpers."""
from __future__ import annotations

import subprocess
import sys


def run_command(command: list[str], *, timeout: int) -> subprocess.CompletedProcess[str]:
    """Run a captured helper command without flashing a Windows console.

    Reticulum utilities are console executables on Windows.  Bolt-On invokes
    them from a windowless GUI process, so explicitly prevent Windows from
    allocating a transient console.  Other platforms intentionally retain the
    exact subprocess behaviour used before this helper existed.
    """
    kwargs: dict[str, object] = {
        "capture_output": True,
        "text": True,
        "timeout": timeout,
        "check": False,
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.run(command, **kwargs)
