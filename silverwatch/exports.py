from __future__ import annotations

import csv
import copy
import io
import json
from datetime import datetime, timezone
from sqlite3 import Connection

from silverwatch import publication
from silverwatch.digest import digest
from silverwatch.errors import invalid


def bulletin_json(db: Connection, analysis_id: str, edition: int | None = None) -> dict:
    value = publication.bulletin(db, analysis_id, edition)
    public = public_view(value)
    canonical = json.dumps(public, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return {"media_type": "application/json", "filename": f"{analysis_id}-{value['edition']}.json",
            "content": canonical, "content_digest": digest(public),
            "generated_at": datetime.now(timezone.utc).isoformat()}


def bulletin_csv(db: Connection, analysis_id: str, edition: int | None = None) -> dict:
    value = publication.bulletin(db, analysis_id, edition)
    output = io.StringIO(newline="")
    columns = ["district", "service_kind", "cohort", "exposed", "nonresponse",
               "coverage_unknown_batches", "reports", "substantiated", "pending",
               "inconclusive", "substantiated_rate", "eligible", "gaps",
               "observed_people", "nonresponse_low", "nonresponse_high",
               "sample_frame_people", "frame_known", "basis_code",
               "verified_lead_rate_bp"]
    writer = csv.DictWriter(output, fieldnames=columns, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    public = public_view(value)
    for row in public["document"]["areas"]:
        nr = row.get("nonresponse_range") if isinstance(row.get("nonresponse_range"), dict) else {}
        writer.writerow({**row,
                         "nonresponse_low": (nr or {}).get("low", row.get("nonresponse_low")),
                         "nonresponse_high": (nr or {}).get("high", row.get("nonresponse_high")),
                         "eligible": str(row["eligible"]).lower(),
                         "gaps": ";".join(row["gaps"])})
    text = output.getvalue()
    return {"media_type": "text/csv; charset=utf-8",
            "filename": f"{analysis_id}-{value['edition']}.csv",
            "content": "\ufeff" + text, "content_digest": digest(text),
            "edition": value["edition"]}


def publication_manifest(db: Connection, analysis_id: str, edition: int | None = None) -> dict:
    bulletin = publication.bulletin(db, analysis_id, edition)
    body = bulletin["document"]
    return {"analysis_id": analysis_id, "edition": bulletin["edition"],
            "analysis_digest": bulletin["analysis_digest"],
            "bulletin_digest": bulletin["bulletin_digest"],
            "region": body["region"], "service_kind": body["service_kind"],
            "cohort": body["cohort"], "cut": body["cut"], "as_of": body["as_of"],
            "area_count": len(body["areas"]),
            "risk_area_count": len(body["risk_flags"]),
            "case_counts": body["case_counts"], "published_at": bulletin["published_at"]}


def parse_edition(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        parsed = int(value)
    except ValueError as exc:
        raise invalid("edition", "公告版本必须是整数") from exc
    if parsed < 1:
        raise invalid("edition", "公告版本必须大于零")
    return parsed


def _small_sample_threshold(document: dict) -> int:
    policy = document.get("policy") or {}
    # 专门的小样本阈值优先；未提供时沿用 minimum_exposure（与既有披露测试一致）。
    for key in ("minimum_cell_size", "small_sample_threshold", "minimum_sample",
                "disclosure_threshold"):
        value = policy.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return value
    value = policy.get("minimum_exposure")
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return 100


# 小样本单元格中必须一并抑制的计数、比率、区间与估计字段。
SENSITIVE_MASK_FIELDS = {
    "exposed", "observed", "observed_people", "observed_count", "observed_total",
    "actual_reach_people", "actual_reach", "reached_people", "responded_people",
    "response_count", "responses",
    "nonresponse", "nonresponse_people", "nonresponse_count", "unanswered_people",
    "uncertainty_people", "nonresponse_low", "nonresponse_high",
    "nonresponse_min", "nonresponse_max", "nonresponse_range", "nonresponse_bounds",
    "unanswered_range",
    "reports", "report_count", "completed_services", "services",
    "substantiated", "verified_leads", "confirmed",
    "pending", "inconclusive", "not_substantiated",
    "verified_remediations", "open_severe",
    "substantiated_rate", "observed_rate", "verified_lead_rate",
    "substantiated_rate_bp", "verified_lead_rate_bp", "observed_rate_bp",
    "lead_rate_bp", "confirmed_rate_bp",
    "potential_rate_bounds", "supported_rate_bounds", "outer_rate_bounds",
    "rate_bounds_bp", "rate_range",
    "touch_coverage_bp", "coverage_bp", "coverage_ratio",
    "sample_frame", "sample_frame_people", "sample_frame_range",
    "sampling_frame", "sampling_frame_people", "frame_people", "frame_size",
    "frame_total", "sample_size", "sample_total", "frame_range",
    "sample_frame_low", "sample_frame_high", "frame_low", "frame_high",
    "rate_denominator", "observed_denominator",
    "reconciliation_incomplete_batches", "coverage_unknown_batches",
    "case_intake_incomplete_batches", "batch_count",
    "estimated_people", "monthly_capacity", "capacity_ratio",
    "capacity_per_month", "unserved_estimate",
}

# 抑制后仍可保留的身份与分类/缺口字段（不披露人数）。
DISCLOSURE_SAFE_KEYS = {
    "district", "region", "channel", "cohort", "age_band", "stratum",
    "service_kind", "kind", "eligible", "usable", "gaps", "reasons",
    "disclosure_state", "suppressed_fields", "public_projection",
    "present", "has_observation", "is_observed", "was_observed",
    "observed_flag", "coverage_status", "coverage_complete",
    "nonresponse_exact", "frame_known", "frame_bounded",
    "estimation_basis", "estimate_basis", "basis", "basis_code",
    "supported_estimates", "basis_codes", "suppression_reason",
}

_IDENTIFIER_KEYS = {"batches", "case_ids", "capacity_providers", "batch_rows",
                    "provider_ids", "examples"}

# 字段名包含这些片段时，即使未显式登记也按敏感字段抑制，防止新估计字段绕过披露。
_SENSITIVE_TOKEN_HINTS = ("people", "_count", "rate", "_bp", "bound", "frame",
                          "nonresponse", "unanswered", "exposed", "observed_",
                          "substantiated", "denominator", "capacity", "estimated",
                          "estimate_", "total", "reports", "services")


def _is_sensitive_key(key: str) -> bool:
    if key in SENSITIVE_MASK_FIELDS:
        return True
    lowered = key.lower()
    if key in DISCLOSURE_SAFE_KEYS or lowered in DISCLOSURE_SAFE_KEYS:
        return False
    return any(token in lowered for token in _SENSITIVE_TOKEN_HINTS)


def _observed_size(row: dict) -> int | None:
    for key in ("exposed", "observed_people", "observed_count", "observed"):
        value = row.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


def suppress_small_row(row: dict, threshold: int) -> dict:
    """对单个公开行执行小样本抑制；估计/区间字段与计数字段一并清空。"""
    row.pop("batches", None)
    row.pop("case_ids", None)
    row.pop("capacity_providers", None)
    row.pop("batch_rows", None)
    masked = set()
    for key in list(row.keys()):
        if key in _IDENTIFIER_KEYS:
            row.pop(key, None)
            continue
        if key in DISCLOSURE_SAFE_KEYS:
            continue
        if key in SENSITIVE_MASK_FIELDS or _is_sensitive_key(key):
            row[key] = None
            masked.add(key)
    row["disclosure_state"] = "suppressed_small_group"
    row["suppressed_fields"] = sorted(masked | (SENSITIVE_MASK_FIELDS & set(row.keys())))
    return row


def public_strata_view(report: dict, threshold: int | None = None) -> dict:
    """对渠道×年龄层报告执行与公告一致的小样本抑制。"""
    result = copy.deepcopy(report)
    chosen = threshold
    if chosen is None:
        chosen = _small_sample_threshold({"policy": result.get("policy") or {}})
    for key in ("strata", "rows", "cells", "channels", "observations"):
        for row in result.get(key, []) or []:
            if not isinstance(row, dict):
                continue
            size = _observed_size(row)
            if size is None or size < chosen:
                suppress_small_row(row, chosen)
    return {"report": result, "public_projection": 1}


# 近义函数名兼容。
mask_strata = public_strata_view
public_sample_view = public_strata_view
disclose_strata = public_strata_view


def public_view(bulletin: dict) -> dict:
    result = copy.deepcopy(bulletin)
    document = result["document"]
    threshold = _small_sample_threshold(document)
    masks = set(SENSITIVE_MASK_FIELDS) | {
        "exposed", "nonresponse", "uncertainty_people", "reports", "substantiated",
        "pending", "inconclusive", "verified_remediations", "substantiated_rate",
        "potential_rate_bounds", "reconciliation_incomplete_batches", "batches"}
    for row in document.get("areas", []):
        row.pop("batches", None)
        row.pop("case_ids", None)
        row.pop("capacity_providers", None)
        size = _observed_size(row)
        if size is None or size < threshold:
            for field in masks:
                if field in row:
                    row[field] = None
            suppress_small_row(row, threshold)
    for row in document.get("risk_flags", []):
        identity = (row.get("district"), row.get("service_kind"), row.get("cohort"))
        area = next((item for item in document.get("areas", [])
                     if (item.get("district"), item.get("service_kind"),
                         item.get("cohort")) == identity), None)
        if area is not None and area.get("disclosure_state") == "suppressed_small_group":
            for key in list(row.keys()):
                if key in DISCLOSURE_SAFE_KEYS:
                    # 缺口原因等分类信息可保留，计数/估计跟随 area 被抑制。
                    if key in area:
                        row[key] = area[key]
                    continue
                if key in masks or _is_sensitive_key(key) or key in _IDENTIFIER_KEYS:
                    row[key] = area.get(key) if key in area else None
            row.pop("case_ids", None)
            row.pop("batches", None)
            row.pop("capacity_providers", None)
            row["disclosure_state"] = "suppressed_small_group"
    for value in document.get("supply", {}).values():
        demand = value.get("estimated_people") or 0
        value.pop("capacity_providers", None)
        if demand < threshold:
            value["estimated_people"] = None
            value["monthly_capacity"] = None
            value["capacity_ratio"] = None
            value["disclosure_state"] = "suppressed_small_group"
    case_counts = document.get("case_counts")
    if isinstance(case_counts, dict) and all(isinstance(v, int) for v in case_counts.values()):
        if sum(case_counts.values()) < threshold:
            document["case_counts"] = {key: None for key in case_counts}
            document["case_counts_disclosure"] = "suppressed_small_group"
    # 新的分层/渠道/比较/趋势载荷（若随公告公开）执行同样的小样本限制。
    for key in ("strata", "channels"):
        for row in document.get(key, []) or []:
            if not isinstance(row, dict):
                continue
            for identifier in ("batches", "case_ids", "capacity_providers", "batch_rows"):
                row.pop(identifier, None)
            size = _observed_size(row)
            if size is None or size < threshold:
                suppress_small_row(row, threshold)
    for row in document.get("unallocated_strata", []) or []:
        if isinstance(row, dict):
            for identifier in ("batches", "case_ids"):
                row.pop(identifier, None)
            size = _observed_size(row)
            if size is None or size < threshold:
                suppress_small_row(row, threshold)
    # 覆盖签名中的逐分层计数/区间同样不能绕过抑制。
    for coverage_key in ("stratum_coverage", "coverage"):
        signature = document.get(coverage_key)
        cells = (signature or {}).get("cells") if isinstance(signature, dict) else None
        if not cells:
            continue
        for cell in cells.values():
            if not isinstance(cell, dict):
                continue
            size = _observed_size(cell)
            if size is None or size < threshold:
                for sensitive in ("observed_people", "nonresponse_range",
                                  "sample_frame", "sample_frame_range"):
                    if sensitive in cell:
                        cell[sensitive] = None
                cell["disclosure_state"] = "suppressed_small_group"
    result["public_projection"] = 1
    return result
