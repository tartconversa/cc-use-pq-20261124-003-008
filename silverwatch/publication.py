from __future__ import annotations

from datetime import datetime, timezone
import json
from sqlite3 import Connection

from silverwatch import cases
from silverwatch.digest import digest
from silverwatch.errors import conflict, invalid, missing
from silverwatch.storage import encode


def store_analysis(db: Connection, analysis_id: str, cut: int, as_of: datetime,
                   policy, document: dict) -> dict:
    revision = (db.execute("SELECT COALESCE(MAX(revision),0)+1 FROM analysis WHERE analysis_id=?",
                           (analysis_id,)).fetchone()[0])
    fingerprint = digest(document)
    db.execute("INSERT INTO analysis VALUES(?,?,?,?,?,?,?,?,?)",
               (analysis_id, revision, cut, as_of.isoformat(), encode(policy.model_dump(mode="json")),
                encode(document), fingerprint, "draft", datetime.now(timezone.utc).isoformat()))
    return {"analysis_id": analysis_id, "revision": revision, "cut": cut,
            "digest": fingerprint, "state": "draft"}


def selected_analysis(db: Connection, analysis_id: str, revision: int | None = None):
    if revision is None:
        row = db.execute("SELECT * FROM analysis WHERE analysis_id=? ORDER BY revision DESC LIMIT 1",
                         (analysis_id,)).fetchone()
    else:
        row = db.execute("SELECT * FROM analysis WHERE analysis_id=? AND revision=?",
                         (analysis_id, revision)).fetchone()
    if row is None:
        raise missing("analysis", analysis_id)
    return row


def approval_set(db: Connection, analysis_id: str, revision: int | None = None) -> dict:
    row = selected_analysis(db, analysis_id, revision)
    document = json.loads(row["document"])
    approvals = [entry for entry in db.execute("SELECT * FROM approvals WHERE analysis_id=?",
                                                (analysis_id,))
                 if entry["analysis_digest"] == row["digest"]]
    roles = {entry["role"]: entry for entry in approvals}
    failures = []
    if set(roles) != {"service_planning", "consumer_protection"}:
        failures.append("approvals_missing")
    if len({entry["reviewer"] for entry in roles.values()}) != len(roles):
        failures.append("reviewers_not_independent")
    if any(not entry["approved"] for entry in roles.values()):
        failures.append("approval_denied")
    if document["risk_flags"]:
        failures.append("risk_flags_present")
    if any(area["open_severe"] for area in document["areas"]):
        failures.append("severe_case_open")
    return {"analysis_id": analysis_id, "revision": row["revision"], "digest": row["digest"],
            "approvals": [dict(entry) for entry in roles.values()],
            "failures": failures, "ready": not failures}


def approve(db: Connection, body) -> dict:
    row = selected_analysis(db, body.analysis_id)
    if body.analysis_digest != row["digest"]:
        raise conflict("analysis_changed", "复核对应的分析版本已经改变",
                       {"current_digest": row["digest"]})
    old = db.execute("SELECT * FROM approvals WHERE approval_id=?", (body.approval_id,)).fetchone()
    fingerprint = digest(body.model_dump(mode="json"))
    if old is not None:
        if old["digest"] != fingerprint:
            raise conflict("approval_changed", "审批编号已经绑定另一项决定")
        return dict(old)
    same = db.execute("SELECT reviewer FROM approvals WHERE analysis_id=? AND analysis_digest=? AND role=?",
                      (body.analysis_id, body.analysis_digest, body.role)).fetchone()
    if same:
        raise conflict("role_already_reviewed", "该职责已对这版分析作出复核")
    others = db.execute("SELECT reviewer FROM approvals WHERE analysis_id=? AND analysis_digest=?",
                        (body.analysis_id, body.analysis_digest)).fetchall()
    if any(value["reviewer"] == body.reviewer for value in others):
        raise conflict("reviewer_not_independent", "两项审批须由不同人员完成")
    db.execute("INSERT INTO approvals VALUES(?,?,?,?,?,?,?,?)",
               (body.approval_id, body.analysis_id, body.reviewer, body.role,
                int(body.approved), body.analysis_digest, body.rationale, fingerprint))
    return {**body.model_dump(mode="json"), "digest": fingerprint}


def publish(db: Connection, analysis_id: str, expected_revision: int) -> dict:
    row = selected_analysis(db, analysis_id)
    actual = db.execute("SELECT COALESCE(MAX(revision),0) FROM bulletin WHERE analysis_id=?",
                        (analysis_id,)).fetchone()[0]
    if expected_revision != actual:
        raise conflict("bulletin_revision", "公告版本已变化", {"current_revision": actual})
    review = approval_set(db, analysis_id, row["revision"])
    if not review["ready"]:
        raise conflict("publication_blocked", "分析未达到公告条件", {"failures": review["failures"]})
    document = json.loads(row["document"])
    edition = actual + 1
    created = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    payload = {"analysis_id": analysis_id, "analysis_revision": row["revision"],
               "analysis_digest": row["digest"], "edition": edition,
               "document": document, "approvals": review["approvals"], "published_at": created}
    payload["bulletin_digest"] = digest(payload)
    db.execute("INSERT INTO bulletin VALUES(?,?,?,?,?)",
               (analysis_id, edition, row["digest"], encode(payload), created))
    db.execute("UPDATE analysis SET state='published' WHERE analysis_id=? AND revision=?",
               (analysis_id, row["revision"]))
    return payload


def bulletin(db: Connection, analysis_id: str, edition: int | None = None) -> dict:
    if edition is None:
        edition = db.execute("SELECT MAX(revision) FROM bulletin WHERE analysis_id=?",
                             (analysis_id,)).fetchone()[0]
    if edition is None:
        raise missing("bulletin", analysis_id)
    row = db.execute("SELECT result FROM bulletin WHERE analysis_id=? AND revision=?",
                     (analysis_id, edition)).fetchone()
    if row is None:
        raise missing("bulletin_revision", f"{analysis_id}/{edition}")
    return json.loads(row["result"])


def compare(db: Connection, analysis_id: str, left: int, right: int) -> dict:
    first, second = bulletin(db, analysis_id, left), bulletin(db, analysis_id, right)
    return {"left": left, "right": right,
            "left_digest": first["bulletin_digest"], "right_digest": second["bulletin_digest"],
            "changed": first["document"] != second["document"]}
