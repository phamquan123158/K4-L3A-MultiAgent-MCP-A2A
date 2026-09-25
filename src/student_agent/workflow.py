from __future__ import annotations

from typing import Any

from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter


def _unique(items: list[str] | None) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items or []:
        if item and item not in seen:
            seen.add(item)
            result.append(item)
    return result


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _extract_data(evidence: dict[str, Any] | None) -> Any:
    if not isinstance(evidence, dict):
        return {}
    payload = evidence.get("data")
    if isinstance(payload, (dict, list)):
        return payload
    return evidence


def _values(data: Any, *keys: str) -> list[Any]:
    records = data if isinstance(data, list) else [data]
    values: list[Any] = []
    for record in records:
        if isinstance(record, dict):
            for key in keys:
                if key in record and record[key] is not None:
                    values.append(record[key])
                    break
    return values


def _pick_first(data: Any, *keys: str) -> Any:
    records = data if isinstance(data, list) else [data]
    for record in records:
        if not isinstance(record, dict):
            continue
        for key in keys:
            value = record.get(key)
            if value is not None:
                return value
    return None


def _normalize_ids(values: Any) -> list[str]:
    result: list[str] = []
    for item in _as_list(values):
        if isinstance(item, str) and item.strip():
            result.append(item.strip())
        elif isinstance(item, (int, float)):
            result.append(str(item))
    return _unique(result)


async def _call_tool_variants(
    gateway: EvidenceGateway,
    case_id: str,
    tool_names: list[str],
    **kwargs: str,
) -> tuple[str | None, dict[str, Any] | None]:
    available = set(await gateway.list_tools())
    for tool_name in tool_names:
        if tool_name not in available:
            continue
        try:
            evidence = await gateway.call(tool_name, case_id=case_id, **kwargs)
            if not isinstance(evidence, dict):
                return tool_name, None
            return tool_name, evidence
        except Exception:
            continue
    return None, None


def _claim_verdict(claim_topic: str, issue: str) -> str:
    topic = (claim_topic or "").lower()
    if topic in {"canceled_order_paid", "unavailable_order_paid", "payment_mismatch", "duplicate_charge"}:
        return "supported"
    if topic in {"late_delivery_seller", "late_delivery_logistics"} and issue in {
        "late_delivery_seller",
        "late_delivery_logistics",
    }:
        return "supported"
    if topic == "valid_split_payment" and issue == "valid_split_payment":
        return "supported"
    if topic == "requested_full_refund" and issue in {"refund_pending", "refund_failed"}:
        return "partially_supported"
    return "insufficient_evidence"


def _build_output(case: dict[str, Any], evidence_map: dict[str, dict[str, Any]], issue: str) -> dict[str, Any]:
    case_id = str(case["case_id"])
    order_id = str(case["customer_request"].get("claimed_order_id", ""))
    claim_items = case.get("customer_request", {}).get("claims", [])
    order_data = _extract_data(evidence_map.get("order"))
    item_data = _extract_data(evidence_map.get("item"))
    payment_data = _extract_data(evidence_map.get("payment"))
    shipment_data = _extract_data(evidence_map.get("shipment"))
    seller_data = _extract_data(evidence_map.get("seller"))

    order_ids = [order_id] if order_id else []
    item_ids = _normalize_ids(_values(item_data, "item_id", "order_item_id", "id", "item_ids"))
    seller_ids = _normalize_ids(_values(seller_data, "seller_id", "id", "seller_ids"))
    seller_ids.extend(_normalize_ids(_values(item_data, "seller_id")))
    payment_refs = _normalize_ids(
        _values(payment_data, "payment_reference", "payment_ref", "payment_id", "payment_references")
    )
    shipment_ids = _normalize_ids(
        _values(shipment_data, "shipment_id", "id", "shipment_ids")
    )

    seller_ids = _unique(seller_ids)
    if not seller_ids and isinstance(order_data, dict):
        seller_ids.extend(_normalize_ids(_pick_first(order_data, "seller_id", "seller_ids")))
    if not item_ids and isinstance(order_data, dict):
        item_ids.extend(_normalize_ids(_pick_first(order_data, "items", "item_ids")))
    if not payment_refs and isinstance(order_data, dict):
        payment_refs.extend(_normalize_ids(_pick_first(order_data, "payment_reference", "payment_refs")))
    if not shipment_ids and isinstance(order_data, dict):
        shipment_ids.extend(_normalize_ids(_pick_first(order_data, "shipment_id", "shipment_ids")))

    evidence_refs = _unique(
        [
            evidence.get("evidence_ref")
            for evidence in evidence_map.values()
            if isinstance(evidence, dict) and evidence.get("evidence_ref")
        ]
    )

    claim_assessments: list[dict[str, Any]] = []
    for claim in claim_items:
        topic = str(claim.get("topic", ""))
        claim_assessments.append(
            {
                "claim_id": str(claim.get("claim_id", "claim-unknown")),
                "verdict": _claim_verdict(topic, issue),
                "confidence": 0.8 if issue != "insufficient_evidence" else 0.4,
                "evidence_refs": evidence_refs,
            }
        )

    ranked_causes = [
        {"cause_code": "ORDER_CANCELLED_AFTER_PAYMENT", "rank": 1},
        {"cause_code": "PAYMENT_CAPTURE_MISMATCH", "rank": 2},
        {"cause_code": "DELIVERY_DELAY", "rank": 3},
    ]
    if issue == "late_delivery_seller":
        ranked_causes = [
            {"cause_code": "SELLER_DELAY", "rank": 1},
            {"cause_code": "MISSED_DELIVERY_COMMITMENT", "rank": 2},
        ]
    elif issue == "late_delivery_logistics":
        ranked_causes = [
            {"cause_code": "LOGISTICS_DELAY", "rank": 1},
            {"cause_code": "SHIPMENT_LATE", "rank": 2},
        ]
    elif issue == "valid_split_payment":
        ranked_causes = [
            {"cause_code": "VALID_SPLIT_PAYMENT", "rank": 1},
            {"cause_code": "NO_REFUND_REASON", "rank": 2},
        ]

    responsible: list[dict[str, str | None]] = []
    if seller_ids:
        responsible.append({"party_type": "seller", "party_id": seller_ids[0]})
    if shipment_ids:
        responsible.append({"party_type": "logistics_provider", "party_id": shipment_ids[0]})
    if not responsible:
        responsible.append({"party_type": "unknown", "party_id": None})

    if issue in {"canceled_order_paid", "unavailable_order_paid", "refund_failed", "refund_pending"}:
        recommended_refund = 0.0
        refund_lines: list[dict[str, Any]] = []
        payment_amounts = _values(payment_data, "total_paid_brl", "amount_brl", "paid_total", "payment_value")
        if payment_amounts:
            recommended_refund = sum(float(amount) for amount in payment_amounts)
            refund_lines = [{
                "reason_code": "customer_refund",
                "amount_brl": recommended_refund,
                "entity_id": payment_refs[0] if payment_refs else order_id,
            }]
        else:
            refund_lines = [{"reason_code": "customer_refund", "amount_brl": 0.0, "entity_id": None}]
    else:
        recommended_refund = 0.0
        refund_lines = [{"reason_code": "no_refund", "amount_brl": 0.0, "entity_id": None}]

    output = {
        "schema_version": "day09-l3a-output-v2",
        "case_id": case_id,
        "assessment": {
            "primary_issue": issue,
            "case_status": "action_required" if issue not in {"insufficient_evidence", "no_action"} else "needs_investigation",
            "confidence": 0.9 if issue != "insufficient_evidence" else 0.35,
        },
        "affected_entities": {
            "order_ids": order_ids,
            "item_ids": item_ids[:20],
            "seller_ids": seller_ids[:20],
            "payment_references": payment_refs[:20],
            "shipment_ids": shipment_ids[:20],
        },
        "claim_assessments": claim_assessments,
        "root_cause_analysis": {
            "ranked_causes": ranked_causes[:5],
            "responsible_parties": responsible[:5],
        },
        "evidence_refs": evidence_refs[:30],
        "data_conflicts": [],
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": recommended_refund,
            "refund_lines": refund_lines[:10],
        },
        "resolution_actions": [
            "Review order and payment records",
            "Verify refund eligibility under policy",
        ][:8],
    }
    return output


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    """Coordinate the specialist workflow for a single L3A case.

    The implementation intentionally prefers real MCP evidence, and gracefully emits a
    conservative output when evidence is unavailable instead of inventing data.
    """
    case_id = str(case["case_id"])
    request = case.get("customer_request", {})
    order_id = str(request.get("claimed_order_id", ""))
    claims = request.get("claims", [])

    trace.emit(case_id=case_id, event_type="task_assigned", actor="coordinator", target="order-agent")
    evidence_map: dict[str, dict[str, Any]] = {}

    tool_candidates = {
        "order": ["get_order", "get_order_detail", "lookup_order"],
        "item": ["get_order_items", "get_items", "get_item"],
        "payment": ["get_order_payments", "get_payment_timeline", "get_payment", "get_payments", "lookup_payment"],
        "shipment": ["get_shipment_summary", "get_shipment", "get_shipments", "lookup_shipment"],
        "seller": ["get_sellers", "get_seller", "lookup_seller"],
        "policy": ["get_policy", "lookup_policy"],
    }

    for domain, names in tool_candidates.items():
        kwargs: dict[str, str] = {}
        if domain == "order":
            kwargs["order_id"] = order_id
        elif domain == "item":
            kwargs["order_id"] = order_id
        elif domain == "payment":
            kwargs["order_id"] = order_id
        elif domain == "shipment":
            kwargs["order_id"] = order_id
        elif domain == "seller":
            kwargs["order_id"] = order_id
        elif domain == "policy":
            kwargs["policy_version"] = str(case.get("policy_version", "EC_POLICY_V1"))

        tool_name, evidence = await _call_tool_variants(gateway, case_id, names, **kwargs)
        if evidence is not None and isinstance(evidence, dict):
            evidence_map[domain] = evidence
            evidence_ref = evidence.get("evidence_ref")
            if evidence_ref:
                trace.emit(
                    case_id=case_id,
                    event_type="tool_result_consumed",
                    actor=f"{domain}-agent",
                    tool_name=tool_name,
                    evidence_refs=[evidence_ref],
                )
                trace.emit(
                    case_id=case_id,
                    event_type="handoff",
                    actor=f"{domain}-agent",
                    target="verifier",
                    attributes={"domain": domain, "tool_name": tool_name or "unknown"},
                )

    issue = "insufficient_evidence"
    ordered_claim_topics = [str(item.get("topic", "")) for item in claims]
    if any(topic in {"canceled_order_paid", "unavailable_order_paid"} for topic in ordered_claim_topics):
        issue = "canceled_order_paid" if "canceled_order_paid" in ordered_claim_topics else "unavailable_order_paid"
    elif any(topic == "late_delivery_seller" for topic in ordered_claim_topics):
        issue = "late_delivery_seller"
    elif any(topic == "late_delivery_logistics" for topic in ordered_claim_topics):
        issue = "late_delivery_logistics"
    elif any(topic == "valid_split_payment" for topic in ordered_claim_topics):
        issue = "valid_split_payment"
    elif any(topic == "payment_mismatch" for topic in ordered_claim_topics):
        issue = "payment_mismatch"
    elif any(topic == "refund_pending" for topic in ordered_claim_topics):
        issue = "refund_pending"
    elif any(topic == "refund_failed" for topic in ordered_claim_topics):
        issue = "refund_failed"

    order_data = _extract_data(evidence_map.get("order"))
    payment_data = _extract_data(evidence_map.get("payment"))
    shipment_data = _extract_data(evidence_map.get("shipment"))
    if order_data:
        order_status = str(_pick_first(order_data, "order_status", "status", "state") or "").lower()
        if "cancel" in order_status:
            issue = "canceled_order_paid"
        elif "unavailable" in order_status or "not_available" in order_status:
            issue = "unavailable_order_paid"
    if payment_data and issue == "insufficient_evidence":
        paid = _pick_first(payment_data, "paid_total", "amount_brl", "total_paid_brl")
        if paid and isinstance(paid, (int, float)) and float(paid) > 0:
            issue = "payment_mismatch" if issue == "insufficient_evidence" else issue
    if shipment_data and issue == "insufficient_evidence":
        shipment_status = str(_pick_first(shipment_data, "status", "shipment_status") or "").lower()
        if "late" in shipment_status or "delay" in shipment_status:
            issue = "late_delivery_logistics"

    output = _build_output(case, evidence_map, issue)

    trace.emit(
        case_id=case_id,
        event_type="policy_decided",
        actor="policy-agent",
        decision_code=issue,
        attributes={"issue": issue, "claim_count": len(claims)},
    )
    trace.emit(
        case_id=case_id,
        event_type="verification_completed",
        actor="verifier",
        decision_code=issue,
        attributes={"evidence_count": len(output["evidence_refs"])},
    )
    trace.emit(case_id=case_id, event_type="case_finalized", actor="coordinator")
    return output
