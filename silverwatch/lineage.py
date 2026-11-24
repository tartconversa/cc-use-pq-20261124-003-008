from __future__ import annotations

from datetime import datetime
from sqlite3 import Connection

from silverwatch.errors import missing
from silverwatch.ledger import current_cut, history, revisions


def explain_event(db: Connection, event_type: str, entity_id: str,
                  event_seq: int | None = None) -> dict:
    rows = history(db, event_type, entity_id)
    if event_seq is not None:
        rows = [row for row in rows if row["seq"] == event_seq]
    if not rows:
        raise missing(event_type, entity_id)
    return {"event_type": event_type, "entity_id": entity_id,
            "selected_event": rows[-1], "revision_history": rows,
            "current_cut": current_cut(db)}


def analysis_inputs(db: Connection, analysis_id: str, revision: int | None = None) -> dict:
    if revision is None:
        row = db.execute("SELECT * FROM analysis WHERE analysis_id=? ORDER BY revision DESC LIMIT 1",
                         (analysis_id,)).fetchone()
    else:
        row = db.execute("SELECT * FROM analysis WHERE analysis_id=? AND revision=?",
                         (analysis_id, revision)).fetchone()
    if row is None:
        raise missing("analysis", analysis_id)
    cut = row["cut"]
    as_of = datetime.fromisoformat(row["as_of"])
    snapshot = db.execute("""SELECT event_type,COUNT(*) AS count,MAX(seq) AS last_seq
        FROM event WHERE seq<=? AND observed_at<=? GROUP BY event_type ORDER BY event_type""",
                          (cut, row["as_of"])).fetchall()
    approvals = db.execute("SELECT role,reviewer,digest FROM approvals WHERE analysis_id=? AND analysis_digest=? ORDER BY role",
                            (analysis_id, row["digest"])).fetchall()
    return {"analysis_id": analysis_id, "revision": row["revision"], "cut": cut,
            "as_of": as_of.isoformat(), "captured_event_counts": [dict(item) for item in snapshot],
            "source_revision_examples": {kind: revisions(db, kind, entity_id, cut=cut)
                for kind, entity_id in _representative_entities(db, cut)},
            "approval_roles": [dict(item) for item in approvals],
            "analysis_digest": row["digest"], "state": row["state"]}


def _representative_entities(db: Connection, cut: int) -> list[tuple[str, str]]:
    rows = db.execute("""SELECT event_type,entity_id,MAX(seq) AS seq FROM event
        WHERE seq<=? GROUP BY event_type,entity_id ORDER BY event_type,entity_id LIMIT 12""",
                      (cut,)).fetchall()
    return [(row["event_type"], row["entity_id"]) for row in rows]


def verify_bulletin(db: Connection, analysis_id: str, edition: int) -> dict:
    row = db.execute("SELECT analysis_digest,result,created_at FROM bulletin WHERE analysis_id=? AND revision=?",
                     (analysis_id, edition)).fetchone()
    if row is None:
        raise missing("bulletin_revision", f"{analysis_id}/{edition}")
    import json
    from silverwatch.digest import digest

    document = json.loads(row["result"])
    saved = document.get("bulletin_digest")
    payload = {key: value for key, value in document.items() if key != "bulletin_digest"}
    recalculated = digest(payload)
    return {"analysis_id": analysis_id, "edition": edition,
            "valid": saved == recalculated and row["analysis_digest"] == document["analysis_digest"],
            "saved_digest": saved, "calculated_digest": recalculated,
            "analysis_digest": row["analysis_digest"], "created_at": row["created_at"]}


def verify_analysis(db: Connection, analysis_id: str, revision: int | None = None) -> dict:
    if revision is None:
        row = db.execute("SELECT * FROM analysis WHERE analysis_id=? ORDER BY revision DESC LIMIT 1",
                         (analysis_id,)).fetchone()
    else:
        row = db.execute("SELECT * FROM analysis WHERE analysis_id=? AND revision=?",
                         (analysis_id, revision)).fetchone()
    if row is None:
        raise missing("analysis", analysis_id)
    import json
    from silverwatch.digest import digest
    from silverwatch.ledger import audit_integrity, current_cut

    document = json.loads(row["document"])
    internal = document.get("digest")
    without_internal = {key: value for key, value in document.items() if key != "digest"}
    calculated_internal = digest(without_internal)
    calculated_storage = digest(document)
    ledger_check = audit_integrity(db, through_cut=row["cut"])
    flags = {"document_digest_valid": internal == calculated_internal,
             "stored_digest_valid": row["digest"] == calculated_storage,
             "source_cut_exists": row["cut"] <= current_cut(db),
             "source_ledger_valid": ledger_check["valid"]}
    return {"analysis_id": analysis_id, "revision": row["revision"],
            "valid": all(flags.values()), "checks": flags,
            "internal_digest": internal, "calculated_internal_digest": calculated_internal,
            "stored_digest": row["digest"], "calculated_stored_digest": calculated_storage,
            "source_cut": row["cut"], "source_ledger_issues": ledger_check["issues"]}
