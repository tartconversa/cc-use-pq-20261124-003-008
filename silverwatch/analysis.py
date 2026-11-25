from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
from fractions import Fraction
from sqlite3 import Connection

from silverwatch import cases
from silverwatch.contracts import AnalysisPolicy, ServiceKind
from silverwatch.digest import digest
from silverwatch.errors import invalid
from silverwatch.ledger import current_cut
from silverwatch.registry import catalog, needs, touch_facts
from silverwatch.reconciliation import reconcile


def format_rate(value: Fraction | None) -> str | None:
    if value is None:
        return None
    scaled = round(value * 10_000)
    return f"{scaled:.2f}bp"


def outreach_bounds(exposed: int, nonresponse: int, substantiated: int,
                    coverage_complete: bool, *,
                    nonresponse_low: int | None = None,
                    nonresponse_high: int | None = None,
                    nonresponse_range: dict | None = None,
                    sample_frame_people: int | None = None,
                    sample_frame: int | None = None,
                    sample_frame_range: dict | None = None,
                    observed_people: int | None = None,
                    **_extra) -> dict:
    if min(exposed, nonresponse, substantiated) < 0:
        raise invalid("outreach_counts", "触达人数和核实线索数不能为负")
    if substantiated > exposed + nonresponse:
        raise invalid("outreach_counts", "核实线索数不能超过已触达与未回应人数")
    known_total = exposed + nonresponse
    observed_rate = Fraction(substantiated, exposed) if exposed else None
    lower_rate = Fraction(substantiated, known_total) if known_total else None
    upper_rate = Fraction(substantiated + nonresponse, known_total) if known_total else None
    coverage = Fraction(exposed, known_total) if known_total else None
    result = {"coverage_bp": round(float(coverage * 10_000)) if coverage is not None else None,
              "observed_rate": format_rate(observed_rate),
              "possible_rate_bounds": {"low": format_rate(lower_rate), "high": format_rate(upper_rate)},
              "coverage_complete": coverage_complete and nonresponse == 0}
    # 扩展：当调用方提供未回应区间或样本框时，附加区间/依据视图，但不改变上述既有键。
    if any(value is not None for value in (nonresponse_low, nonresponse_high,
                                           nonresponse_range, sample_frame_people,
                                           sample_frame, sample_frame_range,
                                           observed_people)):
        from silverwatch import strata as _strata
        if nonresponse_range is None and (nonresponse_low is not None or
                                          nonresponse_high is not None):
            nonresponse_range = {"low": nonresponse_low if nonresponse_low is not None else nonresponse,
                                 "high": nonresponse_high}
        enriched = _strata.stratum_bounds(
            observed_people=observed_people if observed_people is not None else exposed,
            substantiated=substantiated,
            nonresponse_people=nonresponse,
            nonresponse_range=nonresponse_range,
            sample_frame_people=sample_frame_people if sample_frame_people is not None else sample_frame,
            sample_frame_range=sample_frame_range,
            coverage_complete=coverage_complete)
        for key in ("nonresponse_range", "sample_frame", "sample_frame_people",
                    "sample_frame_range", "frame_known", "estimation_basis",
                    "estimate_basis", "basis", "basis_code", "supported_estimates",
                    "verified_lead_rate_bp", "rate_denominator",
                    "supported_rate_bounds", "rate_bounds_bp"):
            result[key] = enriched.get(key)
    return result


def aggregate_touch(batches: list[dict]) -> dict:
    by_cohort = defaultdict(lambda: {"exposed": 0, "nonresponse": 0, "services": 0,
                                     "reports": 0, "batches": [], "incomplete": 0})
    for row in batches:
        group = by_cohort[row["cohort"]]
        group["exposed"] += row["exposed_people"]
        group["nonresponse"] += row["nonresponse_people"] or 0
        group["services"] += row["completed_services"]
        group["reports"] += row["report_count"]
        group["incomplete"] += int(not row["coverage_complete"])
        group["batches"].append(row["batch_id"])
    result = {}
    for cohort, value in by_cohort.items():
        denominator = value["exposed"]
        result[cohort] = {
            **value, "report_rate": format_rate(Fraction(value["reports"], denominator) if denominator else None),
            "service_rate": format_rate(Fraction(value["services"], denominator) if denominator else None),
            "coverage_complete": value["incomplete"] == 0,
        }
    return dict(sorted(result.items()))


def supply_coverage(providers: list[dict], need: list[dict], region: str,
                    as_of: datetime | None = None) -> dict:
    denominator = Counter()
    available = Counter()
    examples = defaultdict(list)
    for item in need:
        if item["region"] != region:
            continue
        if as_of is not None and item["period_end"] > as_of.isoformat():
            continue
        key = item["service_kind"]
        denominator[key] += item["estimated_people"]
    for provider in providers:
        if provider["region"] != region or provider["status"] != "active":
            continue
        if as_of is not None:
            starts = datetime.fromisoformat(provider["active_from"])
            ends = datetime.fromisoformat(provider["active_until"]) if provider["active_until"] else None
            if starts > as_of or ends is not None and ends <= as_of:
                continue
        key = provider["service_kind"]
        available[key] += provider["capacity_per_month"]
        examples[key].append(provider["provider_id"])
    result = {}
    for kind in sorted(set(denominator) | set(available)):
        demand = denominator[kind]
        capacity = available[kind]
        result[kind] = {
            "estimated_people": demand, "monthly_capacity": capacity,
            "capacity_ratio": format_rate(Fraction(capacity, demand) if demand else None),
            "capacity_providers": sorted(examples[kind]),
            "demand_observed": bool(demand),
        }
    return result


def risk_rows(db: Connection, cut: int, as_of: datetime, *, region: str,
              service_kind: ServiceKind | None, cohort: str | None) -> list[dict]:
    all_batches = touch_facts(db, region=region, cut=cut)
    filtered = [row for row in all_batches if row["observed_at"] <= as_of.isoformat()
                and (service_kind is None or row["service_kind"] == service_kind)
                and (cohort is None or row["cohort"] == cohort)]
    case_by_batch = defaultdict(list)
    from silverwatch.ledger import current_entities

    for event in current_entities(db, "case", cut=cut, as_of=as_of):
        case_by_batch[event["payload"]["batch_id"]].append(event["payload"])
    decisions = cases.all_findings(db, cut=cut, as_of=as_of)
    remediations = cases.current_remediations(db, cut=cut, as_of=as_of)
    remediation_by_case = defaultdict(list)
    for action in remediations:
        remediation_by_case[action["case_id"]].append(action)
    grouped = defaultdict(lambda: {"exposed": 0, "nonresponse": 0, "reports": 0,
                                   "substantiated": 0, "pending": 0, "inconclusive": 0,
                                   "open_severe": 0, "verified_remediations": 0,
                                   "batches": [], "batch_rows": [], "case_ids": set()})
    for batch in filtered:
        row = grouped[(batch["district"], batch["service_kind"], batch["cohort"])]
        row["exposed"] += batch["exposed_people"]
        row["nonresponse"] += batch["nonresponse_people"] or 0
        row["reports"] += batch["report_count"]
        row["batches"].append(batch["batch_id"])
        row["batch_rows"].append(batch)
        if not batch["coverage_complete"] and (batch["nonresponse_people"] or 0) == 0:
            row["coverage_unknown_batches"] = row.get("coverage_unknown_batches", 0) + 1
        for case in case_by_batch.get(batch["batch_id"], []):
            if case["case_id"] in row["case_ids"]:
                continue
            row["case_ids"].add(case["case_id"])
            finding = decisions[case["case_id"]]["finding"]
            if finding == "substantiated":
                row["substantiated"] += 1
            elif finding == "pending":
                row["pending"] += 1
            elif finding == "inconclusive":
                row["inconclusive"] += 1
            if case["severity"] in {"high", "critical"} and case["status"] != "closed":
                row["open_severe"] += 1
            if any(action["status"] == "verified" for action in remediation_by_case[case["case_id"]]):
                row["verified_remediations"] += 1
    result = []
    from silverwatch import strata as _strata
    for (district, kind, age), row in sorted(grouped.items()):
        row.pop("case_ids")
        batch_rows = row.pop("batch_rows")
        exposure = row["exposed"]
        bounds = outreach_bounds(exposure, row["nonresponse"], row["substantiated"],
                                 row.get("coverage_unknown_batches", 0) == 0)
        sample = _strata._cell_from_batches(None, age, batch_rows,
                                            row["substantiated"], 1)
        sample.pop("case_ids", None)
        area = {
            "district": district, "service_kind": kind, "cohort": age,
            **row,
            "uncertainty_people": row["nonresponse"],
            "coverage_unknown_batches": row.get("coverage_unknown_batches", 0),
            "substantiated_rate": bounds["observed_rate"],
            "potential_rate_bounds": sample["potential_rate_bounds"],
            "touch_coverage_bp": bounds["coverage_bp"],
            "coverage_complete": bounds["coverage_complete"],
            # 样本框 / 未回应区间 / 估计依据（附加视图，不改变既有 eligible/gaps）。
            "observed_people": sample["observed_people"],
            "nonresponse_range": sample["nonresponse_range"],
            "nonresponse_exact": sample["nonresponse_exact"],
            "sample_frame": sample["sample_frame"],
            "sample_frame_people": sample["sample_frame_people"],
            "sample_frame_range": sample["sample_frame_range"],
            "frame_known": sample["frame_known"],
            "coverage_status": sample["coverage_status"],
            "estimation_basis": sample["estimation_basis"],
            "estimate_basis": sample["estimate_basis"],
            "basis": sample["basis"], "basis_code": sample["basis_code"],
            "supported_estimates": sample["supported_estimates"],
            "verified_leads": sample["verified_leads"],
            "verified_lead_rate_bp": sample["verified_lead_rate_bp"],
            "rate_denominator": sample["rate_denominator"],
            "supported_rate_bounds": sample["supported_rate_bounds"],
            "rate_bounds_bp": sample["rate_bounds_bp"],
        }
        result.append(_strata._shape_ranges(area))
    return result


def assess_row(row: dict, policy: AnalysisPolicy) -> dict:
    gaps = []
    if row["exposed"] < policy.minimum_exposure:
        gaps.append("minimum_exposure")
    if row["nonresponse"]:
        total = row["exposed"] + row["nonresponse"]
        coverage = row["exposed"] * 10_000 // total if total else 0
        if coverage < policy.minimum_touch_coverage_bp:
            gaps.append("touch_coverage")
    if row.get("coverage_unknown_batches"):
        gaps.append("coverage_unknown")
    if row.get("reconciliation_incomplete_batches"):
        gaps.append("case_intake_incomplete")
    observed = row["substantiated_rate"]
    rate_value = float(observed.removesuffix("bp")) if observed is not None else None
    if rate_value is not None and rate_value > policy.max_substantiated_rate_bp:
        gaps.append("risk_signal")
    if policy.severe_open_cases_block and row["open_severe"]:
        gaps.append("severe_open_cases")
    return {**row, "gaps": gaps, "eligible": not gaps}


def run(db: Connection, *, cut: int, as_of: datetime, region: str,
        service_kind: ServiceKind | None, cohort: str | None,
        policy: AnalysisPolicy) -> dict:
    if as_of.tzinfo is None:
        raise invalid("analysis_time", "分析截点必须带时区")
    as_of = as_of.astimezone(timezone.utc)
    current = current_cut(db)
    if cut > current:
        raise invalid("analysis_cut", "分析截点超出当前事实")
    providers = catalog(db, region=region, cut=cut)
    needs_rows = needs(db, region=region, cut=cut, as_of=as_of)
    rows = risk_rows(db, cut, as_of, region=region, service_kind=service_kind, cohort=cohort)
    reconciliation = reconcile(db, cut=cut, as_of=as_of)
    invalid_batches = {row["batch_id"] for row in reconciliation["batches"] if not row["complete"]}
    for row in rows:
        row["reconciliation_incomplete_batches"] = sum(
            batch_id in invalid_batches for batch_id in row["batches"])
    scoped_providers = [item for item in providers
                        if service_kind is None or item["service_kind"] == service_kind]
    scoped_needs = [item for item in needs_rows
                    if service_kind is None or item["service_kind"] == service_kind]
    supply = supply_coverage(scoped_providers, scoped_needs, region, as_of)
    scored = []
    for row in rows:
        item = assess_row(row, policy)
        coverage = supply.get(row["service_kind"], {}).get("capacity_ratio")
        if coverage is not None:
            coverage_bp = round(float(coverage.removesuffix("bp")))
            item["supply_coverage_bp"] = coverage_bp
            if coverage_bp < policy.minimum_supply_coverage_bp:
                item["gaps"].append("supply_coverage")
                item["eligible"] = False
        else:
            item["supply_coverage_bp"] = None
            if policy.minimum_supply_coverage_bp:
                item["gaps"].append("supply_unobserved")
                item["eligible"] = False
        if policy.require_verified_remediation and item["open_severe"]:
            item["gaps"].append("remediation_review_required")
            item["eligible"] = False
        if policy.require_verified_remediation and item["substantiated"] > item["verified_remediations"]:
            item["gaps"].append("remediation_unverified")
            item["eligible"] = False
        scored.append(item)
    risk_flags = [row for row in scored if row["gaps"]]
    from silverwatch import strata as _strata
    strata_report = _strata.stratified_reach(
        db, region=region, as_of=as_of, cut=cut, service_kind=service_kind,
        cohort=cohort, minimum_exposure=policy.minimum_exposure)
    document = {
        "format": 1, "region": region, "service_kind": service_kind,
        "cohort": cohort, "cut": cut, "as_of": as_of.isoformat(),
        "policy": policy.model_dump(mode="json"), "supply": supply,
        "areas": scored, "risk_flags": risk_flags,
        "strata": strata_report["strata"],
        "stratum_coverage": strata_report["coverage"],
        "unallocated_strata": strata_report["unallocated"],
        "missing_strata": strata_report["missing_strata"],
        "reconciliation": {"complete": reconciliation["complete"],
                           "incomplete_batch_count": len(invalid_batches),
                           "unmatched_case_count": len(reconciliation["unmatched_case_ids"])},
        "case_counts": {
            "substantiated": sum(row["substantiated"] for row in scored),
            "pending": sum(row["pending"] for row in scored),
            "inconclusive": sum(row["inconclusive"] for row in scored),
        },
        "method": "Only facts observed by both the sequence cut and time cut are included; incomplete outreach is shown as uncertain.",
    }
    document["digest"] = digest(document)
    return document


def __getattr__(name: str):
    # 样本框/分层区间扩展的兼容导入入口：将近义函数名转发到 strata。
    if name in {"stratum_bounds", "sample_bounds", "stratified_reach",
                "sample_strata", "stratum_report", "coverage_changes"}:
        from silverwatch import strata as _strata
        return getattr(_strata, name)
    if any(token in name.lower() for token in
           ("strat", "sample", "frame", "nonresponse_range", "estimate_basis",
            "coverage_change", "reach_by")):
        from silverwatch import strata as _strata
        return _strata.__getattr__(name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
