from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


HEX32 = re.compile(r"^[0-9a-fA-F]{32}$")
SAFE_RNS_157_CAPABILITIES = {
    "drop-path": "drop_path", "drop-via": "drop_all_via", "drop-announces": "drop_announce_queues",
    "request-path": "request_path", "blackhole-identity": "blackhole_identity", "unblackhole-identity": "unblackhole_identity",
}
REQUIRED_EXISTING_PATHS = (
    "config", "storage", "storage/cache", "storage/resources", "storage/identities",
    "storage/blackhole", "interfaces", "storage/cache/announces",
)
RUNTIME_INTERFACE_LIMIT = 256
RUNTIME_STRING_LIMIT = 240
RUNTIME_INTERFACE_FIELDS = (
    "name", "short_name", "type", "status", "mode", "rxb", "txb",
    "arxb", "atxb", "arxc", "atxc", "prxc", "ptxc", "clients", "peers",
    "parent_interface_name", "parent_interface_hash", "hash", "interface_hash",
    "ifac_size",
)


def state_tree_ready(config_dir: Path) -> bool:
    return all((config_dir / relative).is_file() if relative == "config" else (config_dir / relative).is_dir()
               for relative in REQUIRED_EXISTING_PATHS)


def probe_capabilities(rns, config_dir: Path) -> list[str]:
    if str(getattr(rns, "__version__", "")) != "1.5.7" or not state_tree_ready(config_dir):
        return []
    # RNS 1.5.7 expands some live interface names (notably TCP clients),
    # while reattach requires the configured section name. Advertising reload
    # could therefore detach successfully and then fail to reattach.
    return sorted(action for action, method in SAFE_RNS_157_CAPABILITIES.items()
                  if hasattr(rns.Reticulum, method) or hasattr(getattr(rns, "Transport", object()), method))


def _result(success: bool, message: str, **metadata) -> int:
    payload = {"success": success, "message": message[:500]}
    payload.update(metadata)
    print(json.dumps(payload, separators=(",", ":")))
    return 0 if success else 1


def _json_safe(value):
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _bounded_value(value):
    """Return only small, public runtime values from shared-instance RPC data."""
    value = _json_safe(value)
    if isinstance(value, str):
        return value[:RUNTIME_STRING_LIMIT]
    if value is None or isinstance(value, (int, float, bool)):
        return value
    return None


def _count_or_scalar(value):
    if isinstance(value, (str, bytes, dict, list, tuple, set)):
        try:
            return len(value)
        except TypeError:
            return None
    return _bounded_value(value)


def read_runtime(config_dir: Path) -> tuple[bool, str, dict]:
    """Read bounded runtime state without ever initialising an independent RNS instance.

    ``get_interface_stats`` is served by the already-running shared instance
    when Reticulum was constructed with ``require_shared_instance=True``.
    Secrets such as IFAC names, passphrases, keys, signatures and identities
    are deliberately omitted from the helper response.
    """
    try:
        import RNS
    except ImportError:
        return False, "The selected Reticulum runtime is unavailable", {}
    if not state_tree_ready(config_dir):
        return False, "Existing Reticulum state tree is incomplete; refusing to create or modify it", {}
    try:
        reticulum = RNS.Reticulum(configdir=str(config_dir), require_shared_instance=True)
        stats = reticulum.get_interface_stats()
    except Exception:
        return False, "Existing Reticulum shared-instance attachment is unavailable", {}
    if not isinstance(stats, dict):
        return False, "Existing Reticulum shared-instance returned unsupported runtime data", {}
    interfaces = stats.get("interfaces")
    if not isinstance(interfaces, list):
        return False, "Existing Reticulum shared-instance returned unsupported runtime data", {}
    rows = []
    for raw in interfaces[:RUNTIME_INTERFACE_LIMIT]:
        if not isinstance(raw, dict):
            continue
        row = {
            key: (_count_or_scalar(raw[key]) if key in {"clients", "peers"} else _bounded_value(raw[key]))
            for key in RUNTIME_INTERFACE_FIELDS if key in raw
        }
        # Presence is useful; the underlying IFAC material is not returned.
        row["ifac_present"] = any(raw.get(key) is not None for key in
                                  ("ifac_signature", "ifac_netname", "ifac_size"))
        rows.append(row)
    transport_enabled = "transport_id" in stats
    return True, "Read runtime state from the existing shared instance", {
        "rns_version": str(getattr(RNS, "__version__", ""))[:64] or None,
        "shared_instance_available": True,
        "transport_enabled": transport_enabled,
        "interfaces": rows,
    }


def read_paths(config_dir: Path) -> tuple[bool, str, list[dict]]:
    try:
        import RNS
    except ImportError:
        return False, "The selected Reticulum runtime is unavailable", []
    if not state_tree_ready(config_dir):
        return False, "Existing Reticulum state tree is incomplete; refusing to create or modify it", []
    try:
        reticulum = RNS.Reticulum(configdir=str(config_dir), require_shared_instance=True)
        table = reticulum.get_path_table()
        rows = [_json_safe(row) for row in table if isinstance(row, dict)]
        return True, "Read paths from the existing shared instance", rows
    except Exception:
        return False, "Existing Reticulum shared-instance path query is unavailable", []


def execute(config_dir: Path, action: str, target: str | None) -> tuple[bool, str]:
    try:
        import RNS
    except ImportError:
        return False, "The selected Reticulum runtime is unavailable"

    if action == "probe":
        return False, "Probe is available only through the command entry point"

    if action in {"drop-path", "drop-via", "request-path", "blackhole-identity", "unblackhole-identity"}:
        if not target or not HEX32.fullmatch(target):
            return False, "Target must be exactly 32 hexadecimal characters"
        target_bytes = bytes.fromhex(target)
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
        if action == "request-path":
            RNS.Transport.request_path(target_bytes)
            return True, f"Requested path {target.lower()}"
        if action == "blackhole-identity":
            success = RNS.Transport.blackhole_identity(target_bytes)
            return bool(success), f"Blackholed identity {target.lower()}" if success else "Reticulum did not blackhole the identity"
        if action == "unblackhole-identity":
            success = RNS.Transport.unblackhole_identity(target_bytes)
            return bool(success), f"Removed blackhole for identity {target.lower()}" if success else "Identity was not blackholed"
    except Exception:
        return False, "Reticulum rejected or failed the requested management operation"


def main() -> None:
    parser = argparse.ArgumentParser(description="Narrow shared-instance-only Reticulum management helper")
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("action", choices=("probe", "read-runtime", "read-paths", "drop-path", "drop-via", "drop-announces", "request-path", "blackhole-identity", "unblackhole-identity"))
    parser.add_argument("target", nargs="?")
    args = parser.parse_args()
    if args.action == "probe":
        try:
            import RNS
            version = str(getattr(RNS, "__version__", ""))
            supported = probe_capabilities(RNS, args.config)
            raise SystemExit(_result(bool(supported), "Supported RNS management capability probe",
                                     rns_version=version or None, capabilities=supported))
        except ImportError:
            raise SystemExit(_result(False, "The selected Reticulum runtime is unavailable", capabilities=[]))
    if args.action == "read-paths":
        success, message, paths = read_paths(args.config)
        raise SystemExit(_result(success, message, paths=paths))
    if args.action == "read-runtime":
        success, message, runtime = read_runtime(args.config)
        raise SystemExit(_result(success, message, runtime=runtime))
    success, message = execute(args.config, args.action, args.target)
    raise SystemExit(_result(success, message))


if __name__ == "__main__":
    main()
