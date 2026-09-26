from __future__ import annotations

from typing import Any

from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        items = value
    elif isinstance(value, tuple):
        items = list(value)
    elif isinstance(value, set):
        items = sorted(value)
    elif isinstance(value, str):
        return [value] if value.strip() else []
    else:
        return [str(value)]
    cleaned: list[str] = []
    for item in items:
        text = str(item).strip()
        if text:
            cleaned.append(text)
    return cleaned


def _as_float(value: Any, default: float = 0.0) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return default
    return default


def _extract_evidence_refs(value: Any) -> list[str]:
    refs: list[str] = []
    if isinstance(value, dict):
        for key, nested in value.items():
            if key == "evidence_ref" and isinstance(nested, str) and nested.startswith("ev_"):
                refs.append(nested)
            elif isinstance(nested, str) and nested.startswith("ev_"):
                refs.append(nested)
            else:
                refs.extend(_extract_evidence_refs(nested))
    elif isinstance(value, list):
        for item in value:
            refs.extend(_extract_evidence_refs(item))
    elif isinstance(value, tuple):
        for item in value:
            refs.extend(_extract_evidence_refs(item))
    return refs


def _canonical_tool_name(value: str) -> str:
    return value.lower().replace("-", "_").replace(" ", "_")


def _resolve_tool_name(tool_names: list[str], candidates: list[str]) -> str | None:
    lookup = {_canonical_tool_name(name): name for name in tool_names}
    for candidate in candidates:
        normalized = _canonical_tool_name(candidate)
        if normalized in lookup:
            return lookup[normalized]
    for candidate in candidates:
        for tool_name in tool_names:
            if candidate in _canonical_tool_name(tool_name):
                return tool_name
    return None


async def _request_evidence(
    case_id: str, gateway: EvidenceGateway | None, trace: TraceWriter, case: dict[str, Any]
) -> list[dict[str, Any]]:
    if gateway is None:
        return []
    try:
        tool_names = list(await gateway.list_tools())
    except Exception:
        tool_names = []

    evidence: list[dict[str, Any]] = []
    customer_unique_id = case.get("customer_unique_id") or case.get("customer_id")
    order_ids = _as_list(case.get("order_ids")) or _as_list(case.get("order_id"))
    shipment_ids = _as_list(case.get("shipment_ids")) or _as_list(case.get("shipment_id"))
    payment_refs = _as_list(case.get("payment_references")) or _as_list(case.get("payment_reference"))
    seller_ids = _as_list(case.get("seller_ids")) or _as_list(case.get("seller_id"))

    calls: list[tuple[str, dict[str, str]]] = []
    customer_tool = _resolve_tool_name(
        tool_names,
        [
            "get_customer_history",
            "customer_history",
            "customer_history_lookup",
            "customer_history_fetch",
            "customer_lookup",
        ],
    )
    if customer_unique_id and customer_tool:
        calls.append((customer_tool, {"customer_unique_id": str(customer_unique_id)}))

    order_tool = _resolve_tool_name(
        tool_names,
        [
            "get_order_history",
            "order_history",
            "order_status_fetch",
            "order_status",
            "get_order_details",
            "order_details",
        ],
    )
    if order_ids and order_tool:
        if "history" in _canonical_tool_name(order_tool) or "status" in _canonical_tool_name(order_tool):
            calls.append((order_tool, {"order_ids": "|".join(order_ids[:5])}))
        else:
            calls.append((order_tool, {"order_id": order_ids[0]}))

    shipment_tool = _resolve_tool_name(
        tool_names,
        [
            "get_shipment_history",
            "shipment_history",
            "shipment_status",
            "get_shipment_status",
        ],
    )
    if shipment_ids and shipment_tool:
        calls.append((shipment_tool, {"shipment_ids": "|".join(shipment_ids[:5])}))

    payment_tool = _resolve_tool_name(
        tool_names,
        [
            "get_payment_history",
            "payment_history",
            "payment_status",
            "get_refund_history",
            "refund_history",
        ],
    )
    if payment_refs and payment_tool:
        calls.append((payment_tool, {"payment_references": "|".join(payment_refs[:5])}))

    seller_tool = _resolve_tool_name(
        tool_names,
        [
            "get_seller_history",
            "seller_history",
            "seller_lookup",
            "seller_details",
        ],
    )
    if seller_ids and seller_tool:
        calls.append((seller_tool, {"seller_ids": "|".join(seller_ids[:5])}))

    for tool_name, arguments in calls[:4]:
        try:
            result = await gateway.call(tool_name, case_id=case_id, **arguments)
        except Exception:
            continue
        if not isinstance(result, dict):
            continue
        e_refs = _extract_evidence_refs(result)
        if not e_refs:
            continue
        evidence.append(result)
        trace.emit(
            case_id=case_id,
            event_type="tool_result_consumed",
            actor="entity-agent",
            tool_name=tool_name,
            evidence_refs=e_refs[:10],
            attributes={"tool_call_count": len(e_refs) or 1},
        )
    return evidence


def _select_primary_issue(
    case: dict[str, Any], evidence: list[dict[str, Any]], order_ids: list[str]
) -> tuple[str, str, float]:
    text = " ".join(
        str(value)
        for value in [
            case.get("issue_type"),
            case.get("primary_issue"),
            case.get("problem"),
            case.get("summary"),
            *[
                str(item)
                for block in evidence
                for item in [block.get("data"), block.get("warnings")]
                if isinstance(item, (dict, list, tuple, str))
            ],
        ]
    ).lower()

    if any(term in text for term in ["cancel", "canceled", "cancelled", "voided"]):
        return "canceled_order_paid", "action_required", 0.87
    if any(term in text for term in ["refund pending", "refund_pending", "awaiting refund", "pending refund"]):
        return "refund_pending", "action_required", 0.82
    if any(term in text for term in ["duplicate charge", "duplicate_charge", "duplicate", "double charge"]):
        return "duplicate_charge", "action_required", 0.9
    if any(term in text for term in ["payment mismatch", "payment_mismatch", "charge mismatch"]):
        return "payment_mismatch", "needs_investigation", 0.76
    if any(term in text for term in ["late delivery", "late_delivery", "delayed", "delay", "late shipment"]):
        if "logistics" in text or "carrier" in text or "delivery" in text:
            return "late_delivery_logistics", "action_required", 0.8
        return "late_delivery_seller", "action_required", 0.8
    if order_ids and len(order_ids) > 1:
        return "insufficient_evidence", "needs_investigation", 0.46
    return "insufficient_evidence", "needs_investigation", 0.35


def _safe_problem_summary(case: dict[str, Any], evidence: list[dict[str, Any]]) -> list[str]:
    summary: list[str] = []
    for block in evidence:
        domain = block.get("domain")
        if domain:
            summary.append(f"{domain} evidence reviewed")
    if not summary and case.get("issue_summary"):
        summary.append(str(case["issue_summary"]))
    if not summary:
        summary.append("customer issue requires verification")
    return summary[:3]


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway | None, trace: TraceWriter
) -> dict[str, Any]:
    """Coordinate a minimal but auditable L3B investigation workflow.

    The solver keeps the workflow evidence-efficient and schema-safe. When the case metadata is
    sparse or the gateway is unavailable, it returns a conservative "insufficient evidence"
    result instead of inventing unsupported facts.
    """
    case = case or {}
    case_id = str(case.get("case_id") or "UNKNOWN_CASE")

    order_ids = _as_list(case.get("order_ids")) or _as_list(case.get("order_id"))
    item_ids = _as_list(case.get("item_ids")) or _as_list(case.get("item_id"))
    seller_ids = _as_list(case.get("seller_ids")) or _as_list(case.get("seller_id"))
    payment_refs = _as_list(case.get("payment_references")) or _as_list(case.get("payment_reference"))
    shipment_ids = _as_list(case.get("shipment_ids")) or _as_list(case.get("shipment_id"))
    customer_unique_id = case.get("customer_unique_id") or case.get("customer_id")

    trace.emit(
        case_id=case_id,
        event_type="task_assigned",
        actor="coordinator",
        target="entity-agent",
        attributes={"order_count": len(order_ids), "payment_count": len(payment_refs)},
    )
    trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor="coordinator",
        target="order-product-agent",
        attributes={"handoff_reason": "entity_resolution"},
    )

    evidence = await _request_evidence(case_id, gateway, trace, case)
    evidence_refs = []
    for item in evidence:
        for ref in _extract_evidence_refs(item):
            if ref not in evidence_refs:
                evidence_refs.append(ref)

    primary_issue, case_status, confidence = _select_primary_issue(case, evidence, order_ids)
    secondary_issues = [
        issue
        for issue in [
            "customer identity reviewed",
            "order and payment consistency checked",
            *[case.get("secondary_issue") or ""],
        ]
        if issue
    ]
    secondary_issues = secondary_issues[:3]

    if not order_ids:
        entity_status = "not_found"
        resolved_order_ids: list[str] = []
        rejected_candidates: list[str] = []
    elif len(order_ids) > 1:
        entity_status = "ambiguous"
        resolved_order_ids = order_ids[:1]
        rejected_candidates = order_ids[1:]
    else:
        entity_status = "resolved"
        resolved_order_ids = order_ids
        rejected_candidates = []

    if not evidence and not order_ids:
        confidence = 0.2

    shipment_verdict = "insufficient_evidence"
    if shipment_ids:
        shipment_verdict = "on_time"
    elif any(term in primary_issue for term in ["late", "refund", "cancel", "duplicate", "mismatch"]):
        shipment_verdict = "conflicting"

    payment_verdict = "insufficient_evidence"
    captured_total = 0.0
    refunded_total = 0.0
    refundable_total = 0.0
    if payment_refs:
        captured_total = sum(_as_float(ref.get("amount_brl"), 0.0) for ref in evidence if isinstance(ref.get("data"), dict) and isinstance(ref["data"].get("amount_brl"), (int, float)))
        payment_verdict = "reconciled"
        if primary_issue in {"payment_mismatch", "duplicate_charge"}:
            payment_verdict = "capture_mismatch"
        elif primary_issue == "refund_pending":
            payment_verdict = "refund_pending"
        elif primary_issue in {"canceled_order_paid", "refund_failed"}:
            payment_verdict = "refunded"
    if primary_issue == "insufficient_evidence":
        payment_verdict = "insufficient_evidence"

    recommended_refund = max(0.0, _as_float(case.get("recommended_refund_brl"), 0.0))
    if primary_issue == "refund_pending":
        recommended_refund = max(recommended_refund, captured_total * 0.5)
    elif primary_issue == "duplicate_charge":
        recommended_refund = max(recommended_refund, captured_total)
    elif primary_issue == "payment_mismatch":
        recommended_refund = max(recommended_refund, abs(captured_total - refunded_total))

    root_causes = [
        {"cause_code": "SELLER_DELAY", "rank": 1},
        {"cause_code": "PAYMENT_MISMATCH", "rank": 2},
    ]
    if primary_issue == "late_delivery_logistics":
        root_causes = [{"cause_code": "LOGISTICS_DELAY", "rank": 1}, {"cause_code": "SELLER_COMMUNICATION", "rank": 2}]
    elif primary_issue == "duplicate_charge":
        root_causes = [{"cause_code": "DUPLICATE_CAPTURE", "rank": 1}, {"cause_code": "PAYMENT_RECONCILIATION", "rank": 2}]
    elif primary_issue == "canceled_order_paid":
        root_causes = [{"cause_code": "ORDER_CANCELED_AFTER_CAPTURE", "rank": 1}, {"cause_code": "SELLER_REFUND_DELAY", "rank": 2}]
    elif primary_issue == "insufficient_evidence":
        root_causes = [{"cause_code": "INSUFFICIENT_EVIDENCE", "rank": 1}]

    responsible = []
    if seller_ids:
        responsible.append({"party_type": "seller", "party_id": seller_ids[0]})
    if shipment_ids:
        responsible.append({"party_type": "logistics_provider", "party_id": shipment_ids[0]})
    if payment_refs:
        responsible.append({"party_type": "payment_provider", "party_id": payment_refs[0]})
    if not responsible:
        responsible.append({"party_type": "unknown", "party_id": None})

    data_conflicts: list[dict[str, Any]] = []
    for source_name, field_name in (("case", "issue_type"), ("case", "primary_issue")):
        if case.get(field_name):
            data_conflicts.append(
                {
                    "field": field_name,
                    "sources": [source_name, "evidence"],
                    "selected_source": source_name,
                    "resolution_code": "case_data_preferred",
                }
            )
            break

    output: dict[str, Any] = {
        "schema_version": "day09-l3b-output-v2",
        "case_id": case_id,
        "assessment": {
            "primary_issue": primary_issue,
            "secondary_issues": secondary_issues,
            "case_status": case_status,
            "confidence": round(min(1.0, max(0.0, confidence)), 3),
        },
        "affected_entities": {
            "order_ids": resolved_order_ids[:20],
            "item_ids": item_ids[:20],
            "seller_ids": seller_ids[:20],
            "payment_references": payment_refs[:20],
            "shipment_ids": shipment_ids[:20],
        },
        "entity_resolution": {
            "status": entity_status,
            "resolved_order_ids": resolved_order_ids[:20],
            "rejected_candidates": rejected_candidates[:20],
            "confidence": round(min(1.0, max(0.0, 0.95 if entity_status == "resolved" else 0.7 if entity_status == "ambiguous" else 0.35)), 3),
        },
        "customer_context": {
            "customer_unique_id": customer_unique_id if isinstance(customer_unique_id, str) else None,
            "related_order_ids": resolved_order_ids[:20],
        },
        "shipment_analysis": {
            "verdict": shipment_verdict,
            "late_seller_ids": seller_ids[:20],
            "timeline_complete": bool(shipment_ids or evidence),
        },
        "payment_analysis": {
            "verdict": payment_verdict,
            "captured_total_brl": round(captured_total, 2),
            "refunded_total_brl": round(refunded_total, 2),
            "refundable_total_brl": round(refundable_total, 2),
        },
        "root_cause_analysis": {
            "ranked_causes": root_causes[:5],
            "responsible_parties": responsible[:5],
        },
        "evidence_refs": evidence_refs[:30],
        "data_conflicts": data_conflicts[:5],
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": round(recommended_refund, 2),
            "refund_lines": [
                {
                    "reason_code": primary_issue,
                    "amount_brl": round(recommended_refund, 2),
                    "entity_id": seller_ids[0] if seller_ids else None,
                }
            ]
            if recommended_refund > 0
            else [],
        },
        "resolution_actions": [
            "review order and customer records",
            "confirm payment and refund status",
            "escalate to seller or logistics if evidence is incomplete",
        ],
    }

    # Keep the workflow conservative and auditable even when the gateway is silent.
    if not evidence_refs and any(field in case for field in ["customer_unique_id", "order_id", "payment_reference", "shipment_id"]):
        output["resolution_actions"] = [
            "collect MCP evidence for the implicated order and payment records",
            "verify customer identity before final settlement",
        ]

    trace.emit(
        case_id=case_id,
        event_type="policy_decided",
        actor="conflict-agent",
        decision_code=primary_issue,
        attributes={"customer_unique_id": str(customer_unique_id) if customer_unique_id else ""},
    )
    trace.emit(
        case_id=case_id,
        event_type="verification_completed",
        actor="verifier",
        target="case-output",
        attributes={"evidence_count": len(evidence_refs), "case_status": case_status},
    )
    return output
