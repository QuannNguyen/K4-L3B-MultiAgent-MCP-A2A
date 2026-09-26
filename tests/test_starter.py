from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from student_agent import OUTPUT_SCHEMA_VERSION, VARIANT_ID
from student_agent.cases import CaseSet, load_case_set
from student_agent.contracts import Contracts
from student_agent.submission import build_manifest
from student_agent.workflow import solve_case


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def test_load_case_set_rejects_wrong_variant(tmp_path: Path) -> None:
    write_json(
        tmp_path / "case-set.json",
        {"case_set_version": "test-v1", "variant_id": "l3a", "case_ids": ["CASE_001"]},
    )
    write_json(tmp_path / "inputs" / "CASE_001.json", {"case_id": "CASE_001"})
    with pytest.raises(ValueError, match="expected variant"):
        load_case_set(tmp_path, expected_count=1)


def test_load_case_set_accepts_exact_input_inventory(tmp_path: Path) -> None:
    case_ids = ["CASE_001", "CASE_002"]
    write_json(
        tmp_path / "case-set.json",
        {"case_set_version": "test-v1", "variant_id": VARIANT_ID, "case_ids": case_ids},
    )
    for case_id in case_ids:
        write_json(tmp_path / "inputs" / f"{case_id}.json", {"case_id": case_id})
    loaded = load_case_set(tmp_path, expected_count=2)
    assert loaded.case_ids == tuple(case_ids)


def test_generated_manifest_matches_public_contract() -> None:
    root = Path(__file__).resolve().parents[1]
    contracts = Contracts(root / "contracts" / "schemas")
    case_set = CaseSet("test-v1", VARIANT_ID, ("CASE_001",), {})
    manifest = build_manifest(case_set)
    contracts.validate_manifest(manifest)
    assert manifest["output_schema_version"] == OUTPUT_SCHEMA_VERSION


def test_solve_case_uses_real_mcp_evidence_refs() -> None:
    class FakeTrace:
        def emit(self, **kwargs: object) -> None:
            return None

    class FakeGateway:
        async def list_tools(self) -> list[str]:
            return ["customer_history_lookup", "order_status_fetch"]

        async def call(self, tool_name: str, *, case_id: str, **arguments: object) -> dict[str, object]:
            assert case_id == "L3B_CASE_007"
            return {
                "schema_version": "day09-mcp-evidence-v1",
                "evidence_ref": "ev_0123456789abcdef0123456789abcdef",
                "result_hash": "sha256:" + "0" * 64,
                "domain": "customer",
                "data": {"customer_unique_id": "CUST_001", "order_ids": ["ORDER_123"]},
            }

    case = {
        "case_id": "L3B_CASE_007",
        "customer_unique_id": "CUST_001",
        "order_ids": ["ORDER_123"],
        "issue_type": "late delivery",
    }
    output = asyncio.run(solve_case(case, FakeGateway(), FakeTrace()))
    assert output["evidence_refs"] == ["ev_0123456789abcdef0123456789abcdef"]
    assert output["case_id"] == "L3B_CASE_007"
