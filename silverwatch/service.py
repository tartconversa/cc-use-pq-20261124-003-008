from __future__ import annotations

from datetime import datetime, timezone
from sqlite3 import Connection

from pydantic import ValidationError

from silverwatch import analysis, publication
from silverwatch.contracts import AnalysisPolicy, Approval
from silverwatch.errors import Problem, invalid
from silverwatch.ledger import current_cut


def analyze(db: Connection, request: dict) -> dict:
    required = {"analysis_id", "region", "as_of", "policy"}
    if required - request.keys():
        raise invalid("analysis_request", "分析请求缺少必要字段",
                      {"missing": sorted(required - request.keys())})
    try:
        policy = AnalysisPolicy.model_validate(request["policy"])
        as_of = datetime.fromisoformat(request["as_of"])
        if as_of.tzinfo is None:
            raise ValueError("分析截点必须带时区")
    except (ValidationError, ValueError) as exc:
        raise invalid("analysis_request", "分析请求中的政策或时间无效") from exc
    cut = request.get("through_cut")
    if cut is None:
        cut = current_cut(db)
    document = analysis.run(db, cut=cut, as_of=as_of, region=request["region"],
                            service_kind=request.get("service_kind"),
                            cohort=request.get("cohort"), policy=policy)
    return publication.store_analysis(db, request["analysis_id"], cut, as_of, policy, document)


def review_status(db: Connection, analysis_id: str, revision: int | None = None) -> dict:
    row = publication.selected_analysis(db, analysis_id, revision)
    status = publication.approval_set(db, analysis_id, row["revision"])
    return {**status, "source_cut": row["cut"], "created_at": row["created_at"],
            "policy": __import__("json").loads(row["policy"])}


def approve(db: Connection, payload: dict) -> dict:
    try:
        model = Approval.model_validate(payload)
    except ValidationError as exc:
        raise invalid("approval_invalid", "审批内容不符合业务契约",
                      exc.errors(include_url=False)) from exc
    return publication.approve(db, model)


def publish(db: Connection, analysis_id: str, expected_edition: int) -> dict:
    row = publication.selected_analysis(db, analysis_id)
    review = review_status(db, analysis_id, row["revision"])
    if not review["ready"]:
        raise Problem(409, "publication_blocked", "复核未通过，不能生成公告",
                      {"failures": review["failures"], "analysis_revision": row["revision"]})
    result = publication.publish(db, analysis_id, expected_edition)
    result["published_at"] = result.get("published_at") or datetime.now(timezone.utc).isoformat()
    return result


def compare_publications(db: Connection, analysis_id: str, left: int, right: int) -> dict:
    first = publication.bulletin(db, analysis_id, left)
    second = publication.bulletin(db, analysis_id, right)
    comparison = publication.compare(db, analysis_id, left, right)
    return {**comparison,
            "changed_areas": _changed_rows(first["document"]["areas"],
                                           second["document"]["areas"]),
            "left_cut": first["document"]["cut"],
            "right_cut": second["document"]["cut"],
            "left_as_of": first["document"]["as_of"],
            "right_as_of": second["document"]["as_of"]}


def _changed_rows(left: list[dict], right: list[dict]) -> list[dict]:
    identity = lambda row: (row["district"], row["service_kind"], row["cohort"])
    before = {identity(row): row for row in left}
    after = {identity(row): row for row in right}
    result = []
    for key in sorted(set(before) | set(after)):
        old, new = before.get(key), after.get(key)
        if old == new:
            continue
        result.append({"district": key[0], "service_kind": key[1], "cohort": key[2],
                       "before": old, "after": new})
    return result
