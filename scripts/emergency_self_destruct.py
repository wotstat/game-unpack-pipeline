#!/usr/bin/env python3
"""Delete this Aeza service after its four-hour lifetime; safe to retry via systemd."""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from collections.abc import Sequence
from pathlib import Path
from typing import Any

API_URL = "https://my.aeza.net/api/v2"


class EmergencySelfDestructError(RuntimeError):
    """An error safe to log without exposing provider payloads or credentials."""


def request(method: str, service_id: str, api_key: str) -> tuple[int, Any]:
    req = urllib.request.Request(
        f"{API_URL}/services/{service_id}",
        method=method,
        headers={"X-API-KEY": api_key, "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            raw = response.read()
            return response.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return 404, None
        raise EmergencySelfDestructError(f"Aeza returned HTTP {error.code}") from None
    except (OSError, ValueError):
        raise EmergencySelfDestructError("Aeza request failed") from None


def self_destruct(path: Path) -> None:
    try:
        config = json.loads(path.read_text())
        service_id = str(config["server_id"])
        key = config["api_key"]
        name = config["server_name"]
        product = config["product_id"]
        if not re.fullmatch(r"[1-9][0-9]*", service_id) or not key or not name:
            raise ValueError
    except (OSError, ValueError, KeyError, TypeError):
        raise EmergencySelfDestructError("Invalid emergency configuration") from None
    status, server = request("GET", service_id, key)
    if status == 404 or (isinstance(server, dict) and server.get("status") == "deleted"):
        return
    nested_product = server.get("product") if isinstance(server, dict) else None
    product_id = nested_product.get("id") if isinstance(nested_product, dict) else None
    if (
        not isinstance(server, dict)
        or server.get("name") != name
        or server.get("productId", product_id) != product
        or server.get("paymentTerm") != "hour"
    ):
        raise EmergencySelfDestructError("Aeza service ownership does not match")
    request("DELETE", service_id, key)
    # The DELETE queues work. Exit unsuccessfully so systemd polls again until
    # deletion is confirmed (then a GET returns deleted/404 and exits cleanly).
    raise EmergencySelfDestructError("Aeza deletion requested; awaiting confirmation")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        self_destruct(args.config)
        return 0
    except EmergencySelfDestructError as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
