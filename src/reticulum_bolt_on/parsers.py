"""Defensive normalizers for supported Reticulum utility output."""
from __future__ import annotations

import ipaddress
import re
from collections import defaultdict
from typing import Any, Iterable

MODE_NAMES = {1: "Full", 2: "Point-to-Point", 3: "Access Point", 4: "Roaming", 5: "Boundary", 6: "Gateway", 7: "Internal"}
TCP_CHILD = re.compile(r"^TCPInterface\[Client on (?P<label>[^/\[\]]+)/(?P<ip>(?:\d{1,3}\.){3}\d{1,3}):(?P<port>[1-9]\d{0,4})\]$")
VALID_HASH = re.compile(r"^[0-9a-fA-F]{32}$")
PATH_ELEVATED = 100
PATH_HIGH = 1000
CONCENTRATION_MIN = 100
CONCENTRATION_SHARE = 0.50
SAMPLE_LIMIT = 5
RAW_PATH_LIMIT = 500


def _value(item: dict[str, Any], *names: str) -> Any:
    for name in names:
        if name in item and item[name] is not None:
            return item[name]
    return None


def _integer(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _ifac_present(raw: dict[str, Any]) -> bool | None:
    names = ("ifac_signature", "ifac_size", "ifac_netname")
    if not any(name in raw for name in names):
        return None
    return any(raw.get(name) is not None for name in names)


def normalize_interface(raw: dict[str, Any]) -> dict[str, Any]:
    name = str(_value(raw, "name", "interface_name", "display_name") or "Unnamed interface")
    mode = _value(raw, "mode", "interface_mode")
    status = _value(raw, "status", "state", "online")
    numeric_mode = _integer(mode)
    return {
        "name": name,
        "short_name": _value(raw, "short_name", "display_name") or name,
        "type": _value(raw, "type", "interface_type", "class"),
        "enabled": _value(raw, "enabled", "is_enabled"),
        "mode": MODE_NAMES.get(numeric_mode, f"Unknown ({numeric_mode})") if numeric_mode is not None else (str(mode) if mode is not None else None),
        "runtime_status": "Up" if status is True else "Down" if status is False else (str(status) if status is not None else None),
        "rx_bytes": _integer(_value(raw, "rxb", "rx_bytes")),
        "tx_bytes": _integer(_value(raw, "txb", "tx_bytes")),
        "client_count": _integer(raw.get("clients")),
        "announces": _integer(_value(raw, "announces", "announce_count", "arxc", "atxc")),
        "path_requests": _integer(_value(raw, "path_requests", "prxc", "ptxc")),
        "parent_interface_name": raw.get("parent_interface_name"),
        "parent_interface_hash": raw.get("parent_interface_hash"),
        "interface_hash": _value(raw, "hash", "interface_hash"),
        "ifac_netname": _value(raw, "ifac_netname", "network_name"),
        "ifac_present": _ifac_present(raw),
        "source": "runtime",
    }


def find_interfaces(data: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if key.lower() in {"interfaces", "interface_stats"} and isinstance(child, (list, dict)):
                    records = child.values() if isinstance(child, dict) else child
                    found.extend(record for record in records if isinstance(record, dict))
                else:
                    walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(data)
    output: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for raw in found:
        row = normalize_interface(raw)
        key = (row["name"], str(row.get("type") or ""))
        if key not in seen:
            seen.add(key)
            output.append(row)
    return output


def parse_config_interfaces(text: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    sections: dict[str, dict[str, str]] = defaultdict(dict)
    interfaces: list[dict[str, Any]] = []
    section: str | None = None
    current: dict[str, Any] | None = None
    for original in text.splitlines():
        line = original.strip()
        if not line or line.startswith("#"):
            continue
        match = re.match(r"^\[\[(.+?)\]\]$", line)
        if match:
            if current:
                interfaces.append(current)
            section = "interfaces"
            current = {"name": match.group(1), "type": None, "enabled": None, "mode": None, "ifac_present": False, "source": "configured"}
            continue
        match = re.match(r"^\[(.+?)\]$", line)
        if match:
            if current:
                interfaces.append(current)
                current = None
            section = match.group(1).lower()
            continue
        if "=" not in line:
            continue
        key, value = (part.strip() for part in line.split("=", 1))
        if current is not None:
            lowered = key.lower()
            if lowered == "type": current["type"] = value
            elif lowered in {"enabled", "interface_enabled"}: current["enabled"] = value.lower() in {"yes", "true", "1", "on"}
            elif lowered == "mode": current["mode"] = value
            elif "ifac" in lowered: current["ifac_present"] = True
        elif section:
            sections[section][key.lower()] = value
    if current:
        interfaces.append(current)
    return dict(sections), interfaces


def merge_interfaces(configured: Iterable[dict[str, Any]], runtime: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = [dict(item, configured=True, runtime=False) for item in configured]
    by_name = {row["name"]: row for row in rows}
    for item in runtime:
        row = dict(item)
        short = str(row.get("short_name") or "")
        exact = by_name.get(short) or by_name.get(str(row.get("name") or ""))
        if exact:
            exact.update({key: value for key, value in row.items() if value is not None})
            exact["configured"] = True
            exact["runtime"] = True
        else:
            row.update(configured=False, runtime=True)
            rows.append(row)
    return rows


def proven_clients(interfaces: Iterable[dict[str, Any]], configured_servers: Iterable[dict[str, Any]] = ()) -> list[dict[str, Any]]:
    records = list(interfaces)
    by_name = {str(row.get("name")): row for row in records}
    configured_names = {str(row.get("name")) for row in configured_servers if row.get("type") == "TCPServerInterface"}
    output = []
    for child in records:
        if child.get("type") != "TCPClientInterface" or not child.get("parent_interface_name"):
            continue
        match = TCP_CHILD.match(str(child.get("name") or ""))
        if not match:
            continue
        try:
            ip = str(ipaddress.ip_address(match.group("ip")))
            port = int(match.group("port"))
        except ValueError:
            continue
        parent = by_name.get(str(child.get("parent_interface_name")))
        parent_label = match.group("label")
        if parent and parent.get("type") != "TCPServerInterface":
            continue
        if not parent and parent_label not in configured_names:
            continue
        output.append({
            "key": f"{parent_label}|{ip}|{port}", "remote_ip": ip, "remote_port": port,
            "parent_interface": parent_label, "rx_bytes": child.get("rx_bytes"), "tx_bytes": child.get("tx_bytes"),
            "announces": child.get("announces"), "path_requests": child.get("path_requests"),
            "ifac_netname": child.get("ifac_netname"), "identity": None,
        })
    return output


def normalize_paths(data: Any) -> list[dict[str, Any]]:
    records = data.get("paths", []) if isinstance(data, dict) else data if isinstance(data, list) else []
    output = []
    for raw in records:
        if not isinstance(raw, dict):
            continue
        output.append({"destination": str(raw.get("hash") or raw.get("destination") or ""), "hops": _integer(raw.get("hops")),
                       "via": str(raw.get("via") or ""), "interface": raw.get("interface"),
                       "expires": raw.get("expires") or raw.get("expiry")})
    return output


def path_health(paths: Iterable[dict[str, Any]], labels: dict[tuple[str, str], str] | None = None) -> dict[str, Any]:
    labels = labels or {}
    groups: dict[str, dict[str, Any]] = {}
    total = 0
    for path in paths:
        total += 1
        via = str(path.get("via") or "")
        group = groups.setdefault(via, {"via": via, "count": 0, "hops": [], "interfaces": set(), "samples": []})
        group["count"] += 1
        if path.get("hops") is not None: group["hops"].append(path["hops"])
        if path.get("interface"): group["interfaces"].add(str(path["interface"]))
        if path.get("destination") and len(group["samples"]) < SAMPLE_LIMIT: group["samples"].append(path["destination"])
    rows = []
    for via, group in groups.items():
        rows.append({"via": via, "label": labels.get(("transport", via)), "count": group["count"],
                     "share": group["count"] / total if total else 0, "share_percent": round(group["count"] * 100 / total, 1) if total else 0,
                     "min_hops": min(group["hops"]) if group["hops"] else None, "max_hops": max(group["hops"]) if group["hops"] else None,
                     "interfaces": sorted(group["interfaces"]), "samples": group["samples"]})
    rows.sort(key=lambda row: (-row["count"], row["via"]))
    top = rows[0] if rows else None
    warnings = []
    if total >= PATH_HIGH: warnings.append("High path count: 1,000 or more paths are currently known.")
    elif total >= PATH_ELEVATED: warnings.append("Elevated path count: 100 or more paths are currently known.")
    if top and VALID_HASH.fullmatch(top["via"]) and top["count"] >= CONCENTRATION_MIN and top["share"] >= CONCENTRATION_SHARE:
        warnings.append("High concentration of learned paths through one transport.")
    return {"total": total, "groups": rows, "top": top, "unique_vias": len([row for row in rows if row["via"]]), "warnings": warnings}


def bounded_paths(paths: list[dict[str, Any]], limit: int = RAW_PATH_LIMIT) -> tuple[list[dict[str, Any]], int]:
    return paths[:limit], max(0, len(paths) - limit)

