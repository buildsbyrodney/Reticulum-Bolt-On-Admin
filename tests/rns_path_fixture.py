"""Disposable RNS destination publisher for isolated container integration tests."""
from __future__ import annotations

import argparse
import json
import os
import signal
import time
from pathlib import Path

import RNS


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--count", required=True, type=int)
    parser.add_argument("--prefix", required=True)
    args = parser.parse_args()
    if not 1 <= args.count <= 8:
        raise SystemExit("fixture destination count is out of bounds")

    RNS.Reticulum(configdir=str(args.config), require_shared_instance=True)
    destinations = []
    for index in range(args.count):
        identity = RNS.Identity()
        destination = RNS.Destination(
            identity, RNS.Destination.IN, RNS.Destination.SINGLE,
            "bolt_on_integration", args.prefix, str(index),
        )
        destinations.append(destination)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(".tmp")
    temporary.write_text(json.dumps({"destinations": [item.hash.hex() for item in destinations]}), encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, args.output)

    for destination in destinations:
        destination.announce()
        time.sleep(0.25)

    running = True

    def stop(*_args) -> None:
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    trigger = args.output.with_name("reannounce")
    by_hash = {item.hash.hex(): item for item in destinations}
    while running:
        if trigger.is_file():
            requested = trigger.read_text(encoding="utf-8").strip().lower()
            trigger.unlink(missing_ok=True)
            if requested in by_hash:
                by_hash[requested].announce()
        time.sleep(0.2)


if __name__ == "__main__":
    main()
