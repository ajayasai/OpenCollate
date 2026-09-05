from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from opencollate.cli import main
from opencollate.formal import (
    SEMANTICS,
    _digest,
    replay_receipt,
    run_obligations,
    validate_obligations,
)


def request(left: str = "A", right: str = "A", assume: str = "1") -> dict:
    return {
        "schema_version": 1,
        "semantics": SEMANTICS,
        "obligations": [{"id": "route", "left": left, "right": right, "assume": assume}],
    }


@pytest.mark.parametrize(
    ("right", "guard", "code"), [("A", "1", 0), ("!A", "1", 1), ("A", "S&!S", 2), ("A?B:C", "1", 2)]
)
def test_receipts_and_replay(right: str, guard: str, code: int) -> None:
    value = request(right=right, assume=guard)
    receipt = run_obligations(value)
    assert receipt["exit_code"] == code
    assert replay_receipt(value, receipt) == receipt


def test_changed_request_or_content_is_rejected() -> None:
    original = request()
    receipt = run_obligations(original)
    with pytest.raises(ValueError, match="different"):
        replay_receipt(request(right="!A"), receipt)
    receipt["results"][0]["status"] = "different"
    with pytest.raises(ValueError, match="digest"):
        replay_receipt(original, receipt)


def test_recomputing_digest_does_not_make_false_proof_trusted() -> None:
    value = request(right="!A")
    receipt = run_obligations(value)
    receipt["results"][0]["status"] = "equivalent"
    receipt["results"][0]["counterexample"] = None
    receipt["status"], receipt["exit_code"] = "pass", 0
    receipt["receipt_sha256"] = _digest({k: v for k, v in receipt.items() if k != "receipt_sha256"})
    with pytest.raises(ValueError, match="rerun"):
        replay_receipt(value, receipt)


@pytest.mark.parametrize(
    "change",
    [
        {"schema_version": True},
        {"semantics": "sequential"},
        {"obligations": []},
        {"unknown": 1},
        {"obligations": [{"id": "x", "left": "A"}]},
        {"obligations": [{"id": "x", "left": "A", "right": "A", "assume": False}]},
    ],
)
def test_strict_obligations(change: dict) -> None:
    value = {**request(), **change}
    with pytest.raises(ValueError):
        validate_obligations(value)


def test_duplicate_ids_and_missing_results() -> None:
    value = request()
    value["obligations"] *= 2
    with pytest.raises(ValueError, match="unique"):
        run_obligations(value)
    value = request()
    receipt = run_obligations(value)
    receipt["results"] = []
    receipt["receipt_sha256"] = _digest({k: v for k, v in receipt.items() if k != "receipt_sha256"})
    with pytest.raises(ValueError, match="missing"):
        replay_receipt(value, receipt)


def test_order_is_canonical_and_assumptions_are_bound() -> None:
    value = request()
    value["obligations"].append({"id": "another", "left": "B", "right": "B"})
    reverse = copy.deepcopy(value)
    reverse["obligations"].reverse()
    assert run_obligations(value) == run_obligations(reverse)
    assert (
        run_obligations(request(assume="S"))["request_sha256"]
        != run_obligations(request())["request_sha256"]
    )


def test_cli_check_replay_and_output_alias(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source, receipt = tmp_path / "request.json", tmp_path / "receipt.json"
    source.write_text(json.dumps(request()))
    assert main(["formal", "check", str(source), "-o", str(receipt)]) == 0
    assert main(["formal", "replay", str(source), str(receipt)]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "pass"
    assert main(["formal", "check", str(source), "-o", str(source)]) == 2
    assert json.loads(source.read_text()) == request()
    assert main(["formal", "check", str(source), "--timeout-ms", "0"]) == 2


def test_duplicate_json_keys_are_rejected(tmp_path: Path) -> None:
    source = tmp_path / "ambiguous.json"
    source.write_text('{"schema_version":1,"schema_version":1}')
    assert main(["formal", "check", str(source)]) == 2
