from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from sqlite3 import Connection

from silverwatch import cases
from silverwatch.ledger import current_entities


def reconcile(db: Connection, *, cut: int | None = None,
              as_of: datetime | None = None) -> dict:
    """对照触达声明与案件录入数量，并检查两者共同的服务范围。"""
    touches = current_entities(db, "touch", cut=cut, as_of=as_of)
    providers = {row["entity_id"]: row["payload"]
                 for row in current_entities(db, "provider", cut=cut, as_of=as_of)}
    case_rows = cases.current_cases(db, cut=cut, as_of=as_of)
    by_batch: dict[str, list[dict]] = defaultdict(list)
    for row in case_rows:
        by_batch[row["payload"]["batch_id"]].append(row["payload"])
    results = []
    known_batches = {row["entity_id"]: row for row in touches}
    for batch_id, row in sorted(known_batches.items()):
        batch = row["payload"]
        reports = by_batch.get(batch_id, [])
        declared = batch["report_count"]
        problems = []
        if len(reports) < declared:
            problems.append("case_intake_incomplete")
        if len(reports) > declared:
            problems.append("case_intake_exceeds_declaration")
        provider = providers.get(batch["provider_id"])
        if provider is None:
            problems.append("provider_missing")
        elif (provider["region"] != batch["region"] or
              provider["district"] != batch["district"] or
              provider["service_kind"] != batch["service_kind"]):
            problems.append("provider_scope_changed")
        wrong_scope = [case["case_id"] for case in reports
                       if case["provider_id"] != batch["provider_id"]]
        if wrong_scope:
            problems.append("case_provider_mismatch")
        if batch["exposed_people"] == 0 and batch["report_count"]:
            problems.append("reports_without_exposure")
        results.append({"batch_id": batch_id, "provider_id": batch["provider_id"],
                        "declared_reports": declared, "received_cases": len(reports),
                        "difference": declared - len(reports), "wrong_scope_cases": wrong_scope,
                        "complete": not problems, "problems": problems,
                        "event_seq": row["seq"]})
    orphan_cases = sorted(row["entity_id"] for row in case_rows
                          if row["payload"]["batch_id"] not in known_batches)
    return {"cut": cut, "as_of": as_of.isoformat() if as_of else None,
            "batch_count": len(results), "complete_batch_count": sum(row["complete"] for row in results),
            "unmatched_case_ids": orphan_cases,
            "complete": not orphan_cases and all(row["complete"] for row in results),
            "batches": results}


def batch_status(db: Connection, batch_id: str, *, cut: int | None = None,
                 as_of: datetime | None = None) -> dict | None:
    result = reconcile(db, cut=cut, as_of=as_of)
    return next((row for row in result["batches"] if row["batch_id"] == batch_id), None)


def quality_summary(db: Connection, *, cut: int | None = None,
                    as_of: datetime | None = None) -> dict:
    report = reconcile(db, cut=cut, as_of=as_of)
    totals = {"declared_reports": 0, "received_cases": 0, "unmatched_reports": 0,
              "wrong_scope_cases": 0, "incomplete_batches": 0}
    categories: dict[str, int] = defaultdict(int)
    for row in report["batches"]:
        totals["declared_reports"] += row["declared_reports"]
        totals["received_cases"] += row["received_cases"]
        totals["unmatched_reports"] += max(row["difference"], 0)
        totals["wrong_scope_cases"] += len(row["wrong_scope_cases"])
        totals["incomplete_batches"] += int(not row["complete"])
        for problem in row["problems"]:
            categories[problem] += 1
    return {**totals, "unmatched_case_count": len(report["unmatched_case_ids"]),
            "issue_counts": dict(sorted(categories.items())),
            "complete": report["complete"], "cut": cut,
            "as_of": as_of.isoformat() if as_of else None}
