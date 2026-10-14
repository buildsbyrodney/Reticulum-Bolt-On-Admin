from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class InstallationCandidate:
    config_path: Path
    storage_path: Path | None
    deployment_type: str
    service_manager: str | None = None
    process_owner: str | None = None
    rns_version: str | None = None
    rnsd_running: bool | None = None
    shared_instance_available: bool | None = None
    evidence: tuple[str, ...] = ()

    def public(self) -> dict[str, Any]:
        value = asdict(self)
        value["config_path"] = str(self.config_path)
        value["storage_path"] = str(self.storage_path) if self.storage_path else None
        return value


@dataclass(frozen=True)
class DetectionResult:
    candidates: tuple[InstallationCandidate, ...] = ()
    selected: InstallationCandidate | None = None
    ambiguous: bool = False
    message: str = ""

    def public(self) -> dict[str, Any]:
        return {
            "candidates": [item.public() for item in self.candidates],
            "selected": self.selected.public() if self.selected else None,
            "ambiguous": self.ambiguous,
            "message": self.message,
        }


@dataclass
class RuntimeSnapshot:
    available: bool = False
    running: bool | None = None
    rns_version: str | None = None
    transport_enabled: bool | None = None
    interfaces: list[dict[str, Any]] = field(default_factory=list)
    paths: list[dict[str, Any]] = field(default_factory=list)
    clients: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    path_count: int | None = None
    paths_truncated: bool = False

