from __future__ import annotations

from datetime import datetime, timezone
import json
from sqlite3 import Connection

from pydantic import ValidationError

from silverwatch import cases
from silverwatch.contracts import Case, CaseEvidence, Remediation, ReviewDecision
from silverwatch.errors import conflict, invalid, missing
from silverwatch.ledger import append, current_entities, latest


def open_case(db: Connection, body: Case, revision: int, source_ref: str) -> dict:
    return cases.create(db, body, source_ref, revision)


def add_evidence(db: Connection, body: CaseEvidence) -> dict:
    from silverwatch.evidence import add

    return add(db, body)


def decide(db: Connection, body: ReviewDecision) -> dict:
    return cases.submit_decision(db, body)


def case_status(db: Connection, case_id: str, *, cut: int | None = None) -> dict:
    row = next((value for value in current_entities(db, "case", cut=cut)
                if value["entity_id"] == case_id), None)
    if row is None:
        raise missing("case", case_id)
    finding = cases.finding(db, case_id, cut=cut)
    remediations = [value for value in cases.current_remediations(db, cut=cut)
                    if value["case_id"] == case_id]
    from silverwatch.evidence import for_case

    material = for_case(db, case_id, cut=cut)
    return {
        "case": row["payload"], "finding": finding,
        "evidence": material, "remediations": remediations,
        "case_digest": row["digest"],
        "may_close": (row["payload"]["status"] == "closed"
                      and finding["finding"] in {"substantiated", "not_substantiated"}
                      and all(value["status"] == "verified" for value in remediations)),
    }


def close(db: Connection, case_id: str, expected_finding: str, revision: int) -> dict:
    state = case_status(db, case_id)
    if state["finding"]["finding"] != expected_finding:
        raise conflict("finding_changed", "案件核实结果已变化",
                       {"current": state["finding"]["finding"]})
    if state["finding"]["finding"] == "substantiated":
        if not state["remediations"] or any(item["status"] != "verified" for item in state["remediations"]):
            raise conflict("remediation_open", "已确认案件仍有未核验整改")
    body = {**state["case"], "status": "closed"}
    observed = datetime.now(timezone.utc)
    return append(db, "case", case_id, revision, "case-management", body, observed)


def cases_for_provider(db: Connection, provider_id: str, *, cut: int | None = None) -> list[dict]:
    return [case_status(db, row["entity_id"], cut=cut)
            for row in current_entities(db, "case", cut=cut)
            if row["payload"]["provider_id"] == provider_id]


def remediation_queue(db: Connection, *, at: datetime | None = None) -> dict:
    today = at or datetime.now(timezone.utc)
    rows = cases.current_remediations(db)
    return {
        "overdue": [row for row in rows if row["status"] not in {"verified", "rejected"}
                    and datetime.fromisoformat(row["due_at"]) < today],
        "awaiting_verification": [row for row in rows if row["status"] == "submitted"],
        "verified": [row for row in rows if row["status"] == "verified"],
    }
