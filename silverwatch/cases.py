from __future__ import annotations

from datetime import datetime, timezone
from sqlite3 import Connection

from silverwatch import evidence
from silverwatch.contracts import Case, Remediation, ReviewDecision
from silverwatch.digest import digest
from silverwatch.errors import conflict, invalid, missing
from silverwatch.ledger import append, current_entities, latest


def create(db: Connection, body: Case, source_ref: str, revision: int) -> dict:
    providers = {row["entity_id"] for row in current_entities(db, "provider")}
    if body.provider_id not in providers:
        raise missing("provider", body.provider_id)
    batches = {row["entity_id"]: row for row in current_entities(db, "touch")}
    batch = batches.get(body.batch_id)
    if batch is None:
        raise missing("touch_batch", body.batch_id)
    touch = batch["payload"]
    if touch["provider_id"] != body.provider_id or touch["region"] == "":
        raise invalid("case_scope", "线索归属与触达批次不一致")
    return append(db, "case", body.case_id, revision, source_ref,
                  body.model_dump(mode="json"), body.reported_at)


def decisions(db: Connection, case_id: str, *, cut: int | None = None,
              as_of: datetime | None = None) -> list[dict]:
    cases = {row["entity_id"] for row in current_entities(db, "case", cut=cut, as_of=as_of)}
    if case_id not in cases:
        raise missing("case", case_id)
    return [row["payload"] | {"event_seq": row["seq"], "digest": row["digest"]}
            for row in latest(db, "decision", cut=cut, as_of=as_of)
            if row["entity_id"] == case_id]


def current_cases(db: Connection, *, cut: int | None = None,
                  as_of: datetime | None = None) -> list[dict]:
    return current_entities(db, "case", cut=cut, as_of=as_of)


def submit_decision(db: Connection, body: ReviewDecision) -> dict:
    case_row = next((row for row in current_entities(db, "case")
                     if row["entity_id"] == body.case_id), None)
    if case_row is None:
        raise missing("case", body.case_id)
    case = case_row["payload"]
    if body.decided_at < datetime.fromisoformat(case["reported_at"]):
        raise invalid("decision_time", "决定时间早于线索上报时间")
    selected = evidence.verify_refs(db, body.case_id, body.evidence_refs, as_of=body.decided_at)
    if body.finding == "substantiated" and any(not row["verified"] for row in selected):
        raise conflict("unverified_evidence", "核实成立须引用已确认的材料")
    current = decisions(db, body.case_id)
    existing = next((row for row in current if row["role"] == body.role), None)
    payload = body.model_dump(mode="json")
    fingerprint = digest(payload)
    if existing:
        if digest({key: value for key, value in existing.items()
                  if key not in {"event_seq", "digest"}}) != fingerprint:
            raise conflict("decision_sealed", "该岗位已提交决定，不能重复改写")
        return {**payload, "digest": fingerprint, "replayed": True}
    if any(row["reviewer"] == body.reviewer for row in current):
        raise conflict("reviewer_not_independent", "两项复核必须由不同人员完成")
    result = append(db, "decision", body.case_id, 1, f"{body.role}:{body.reviewer}",
                    payload, body.decided_at)
    return {**payload, **result}


def combine_findings(case_id: str, rows: list[dict]) -> dict:
    by_role = {row["role"]: row for row in rows}
    if set(by_role) != {"investigator", "compliance"}:
        value = "pending"
    elif by_role["investigator"]["finding"] != by_role["compliance"]["finding"]:
        value = "inconclusive"
    else:
        value = by_role["investigator"]["finding"]
    return {"case_id": case_id, "finding": value,
            "decisions": [by_role[role] for role in sorted(by_role)]}


def finding(db: Connection, case_id: str, *, cut: int | None = None,
            as_of: datetime | None = None) -> dict:
    return combine_findings(case_id, decisions(db, case_id, cut=cut, as_of=as_of))


def all_findings(db: Connection, *, cut: int | None = None,
                 as_of: datetime | None = None) -> dict[str, dict]:
    return {row["entity_id"]: combine_findings(
                row["entity_id"], decisions(db, row["entity_id"], cut=cut, as_of=as_of))
            for row in current_entities(db, "case", cut=cut, as_of=as_of)}


def add_remediation(db: Connection, body: Remediation) -> dict:
    resolved = finding(db, body.case_id)
    if resolved["finding"] != "substantiated":
        raise conflict("case_not_substantiated", "只有两岗位一致确认的案件可以制定整改")
    if body.provider_id not in {row["payload"]["provider_id"] for row in current_entities(db, "case")
                                if row["entity_id"] == body.case_id}:
        raise invalid("remediation_provider", "整改提供方与案件不匹配")
    prior = [row for row in current_entities(db, "remediation") if row["entity_id"] == body.action_id]
    if prior:
        if digest(prior[0]["payload"]) != digest(body.model_dump(mode="json")):
            raise conflict("remediation_changed", "整改编号已登记")
        return prior[0]
    return append(db, "remediation", body.action_id, 1, "operator",
                  body.model_dump(mode="json"), body.due_at)


def update_remediation(db: Connection, action_id: str, body: Remediation, revision: int) -> dict:
    current = next((row for row in current_entities(db, "remediation")
                    if row["entity_id"] == action_id), None)
    if current is None:
        raise missing("remediation", action_id)
    old = current["payload"]
    if body.action_id != action_id or body.case_id != old["case_id"] or body.provider_id != old["provider_id"]:
        raise invalid("remediation_identity", "整改更新不能替换责任对象")
    if body.status == "verified" and body.owner == body.verified_by:
        raise invalid("remediation_independent", "责任人不能复核自己的整改")
    return append(db, "remediation", action_id, revision, "operator",
                  body.model_dump(mode="json"), body.verified_at or datetime.now(timezone.utc))


def current_remediations(db: Connection, *, cut: int | None = None,
                         as_of: datetime | None = None) -> list[dict]:
    return [row["payload"] | {"event_seq": row["seq"]}
            for row in current_entities(db, "remediation", cut=cut, as_of=as_of)]
