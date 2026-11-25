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
    strata_reports = {}
    for region in regions:
        document = analysis.run(db, cut=selected_cut, as_of=as_of, region=region,
                                service_kind=service_kind, cohort=cohort, policy=policy)
        from silverwatch import strata as _strata
        sample_report = _strata.stratified_reach(
            db, region=region, as_of=as_of, cut=selected_cut,
            service_kind=service_kind, cohort=cohort,
            minimum_exposure=policy.minimum_exposure)
        strata_reports[region] = sample_report
        exposed = sum(row["exposed"] for row in document["areas"])
        nonresponse = sum(row["nonresponse"] for row in document["areas"])
        confirmed = sum(row["substantiated"] for row in document["areas"])
        pending = sum(row["pending"] for row in document["areas"])
        incomplete = sum(row["reconciliation_incomplete_batches"] for row in document["areas"])
        coverage_signature = _strata.coverage_signature(sample_report["strata"])
        results.append({"region": region, "exposed": exposed, "nonresponse": nonresponse,
                        "confirmed": confirmed, "pending": pending,
                        "coverage_bp": round(exposed * 10_000 / (exposed + nonresponse))
                            if exposed + nonresponse else None,
                        "confirmed_rate_bp": round(float(Fraction(confirmed, exposed) * 10_000), 2)
                            if exposed else None,
                        "rate_denominator": {"basis": "observed", "people": exposed,
                                             "explainable": exposed > 0},
                        "risk_area_count": len(document["risk_flags"]),
                        "unmatched_intake_batches": incomplete,
                        "supply": document["supply"],
                        "strata": sample_report["strata"],
                        "stratum_coverage": coverage_signature,
                        "coverage": coverage_signature,
                        "missing_strata": sample_report["missing_strata"],
                        "unallocated": sample_report["unallocated"],
                        "sample_summary": sample_report["summary"],
                        "disclosure_state": "observed" if exposed else "no_exposure"})
    coverage_changes = _regional_coverage_changes(regions, strata_reports)
    return {"cut": selected_cut, "as_of": as_of.isoformat(), "service_kind": service_kind,
            "cohort": cohort, "policy_id": policy.policy_id,
            "regions": results,
            "coverage_changes": coverage_changes,
            "stratified_coverage_changes": coverage_changes,
            "coverage_differences": coverage_changes,
            "coverage_variation": coverage_changes,
            "stratum_coverage_by_region": {region: strata_reports[region]["coverage"]
                                           for region in regions},
            "comparison_note": "Values are comparable only for the same time cut, policy, service and cohort; unknown exposure or a missing sample frame is not treated as zero risk. Stratified coverage differences are listed so a rate comparison is not read across unlike channel/age coverage."}


def _regional_coverage_changes(regions: list[str], reports: dict) -> list[dict]:
    """标出同一渠道×年龄层在不同地区之间覆盖状态/观察规模不一致的分层。"""
    signatures = {region: reports[region]["coverage"]["cells"] for region in regions}
    all_keys = sorted(set().union(*(cells.keys() for cells in signatures.values())))
    changes = []
    for key in all_keys:
        channel, cohort = key.split("|", 1)
        per_region = {region: _compact(signatures[region].get(key)) for region in regions}
        states = {value["coverage_status"] if value else "absent"
                  for value in per_region.values()}
        presence = {value["present"] if value else False for value in per_region.values()}
        observed = {value["observed_people"] if value else None for value in per_region.values()}
        if len(states) > 1 or len(presence) > 1 or len(observed) > 1:
            changes.append({"stratum": key, "channel": channel, "cohort": cohort,
                            "change": "regional_coverage_variation",
                            "regions": per_region})
    return changes


def _compact(cell: dict | None) -> dict | None:
    if cell is None:
        return None
    return {"present": cell["present"], "observed_people": cell.get("observed_people"),
            "coverage_status": cell.get("coverage_status"),
            "coverage_complete": cell.get("coverage_complete"),
            "frame_known": cell.get("frame_known")}


def rank_by_confirmed_rate(result: dict) -> list[dict]:
    values = result["regions"]
    comparable = [row for row in values if row["confirmed_rate_bp"] is not None]
    comparable.sort(key=lambda row: (-row["confirmed_rate_bp"], row["region"]))
    return [{"rank": index + 1, **row} for index, row in enumerate(comparable)]
