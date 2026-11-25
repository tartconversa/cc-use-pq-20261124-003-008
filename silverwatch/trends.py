from __future__ import annotations

from datetime import datetime
from sqlite3 import Connection

from silverwatch import analysis
from silverwatch.contracts import AnalysisPolicy, ServiceKind
from silverwatch.errors import invalid
from silverwatch.ledger import current_cut


def _rate(row: dict) -> float | None:
    value = row["substantiated_rate"]
    return float(value.removesuffix("bp")) if value is not None else None


def _index(document: dict) -> dict:
    return {(row["district"], row["service_kind"], row["cohort"]): row
            for row in document["areas"]}


def build(db: Connection, *, region: str, periods: list[dict], policy: AnalysisPolicy,
          service_kind: ServiceKind | None = None,
          cohort: str | None = None) -> dict:
    if not 2 <= len(periods) <= 52:
        raise invalid("trend_periods", "趋势至少包含 2 期且最多包含 52 期")
    current = current_cut(db)
    parsed = []
    previous_cut = -1
    previous_time = None
    for index, value in enumerate(periods):
        if not isinstance(value, dict) or set(value) != {"label", "cut", "as_of"}:
            raise invalid("trend_period_shape", "每一期须含 label、cut 与 as_of", {"position": index})
        as_of = datetime.fromisoformat(value["as_of"])
        cut = value["cut"]
        if as_of.tzinfo is None or not isinstance(cut, int) or isinstance(cut, bool):
            raise invalid("trend_period_value", "趋势截点必须是带时区时间和整数序号", {"position": index})
        if cut < previous_cut or cut > current or previous_time and as_of <= previous_time:
            raise invalid("trend_order", "趋势时间必须递增，序号截点不得倒退或超前",
                          {"position": index, "previous_cut": previous_cut, "current_cut": cut})
        if not isinstance(value["label"], str) or not value["label"].strip():
            raise invalid("trend_label", "趋势期必须有非空名称", {"position": index})
        parsed.append({"label": value["label"].strip(), "cut": cut, "as_of": as_of})
        previous_cut, previous_time = cut, as_of
    snapshots = []
    for point in parsed:
        document = analysis.run(db, cut=point["cut"], as_of=point["as_of"], region=region,
                                service_kind=service_kind, cohort=cohort, policy=policy)
        snapshots.append({**point, "document": document, "areas": _index(document)})
    keys = sorted(set().union(*(item["areas"].keys() for item in snapshots)))
    series = []
    coverage_changes = []
    for key in keys:
        points = []
        previous = None
        previous_signature = None
        for period_index, snapshot in enumerate(snapshots):
            row = snapshot["areas"].get(key)
            if row is None:
                point = {"period": snapshot["label"], "present": False,
                         "exposed": None, "observed_people": None,
                         "nonresponse": None, "nonresponse_people": None,
                         "nonresponse_range": None, "sample_frame": None,
                         "sample_frame_range": None, "frame_known": None,
                         "coverage_status": "unobserved",
                         "reports": None, "substantiated": None,
                         "pending": None, "substantiated_rate_bp": None,
                         "verified_lead_rate_bp": None,
                         "estimation_basis": None, "basis_code": None,
                         "substantiated_rate": None,
                         "coverage_complete": None, "rate_delta_bp": None}
                points.append(point)
                signature = _point_signature(point)
                if previous_signature is not None:
                    change = _point_coverage_change(key, snapshot["label"], period_index,
                                                    previous_signature, signature)
                    if change:
                        coverage_changes.append(change)
                previous = None
                previous_signature = signature
                continue
            rate = _rate(row)
            prior_rate = _rate(previous) if previous else None
            delta = round(rate - prior_rate, 2) if rate is not None and prior_rate is not None else None
            point = {"period": snapshot["label"], "present": True,
                     "exposed": row["exposed"],
                     "observed_people": row.get("observed_people", row["exposed"]),
                     "nonresponse": row["nonresponse"],
                     "nonresponse_people": row.get("nonresponse_people", row["nonresponse"]),
                     "nonresponse_range": row.get("nonresponse_range"),
                     "sample_frame": row.get("sample_frame"),
                     "sample_frame_people": row.get("sample_frame_people"),
                     "sample_frame_range": row.get("sample_frame_range"),
                     "frame_known": row.get("frame_known"),
                     "coverage_status": row.get("coverage_status"),
                     "reports": row["reports"], "substantiated": row["substantiated"],
                     "verified_leads": row.get("verified_leads", row["substantiated"]),
                     "pending": row["pending"], "inconclusive": row["inconclusive"],
                     "substantiated_rate_bp": rate,
                     "verified_lead_rate_bp": row.get("verified_lead_rate_bp", rate),
                     "potential_rate_bounds": row["potential_rate_bounds"],
                     "supported_rate_bounds": row.get("supported_rate_bounds"),
                     "rate_denominator": row.get("rate_denominator"),
                     "estimation_basis": _code(row.get("estimation_basis")),
                     "basis_code": row.get("basis_code"),
                     "substantiated_rate": row["substantiated_rate"],
                     "coverage_complete": row["coverage_unknown_batches"] == 0,
                     "eligible": row["eligible"], "gaps": row["gaps"],
                     "rate_delta_bp": delta}
            points.append(point)
            signature = _point_signature(point)
            if previous_signature is not None:
                change = _point_coverage_change(key, snapshot["label"], period_index,
                                                previous_signature, signature)
                if change:
                    point["coverage_changed"] = True
                    point["coverage_change"] = change["change"]
                    point["coverage_shift"] = change["change"]
                    point["coverage_change_detail"] = change
                    coverage_changes.append(change)
                else:
                    point["coverage_changed"] = False
            previous = row
            previous_signature = signature
        series.append({"district": key[0], "service_kind": key[1],
                       "cohort": key[2], "channel": None, "points": points,
                       "coverage_changes": []})
    for item in coverage_changes:
        for one in series:
            if (one["district"], one["service_kind"], one["cohort"]) == \
                    (item["district"], item["service_kind"], item["cohort"]):
                one["coverage_changes"].append(item)
    return {"region": region, "service_kind": service_kind, "cohort": cohort,
            "policy_id": policy.policy_id,
            "periods": [{"label": point["label"], "cut": point["cut"],
                         "as_of": point["as_of"].isoformat()} for point in parsed],
            "series": series,
            "coverage_changes": coverage_changes,
            "stratified_coverage_changes": coverage_changes,
            "coverage_shifts": coverage_changes,
            "coverage_change_count": len(coverage_changes),
            "appeared_strata": [item for item in coverage_changes
                                if item["change"] == "stratum_appeared"],
            "disappeared_strata": [item for item in coverage_changes
                                   if item["change"] == "stratum_disappeared"],
            "interpretation": "A change in the confirmed rate is shown alongside its denominator, pending cases and coverage; stratified coverage changes are flagged because a rate shift can come from a different channel/age mix rather than a changing risk level."}


def _code(value):
    return str(value) if value is not None else None


def _point_signature(point: dict) -> dict:
    return {"present": point.get("present"),
            "observed_people": point.get("observed_people") if point.get("present") else None,
            "coverage_status": point.get("coverage_status"),
            "coverage_complete": point.get("coverage_complete"),
            "frame_known": point.get("frame_known"),
            "nonresponse_range": point.get("nonresponse_range")}


def _point_coverage_change(key, label: str, index: int,
                           before: dict, after: dict) -> dict | None:
    if before["present"] != after["present"]:
        kind = "stratum_appeared" if after["present"] else "stratum_disappeared"
    elif before.get("coverage_status") != after.get("coverage_status"):
        kind = "coverage_status_changed"
    elif before.get("frame_known") != after.get("frame_known"):
        kind = "frame_knowledge_changed"
    elif before.get("coverage_complete") != after.get("coverage_complete"):
        kind = "coverage_completeness_changed"
    elif (before.get("nonresponse_range") or {}).get("high") != \
            (after.get("nonresponse_range") or {}).get("high") or \
            (before.get("nonresponse_range") or {}).get("low") != \
            (after.get("nonresponse_range") or {}).get("low"):
        kind = "nonresponse_range_changed"
    elif before.get("observed_people") != after.get("observed_people"):
        kind = "observed_count_changed"
    else:
        return None
    return {"series_key": {"district": key[0], "service_kind": key[1], "cohort": key[2]},
            "district": key[0], "service_kind": key[1], "cohort": key[2],
            "period": label, "period_index": index, "change": kind,
            "before": before, "after": after}
