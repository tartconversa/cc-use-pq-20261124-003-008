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
    for key in keys:
        points = []
        previous = None
        for snapshot in snapshots:
            row = snapshot["areas"].get(key)
            if row is None:
                points.append({"period": snapshot["label"], "present": False,
                               "exposed": 0, "reports": 0, "substantiated": 0,
                               "pending": 0, "substantiated_rate_bp": None,
                               "coverage_complete": None, "rate_delta_bp": None})
                previous = None
                continue
            rate = _rate(row)
            prior_rate = _rate(previous) if previous else None
            delta = round(rate - prior_rate, 2) if rate is not None and prior_rate is not None else None
            points.append({"period": snapshot["label"], "present": True,
                           "exposed": row["exposed"], "nonresponse": row["nonresponse"],
                           "reports": row["reports"], "substantiated": row["substantiated"],
                           "pending": row["pending"], "inconclusive": row["inconclusive"],
                           "substantiated_rate_bp": rate,
                           "potential_rate_bounds": row["potential_rate_bounds"],
                           "coverage_complete": row["coverage_unknown_batches"] == 0,
                           "eligible": row["eligible"], "gaps": row["gaps"],
                           "rate_delta_bp": delta})
            previous = row
        series.append({"district": key[0], "service_kind": key[1],
                       "cohort": key[2], "points": points})
    return {"region": region, "service_kind": service_kind, "cohort": cohort,
            "policy_id": policy.policy_id,
            "periods": [{"label": point["label"], "cut": point["cut"],
                         "as_of": point["as_of"].isoformat()} for point in parsed],
            "series": series,
            "interpretation": "A change in the confirmed rate is shown alongside its denominator, pending cases and coverage; the change alone does not establish a cause."}
