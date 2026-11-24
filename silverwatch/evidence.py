from __future__ import annotations

from sqlite3 import Connection

from pydantic import ValidationError

from silverwatch.contracts import CaseEvidence
from silverwatch.digest import digest
from silverwatch.errors import conflict, invalid, missing
from silverwatch.ledger import append, current_entities, latest


def add(db: Connection, item: CaseEvidence) -> dict:
    case = next((row for row in current_entities(db, "case") if row["entity_id"] == item.case_id), None)
    if case is None:
        raise missing("case", item.case_id)
    body = item.model_dump(mode="json")
    body_digest = digest(body)
    existing = next((row for row in latest(db, "case_evidence", entity_id=item.evidence_id)), None)
    if existing:
        if digest(existing["payload"]) != body_digest:
            raise conflict("evidence_changed", "证据编号已对应其他内容")
        return {"evidence_id": item.evidence_id, "digest": existing["digest"], "replayed": True}
    return {**append(db, "case_evidence", item.evidence_id, 1, item.source_ref,
                      body, item.observed_at), "replayed": False}


def for_case(db: Connection, case_id: str, *, cut: int | None = None,
             require_verified: bool = False) -> list[dict]:
    cases = {row["entity_id"] for row in current_entities(db, "case", cut=cut)}
    if case_id not in cases:
        raise missing("case", case_id)
    result = []
    for row in latest(db, "case_evidence", cut=cut):
        body = row["payload"]
        if body["case_id"] == case_id and (not require_verified or body["verified"]):
            result.append({**body, "digest": row["digest"], "event_seq": row["seq"]})
    return result


def verify_refs(db: Connection, case_id: str, references: list[str], *, cut: int | None = None,
                as_of: datetime | None = None) -> list[dict]:
    available = {row["evidence_id"]: row for row in for_case(db, case_id, cut=cut)
                 if as_of is None or row["observed_at"] <= as_of.isoformat()}
    absent = sorted(set(references) - set(available))
    if absent:
        raise invalid("evidence_reference", "复核引用了未登记证据", {"missing": absent})
    return [available[key] for key in sorted(set(references))]

