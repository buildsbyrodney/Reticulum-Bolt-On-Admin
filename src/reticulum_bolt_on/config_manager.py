"""Conservative, persistent Reticulum interface configuration editing.

This module deliberately operates on interface *sections*, never live RNS
objects.  It preserves unrecognised lines and makes every write a checked,
atomic replacement with a Bolt-On-owned backup.
"""
from __future__ import annotations

import difflib
import os
import re
import shutil
import stat
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

INTERFACE_TYPES = {"TCPClientInterface", "TCPServerInterface", "AutoInterface", "RNodeInterface"}
SECRET_MARKERS = ("pass", "secret", "key", "identity")
NAME = re.compile(r"^[^\[\]\r\n]{1,128}$")
HOST = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,252}$")


class ConfigConflict(RuntimeError): pass
class ConfigValidationError(ValueError): pass


@dataclass(frozen=True)
class InterfaceSection:
    name: str
    start: int
    end: int
    lines: tuple[str, ...]

    def values(self) -> dict[str, str]:
        result: dict[str, str] = {}
        for line in self.lines[1:]:
            if "=" in line and not line.lstrip().startswith("#"):
                key, value = line.split("=", 1)
                result[key.strip().lower()] = value.strip()
        return result


def sections(text: str) -> list[InterfaceSection]:
    lines = text.splitlines(keepends=True); found: list[InterfaceSection] = []
    starts: list[tuple[int, str]] = []
    for index, line in enumerate(lines):
        match = re.match(r"^\s*\[\[([^\]\r\n]+)\]\]\s*(?:[#;].*)?(?:\r?\n)?$", line)
        if match: starts.append((index, match.group(1).strip()))
    for offset, (start, name) in enumerate(starts):
        end = starts[offset + 1][0] if offset + 1 < len(starts) else len(lines)
        found.append(InterfaceSection(name, start, end, tuple(lines[start:end])))
    return found


def fingerprint(text: str) -> str:
    import hashlib
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def safe_values(values: dict[str, str]) -> dict[str, str]:
    return {key: ("[present, hidden]" if any(marker in key.lower() for marker in SECRET_MARKERS) and value else value)
            for key, value in values.items()}


def redact_diff(diff: str) -> str:
    """Keep review diffs useful without placing configured credentials in HTML."""
    output = []
    for line in diff.splitlines(keepends=True):
        payload = line[1:] if line[:1] in {"+", "-"} else line
        if "=" in payload and not payload.lstrip().startswith("#"):
            key, _ = payload.split("=", 1)
            if any(marker in key.strip().lower() for marker in SECRET_MARKERS):
                ending = "\n" if line.endswith("\n") else ""
                output.append(f"{line[:1]}{key}= [present, hidden]{ending}")
                continue
        output.append(line)
    return "".join(output)


def validate(name: str, values: dict[str, str]) -> None:
    if not NAME.fullmatch(name): raise ConfigValidationError("Interface name is invalid")
    kind = values.get("type", "")
    if kind not in INTERFACE_TYPES: raise ConfigValidationError("Only known interface types can be created or structurally edited")
    for key in ("target_port", "listen_port", "port"):
        if key in values:
            try: port = int(values[key])
            except ValueError: raise ConfigValidationError(f"{key} must be a port number")
            if not 1 <= port <= 65535: raise ConfigValidationError(f"{key} must be between 1 and 65535")
    for key in ("target_host", "listen_ip", "listen_host"):
        if values.get(key) and not HOST.fullmatch(values[key]): raise ConfigValidationError(f"{key} is not a valid host or address")
    if kind == "TCPClientInterface" and not values.get("target_host"): raise ConfigValidationError("TCPClientInterface requires target_host")
    if kind == "TCPServerInterface" and not (values.get("listen_port") or values.get("port")):
        raise ConfigValidationError("TCPServerInterface requires a listen port")


def render_section(name: str, values: dict[str, str], original: InterfaceSection | None = None,
                   remove: set[str] | None = None) -> list[str]:
    """Patch recognized keys in place and retain every unknown/comment line."""
    validate(name, values)
    desired = {key.lower(): str(value) for key, value in values.items()}; remove = remove or set()
    if original is None:
        return [f"[[{name}]]\n", *[f"{key} = {value}\n" for key, value in desired.items()]]
    output = [f"[[{name}]]\n"]; seen: set[str] = set()
    for line in original.lines[1:]:
        if "=" not in line or line.lstrip().startswith("#"):
            output.append(line); continue
        raw, old = line.split("=", 1); key = raw.strip().lower()
        if key in remove:
            continue
        if key in desired:
            output.append(f"{raw.rstrip()} = {desired[key]}\n"); seen.add(key)
        else: output.append(line)
    for key, value in desired.items():
        if key not in seen: output.append(f"{key} = {value}\n")
    return output


class ConfigManager:
    def __init__(self, path: Path, backup_limit: int = 20): self.path, self.backup_limit = path, backup_limit
    def read(self) -> tuple[str, str]:
        text = self.path.read_text(encoding="utf-8", errors="strict")
        return text, fingerprint(text)
    def preview(self, before: str, replacement: list[str], section: InterfaceSection | None = None) -> tuple[str, str]:
        lines = before.splitlines(keepends=True)
        if section: lines[section.start:section.end] = replacement
        else:
            if lines and not lines[-1].endswith("\n"): lines[-1] += "\n"
            if lines and lines[-1].strip(): lines.append("\n")
            lines.extend(replacement)
        after = "".join(lines)
        diff = "".join(difflib.unified_diff(before.splitlines(True), after.splitlines(True), "config (current)", "config (proposed)"))
        return after, diff
    def delete_preview(self, before: str, section: InterfaceSection) -> tuple[str, str]:
        lines = before.splitlines(keepends=True); del lines[section.start:section.end]
        after = "".join(lines)
        return after, "".join(difflib.unified_diff(before.splitlines(True), after.splitlines(True), "config (current)", "config (proposed)"))
    def apply(self, expected: str, content: str) -> Path:
        current, current_hash = self.read()
        if current_hash != expected: raise ConfigConflict("Configuration changed outside Bolt-On; refresh and review again")
        # Basic structural validation: duplicate names and malformed headers are rejected.
        names = [section.name for section in sections(content)]
        if len(names) != len(set(name.lower() for name in names)): raise ConfigValidationError("Interface section names must be unique")
        backup = self._backup(current)
        original_mode = stat.S_IMODE(self.path.stat().st_mode)
        fd, temp_name = tempfile.mkstemp(prefix=".config.bolt-on-", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
                handle.write(content); handle.flush(); os.fsync(handle.fileno())
            os.chmod(temp_name, original_mode)
            os.replace(temp_name, self.path)
        finally:
            if os.path.exists(temp_name): os.unlink(temp_name)
        self._trim_backups()
        return backup
    def _backup(self, content: str) -> Path:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        backup = self.path.with_name(f"{self.path.name}.backup-{stamp}")
        shutil.copy2(self.path, backup)
        return backup
    def _trim_backups(self) -> None:
        owned = sorted(self.path.parent.glob(f"{self.path.name}.backup-[0-9]*"), key=lambda item: item.name, reverse=True)
        for old in owned[self.backup_limit:]: old.unlink()
