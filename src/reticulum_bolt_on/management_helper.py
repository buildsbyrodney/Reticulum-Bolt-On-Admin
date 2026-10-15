from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


HEX32 = re.compile(r"^[0-9a-fA-F]{32}$")
INTERFACE_OPERATIONS = {"attach-interface", "detach-interface", "reload-interface"}
REQUIRED_EXISTING_PATHS = (
    "config", "storage", "storage/cache", "storage/resources", "storage/identities",
    "storage/blackhole", "interfaces", "storage/cache/announces",
)


def state_tree_ready(config_dir: Path) -> bool:
    return all((config_dir / relative).is_file() if relative == "config" else (config_dir / relative).is_dir()
               for relative in REQUIRED_EXISTING_PATHS)


def _result(success: bool, message: str, **metadata) -> int:
    payload = {"success": success, "message": message[:500]}
    payload.update(metadata)
    print(json.dumps(payload, separators=(",", ":")))
    return 0 if success else 1


def execute(config_dir: Path, action: str, target: str | None) -> tuple[bool, str]:
    try:
        import RNS
    except ImportError:
        return False, "The selected Reticulum runtime is unavailable"

    if action == "probe":
        return False, "Probe is available only through the command entry point"

    if action in {"drop-path", "drop-via"}:
        if not target or not HEX32.fullmatch(target):
            return False, "Target must be exactly 32 hexadecimal characters"
        target_bytes = bytes.fromhex(target)
    elif action in INTERFACE_OPERATIONS:
        if not target or len(target) > 128 or any(ord(char) < 32 for char in target):
            return False, "Interface name is invalid"
    elif action != "drop-announces":
        return False, "Unsupported management action"

    if not state_tree_ready(config_dir):
        return False, "Existing Reticulum state tree is incomplete; refusing to create or modify it"

    try:
        # This is the central safety boundary: failure to attach is fatal and
        # can never construct an independent Reticulum instance.
        reticulum = RNS.Reticulum(configdir=str(config_dir), require_shared_instance=True)
    except Exception:
        return False, "Existing Reticulum shared-instance attachment is unavailable"

    try:
        if action == "drop-path":
            success = reticulum.drop_path(target_bytes)
            return bool(success), f"Dropped path {target.lower()}" if success else "Reticulum did not drop the requested path"
        if action == "drop-via":
            success = reticulum.drop_all_via(target_bytes)
            return bool(success), f"Dropped paths via {target.lower()}" if success else "Reticulum did not drop paths via the requested transport"
        if action == "drop-announces":
            reticulum.drop_announce_queues()
            return True, "Dropped queued announces on all interfaces"
        method = getattr(reticulum, action.replace("-interface", "_interface"))
        success = method(target)
        return bool(success), f"Interface {target} {action.removesuffix('-interface')} completed" if success else "Reticulum did not complete the interface operation"
    except Exception:
        return False, "Reticulum rejected or failed the requested management operation"


def main() -> None:
    parser = argparse.ArgumentParser(description="Narrow shared-instance-only Reticulum management helper")
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("action", choices=("probe", "drop-path", "drop-via", "drop-announces", *sorted(INTERFACE_OPERATIONS)))
    parser.add_argument("target", nargs="?")
    args = parser.parse_args()
    if args.action == "probe":
        try:
            import RNS
            version = str(getattr(RNS, "__version__", ""))
            methods = {
                "drop-path": "drop_path", "drop-via": "drop_all_via", "drop-announces": "drop_announce_queues",
                "attach-interface": "attach_interface", "detach-interface": "detach_interface", "reload-interface": "reload_interface",
            }
            supported = sorted(action for action, method in methods.items()
                               if version == "1.5.7" and state_tree_ready(args.config) and hasattr(RNS.Reticulum, method))
            raise SystemExit(_result(bool(supported), "Supported RNS management capability probe",
                                     rns_version=version or None, capabilities=supported))
        except ImportError:
            raise SystemExit(_result(False, "The selected Reticulum runtime is unavailable", capabilities=[]))
    success, message = execute(args.config, args.action, args.target)
    raise SystemExit(_result(success, message))


if __name__ == "__main__":
    main()
