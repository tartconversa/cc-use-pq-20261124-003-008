from __future__ import annotations

from datetime import datetime, timezone
from sqlite3 import Connection

from silverwatch import cases, evidence, reconciliation, registry
from silverwatch.ledger import current_entities

SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}


def investigation_queue(db: Connection, *, region: str | None = None,
                        as_of: datetime | None = None, cut: int | None = None) -> dict:
    instant = as_of or datetime.now(timezone.utc)
    findings = cases.all_findings(db, cut=cut, as_of=instant)
    touches = {row["entity_id"]: row["payload"]
               for row in current_entities(db, "touch", cut=cut, as_of=instant)}
    items = []
    for row in cases.current_cases(db, cut=cut, as_of=instant):
        body = row["payload"]
        batch = touches.get(body["batch_id"])
        if region and (batch is None or batch["region"] != region):
            continue
        if body["status"] == "closed":
            continue
        finding = findings[body["case_id"]]
        submitted_roles = {entry["role"] for entry in finding["decisions"]}
        missing_roles = sorted({"investigator", "compliance"} - submitted_roles)
        material = evidence.for_case(db, body["case_id"], cut=cut)
        age = max((instant - datetime.fromisoformat(body["reported_at"])).days, 0)
        reasons = []
        if body["severity"] in {"critical", "high"}:
            reasons.append("high_severity")
        if finding["finding"] == "inconclusive":
            reasons.append("review_disagreement")
        if missing_roles:
            reasons.append("review_incomplete")
        if not any(item["verified"] for item in material):
            reasons.append("verified_evidence_missing")
        items.append({"case_id": body["case_id"], "provider_id": body["provider_id"],
                      "batch_id": body["batch_id"], "district": batch["district"] if batch else None,
                      "severity": body["severity"], "status": body["status"],
                      "finding": finding["finding"], "age_days": age,
                      "missing_roles": missing_roles, "evidence_count": len(material),
                      "reasons": reasons, "event_seq": row["seq"]})
    items.sort(key=lambda item: (SEVERITY_ORDER[item["severity"]], -item["age_days"], item["case_id"]))
    return {"as_of": instant.isoformat(), "cut": cut, "count": len(items), "items": items}


def remediation_queue(db: Connection, *, as_of: datetime | None = None,
                      cut: int | None = None) -> dict:
    instant = as_of or datetime.now(timezone.utc)
    all_cases = {row["entity_id"]: row["payload"]
                 for row in current_entities(db, "case", cut=cut, as_of=instant)}
    items = []
    for action in cases.current_remediations(db, cut=cut, as_of=instant):
        due = datetime.fromisoformat(action["due_at"])
        case = all_cases.get(action["case_id"])
        state = "verified" if action["status"] == "verified" else action["status"]
        if state not in {"verified", "rejected"} and due < instant:
            state = "overdue"
        items.append({**action, "provider_id": action["provider_id"],
                      "case_severity": case["severity"] if case else None,
                      "case_status": case["status"] if case else "missing",
                      "queue_state": state, "days_to_due": (due - instant).days})
    items.sort(key=lambda item: (item["queue_state"] not in {"overdue", "submitted"},
                                 item["due_at"], item["action_id"]))
    return {"as_of": instant.isoformat(), "cut": cut, "count": len(items), "items": items}


def area_attention(db: Connection, *, region: str, as_of: datetime,
                   cut: int | None = None) -> dict:
    report = reconciliation.reconcile(db, cut=cut, as_of=as_of)
    incomplete = [row for row in report["batches"] if not row["complete"]]
    provider_rows = {row["provider_id"]: row for row in registry.catalog(db, region=region, cut=cut)}
    items = []
    for batch in incomplete:
        provider = provider_rows.get(batch["provider_id"])
        if provider is None:
            continue
        rank = 0 if "reports_without_exposure" in batch["problems"] else 1
        if "case_provider_mismatch" in batch["problems"]:
            rank = min(rank, 0)
        items.append({"district": provider["district"], "service_kind": provider["service_kind"],
                      "provider_id": batch["provider_id"], "batch_id": batch["batch_id"],
                      "difference": batch["difference"], "reasons": batch["problems"],
                      "priority": rank})
    items.sort(key=lambda item: (item["priority"], -abs(item["difference"]),
                                 item["district"], item["batch_id"]))
    return {"region": region, "as_of": as_of.isoformat(), "cut": cut,
            "count": len(items), "items": items}
