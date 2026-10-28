from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from scripts import emergency_self_destruct as emergency


@pytest.mark.parametrize("status", ["active", "deleted", "missing", "wrong_owner"])
def test_emergency_deletion_checks_identity_and_confirms_async_completion(
    tmp_path: Path,
    status: str,
) -> None:
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {
                "api_key": "dummy-secret",
                "server_id": "42",
                "server_name": "owned-attempt",
                "product_id": 157,
            }
        )
    )
    server = {
        "name": "other" if status == "wrong_owner" else "owned-attempt",
        "productId": 157,
        "paymentTerm": "hour",
        "status": status,
    }
    with patch.object(
        emergency,
        "request",
        side_effect=[
            (404, None) if status == "missing" else (200, server),
            (204, None),
        ],
    ) as request:
        if status in {"active", "wrong_owner"}:
            with pytest.raises(emergency.EmergencySelfDestructError):
                emergency.self_destruct(config)
        else:
            emergency.self_destruct(config)
    assert request.call_count == (2 if status == "active" else 1)
    if status == "active":
        assert request.call_args.args == ("DELETE", "42", "dummy-secret")
