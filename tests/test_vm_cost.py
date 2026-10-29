from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from scripts import report_vm_cost as cost
from scripts import runner_lifecycle as lifecycle

CONFIG = lifecycle.AezaConfig("dummy-cost-api-key")


def transaction(
    identity: int, kind: str = "buy", amount: int = 53, **changes: Any
) -> dict[str, Any]:
    return {
        "id": identity,
        "serviceId": 42,
        "status": "performed",
        "type": kind,
        "amount": amount,
        "bonusAmount": 0,
        **changes,
    }


def test_cost_uses_this_vms_performed_rent_and_deduplicates_pages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [
        transaction(1),
        transaction(2, "prolong"),
        transaction(3, "prolong", -53),
        transaction(2, "prolong"),
        transaction(4, "prolong", status="created"),
        transaction(5, "buy", status="cancelled"),
        transaction(6, serviceId=99),
        transaction(7, "replenishment", 5000, serviceId=None),
    ]
    monkeypatch.setattr(lifecycle, "aeza_items", lambda _config, _path: rows)
    assert cost.rental_cost_cents(CONFIG, {"42"}) == 159


def test_cost_accounts_for_bonus_spend_and_service_refunds(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [
        transaction(1, amount=-40, bonusAmount=-13),
        transaction(2, "prolong"),
        transaction(3, "refund", 20),
        transaction(4, "compensation", 3),
    ]
    monkeypatch.setattr(lifecycle, "aeza_items", lambda _config, _path: rows)
    assert cost.rental_cost_cents(CONFIG, {"42"}) == 83


def test_missing_charges_do_not_become_a_free_vm(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lifecycle, "aeza_items", lambda _config, _path: [])
    assert cost.rental_cost_cents(CONFIG, {"42"}) is None
    assert cost.rental_cost_cents(CONFIG, set()) is None


@pytest.mark.parametrize(
    "rows",
    [
        [transaction(1, "edit_resources")],
        [transaction(1), transaction(1, amount=106)],
        [transaction(1), transaction(2, "refund", 106)],
    ],
)
def test_ambiguous_ledger_is_not_reported_as_a_final_cost(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rows: list[dict[str, Any]]
) -> None:
    output = tmp_path / "outputs"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setenv("BILLING_SERVICE_IDS", '["42"]')
    monkeypatch.setenv("VM_DELETED", "true")
    monkeypatch.setenv("AEZA_API_KEY", CONFIG.api_key)
    monkeypatch.setattr(lifecycle, "aeza_items", lambda _config, _path: rows)
    cost.main()
    assert output.read_text() == "vm_cost_state=unavailable\nvm_cost_cents=\n"


@pytest.mark.parametrize("amount", [None, True, 0.53, "NaN", "Infinity", "<script>"])
def test_invalid_amounts_are_not_rounded_into_a_cost(
    monkeypatch: pytest.MonkeyPatch, amount: Any
) -> None:
    monkeypatch.setattr(
        lifecycle, "aeza_items", lambda _config, _path: [transaction(1, amount=amount)]
    )
    with pytest.raises(ValueError):
        cost.rental_cost_cents(CONFIG, {"42"})


@pytest.mark.parametrize("deleted,state", [("true", "final"), ("false", "accrued")])
def test_report_outputs_only_sanitized_cost_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, deleted: str, state: str
) -> None:
    output = tmp_path / "outputs"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setenv("BILLING_SERVICE_IDS", '["42"]')
    monkeypatch.setenv("VM_DELETED", deleted)
    monkeypatch.setenv("AEZA_API_KEY", CONFIG.api_key)
    monkeypatch.setattr(lifecycle, "aeza_items", lambda _config, _path: [transaction(1)])
    cost.main()
    assert output.read_text() == f"vm_cost_state={state}\nvm_cost_cents=53\n"
    assert CONFIG.api_key not in output.read_text()


def test_failed_cost_read_never_logs_provider_diagnostics_or_fails_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    output = tmp_path / "outputs"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setenv("BILLING_SERVICE_IDS", '["42"]')
    monkeypatch.setenv("AEZA_API_KEY", CONFIG.api_key)

    def fail(_config: Any, _path: str) -> list[dict[str, Any]]:
        raise RuntimeError("private-account-data " + CONFIG.api_key)

    monkeypatch.setattr(lifecycle, "aeza_items", fail)
    cost.main()
    assert output.read_text() == "vm_cost_state=unavailable\nvm_cost_cents=\n"
    logged = capsys.readouterr().out
    assert "private-account-data" not in logged
    assert CONFIG.api_key not in logged


def test_no_vm_does_not_query_account_billing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "outputs"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setenv("BILLING_SERVICE_IDS", "[]")
    monkeypatch.delenv("AEZA_API_KEY", raising=False)
    cost.main()
    assert output.read_text() == "vm_cost_state=unavailable\nvm_cost_cents=\n"


def test_workflow_cost_is_best_effort_and_aeza_key_stays_out_of_telegram() -> None:
    path = Path(__file__).parents[1] / ".github/workflows/process-game-release.yml"
    jobs = yaml.safe_load(path.read_text())["jobs"]
    cleanup = jobs["cleanup"]
    step = next(s for s in cleanup["steps"] if s.get("id") == "cost")
    assert step["if"] == "always()"
    assert step["continue-on-error"] is True
    assert step["timeout-minutes"] == 2
    assert step["env"]["BILLING_SERVICE_IDS"] == "${{ steps.cleanup.outputs.billing_service_ids }}"
    assert cleanup["steps"].index(step) > next(
        i for i, item in enumerate(cleanup["steps"]) if item.get("id") == "cleanup"
    )
    render = next(s for s in jobs["notify"]["steps"] if s.get("id") == "report")
    assert render["env"]["VM_COST_CENTS"] == "${{ needs.cleanup.outputs.vm_cost_cents }}"
    assert "AEZA_API_KEY" not in render["env"]
