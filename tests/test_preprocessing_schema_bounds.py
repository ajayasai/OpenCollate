"""Manifest inventory bounds must agree in requests, receipts, and certificates."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import jsonschema
import pytest

from opencollate.sequential import replay, run_request
from opencollate.sequential_certificate import certify, verify_certificate
from opencollate.sequential_ir import SequentialError
from opencollate.sequential_spec import normalize

SCHEMAS = Path(__file__).parents[1] / "src/opencollate/schemas"


def _request(root: Path, roots: int, headers: int) -> dict[str, Any]:
    (root / "top.sv").write_text(
        "module top(input logic clk, input logic d, output logic q);\n"
        "always_ff @(posedge clk) q <= d;\nendmodule\n"
    )
    files = ["top.sv"]
    for i in range(roots - 1):
        name = f"extra{i}.sv"
        (root / name).write_text(f"// Extra compilation root {i}\n")
        files.append(name)
    names = []
    for i in range(headers):
        name = f"header{i}.svh"
        (root / name).write_text(f"// Bound unused header {i}\n")
        names.append(name)
    return {
        "schema_version": 1,
        "semantics": "two-valued-synchronous",
        "files": files,
        "preprocess": {"headers": names},
        "top": "top",
        "clock": "clk",
        "depth": 3,
        "induction": 1,
        "properties": [{"id": "copy", "source": "d", "sink": "q", "latency": 1}],
    }


@pytest.mark.parametrize("roots,headers", [(1, 32), (32, 128)])
def test_maximum_inventory_roundtrip_and_schema_validation(
    tmp_path: Path, roots: int, headers: int
) -> None:
    request = _request(tmp_path, roots, headers)
    receipt = run_request(request, root=tmp_path)
    assert receipt["status"] == "proven"
    certificate = certify(request, root=tmp_path)
    checked = verify_certificate(request, certificate, root=tmp_path)
    assert checked["status"] == "certificate-verified"
    assert replay(request, receipt, root=tmp_path) == receipt
    for value, kind in [
        (request, "sequential-request"),
        (receipt, "sequential-receipt"),
        (certificate, "sequential-certificate"),
        (checked, "certificate-verification"),
    ]:
        schema = json.loads((SCHEMAS / f"{kind}.schema.json").read_text())
        jsonschema.validate(value, schema)
    assert len(receipt["binding"]["sources"]) == roots + headers
    assert len(certificate["binding"]["sources"]) == roots + headers
    (tmp_path / request["preprocess"]["headers"][-1]).write_text("// changed\n")
    with pytest.raises(SequentialError, match="changed"):
        replay(request, receipt, root=tmp_path)


def test_inventory_limits_remain_bounded(tmp_path: Path) -> None:
    request = _request(tmp_path, 32, 128)
    normalize(request)
    bad = copy.deepcopy(request)
    bad["preprocess"]["headers"].append("overflow.svh")
    with pytest.raises(SequentialError, match="128"):
        normalize(bad)
    schema = json.loads((SCHEMAS / "sequential-request.schema.json").read_text())
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(bad, schema)
    receipt = run_request(request, root=tmp_path)
    certificate = certify(request, root=tmp_path)
    checked = verify_certificate(request, certificate, root=tmp_path)
    for value, kind in [
        (receipt, "sequential-receipt"),
        (certificate, "sequential-certificate"),
        (checked, "certificate-verification"),
    ]:
        value["binding"]["sources"].append(dict(value["binding"]["sources"][0]))
        schema = json.loads((SCHEMAS / f"{kind}.schema.json").read_text())
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(value, schema)
