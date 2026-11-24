from __future__ import annotations

from datetime import datetime
from fractions import Fraction
from sqlite3 import Connection

from silverwatch import analysis
from silverwatch.contracts import AnalysisPolicy, ServiceKind
from silverwatch.errors import invalid
from silverwatch.ledger import current_cut


def compare_regions(db: Connection, *, regions: list[str], as_of: datetime,
                    policy: AnalysisPolicy, cut: int | None = None,
                    service_kind: ServiceKind | None = None,
                    cohort: str | None = None) -> dict:
    if as_of.tzinfo is None:
        raise invalid("comparison_time", "地区比较截点必须包含时区")
    if not 2 <= len(regions) <= 10 or len(set(regions)) != len(regions):
        raise invalid("comparison_regions", "地区比较需提供 2 到 10 个互不重复的地区")
    if any(region not in {"urban", "suburban", "rural"} for region in regions):
        raise invalid("comparison_region", "比较请求含未知地区")
    selected_cut = current_cut(db) if cut is None else cut
    if selected_cut > current_cut(db):
        raise invalid("comparison_cut", "地区比较截点尚未存在")
    results = []
    for region in regions:
        document = analysis.run(db, cut=selected_cut, as_of=as_of, region=region,
                                service_kind=service_kind, cohort=cohort, policy=policy)
        exposed = sum(row["exposed"] for row in document["areas"])
        nonresponse = sum(row["nonresponse"] for row in document["areas"])
        confirmed = sum(row["substantiated"] for row in document["areas"])
        pending = sum(row["pending"] for row in document["areas"])
        incomplete = sum(row["reconciliation_incomplete_batches"] for row in document["areas"])
        results.append({"region": region, "exposed": exposed, "nonresponse": nonresponse,
                        "confirmed": confirmed, "pending": pending,
                        "coverage_bp": round(exposed * 10_000 / (exposed + nonresponse))
                            if exposed + nonresponse else None,
                        "confirmed_rate_bp": round(float(Fraction(confirmed, exposed) * 10_000), 2)
                            if exposed else None,
                        "risk_area_count": len(document["risk_flags"]),
                        "unmatched_intake_batches": incomplete,
                        "supply": document["supply"],
                        "disclosure_state": "observed" if exposed else "no_exposure"})
    return {"cut": selected_cut, "as_of": as_of.isoformat(), "service_kind": service_kind,
            "cohort": cohort, "policy_id": policy.policy_id,
            "regions": results,
            "comparison_note": "Values are comparable only for the same time cut, policy, service and cohort; unknown exposure is not treated as zero risk."}


def rank_by_confirmed_rate(result: dict) -> list[dict]:
    values = result["regions"]
    comparable = [row for row in values if row["confirmed_rate_bp"] is not None]
    comparable.sort(key=lambda row: (-row["confirmed_rate_bp"], row["region"]))
    return [{"rank": index + 1, **row} for index, row in enumerate(comparable)]
