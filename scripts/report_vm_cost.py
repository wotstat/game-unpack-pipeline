"""Report this attempt's Aeza rental debits after cleanup, without exposing the ledger."""

from __future__ import annotations

import json
import os
import re
from decimal import Decimal, InvalidOperation
from typing import Any

from scripts import runner_lifecycle as lifecycle


def _cents(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError("Invalid billing amount")
    try:
        amount = Decimal(str(value))
    except InvalidOperation:
        raise ValueError("Invalid billing amount") from None
    if not amount.is_finite() or amount != amount.to_integral_value():
        raise ValueError("Invalid billing amount")
    return abs(int(amount))


def rental_cost_cents(config: lifecycle.AezaConfig, service_ids: set[str]) -> int | None:
    """Sum performed rent operations only; never infer spend from an account balance."""
    if not service_ids:
        return None
    charges = 0
    total = 0
    seen: dict[str, tuple[str, int]] = {}
    for item in lifecycle.aeza_items(config, "billing/transactions"):
        if str(item.get("serviceId")) not in service_ids or item.get("status") != "performed":
            continue
        kind = item.get("type")
        amount = _cents(item.get("amount")) + _cents(item.get("bonusAmount", 0))
        # Aeza records a zero-value order beside the actual hourly prolong debit.
        if kind == "order" and amount == 0:
            continue
        if kind not in {"buy", "prolong", "refund", "compensation"}:
            raise ValueError("Unexpected service billing operation")
        transaction_id = str(item.get("id", ""))
        if not re.fullmatch(r"[1-9][0-9]*", transaction_id):
            raise ValueError("Invalid billing transaction")
        entry = (str(kind), amount)
        if transaction_id in seen:
            if seen[transaction_id] != entry:
                raise ValueError("Conflicting billing transaction")
            continue
        seen[transaction_id] = entry
        if kind in {"buy", "prolong"}:
            charges += int(amount > 0)
            total += amount
        else:
            total -= amount
    if not charges or total < 0:
        return None
    return total


def main() -> None:
    # Only IDs captured by this run's cleanup enter the reporter. No Aeza key or
    # account-wide financial data is passed to the Telegram Environment.
    state = "unavailable"
    cost: int | None = None
    try:
        raw = json.loads(os.environ.get("BILLING_SERVICE_IDS") or "[]")
        if not isinstance(raw, list) or any(
            not isinstance(value, str) or re.fullmatch(r"[1-9][0-9]*", value) is None
            for value in raw
        ):
            raise ValueError("Invalid billing service identities")
        if raw:
            cost = rental_cost_cents(lifecycle.AezaConfig.from_environment(), set(raw))
        if cost is not None:
            state = "final" if os.environ.get("VM_DELETED") == "true" else "accrued"
    except Exception as error:
        # Provider exceptions/payloads may contain unrelated financial data.
        print(f"::warning::Aeza VM cost unavailable ({type(error).__name__})")
    lifecycle.write_output("vm_cost_state", state)
    lifecycle.write_output("vm_cost_cents", "" if cost is None else cost)


if __name__ == "__main__":
    main()
