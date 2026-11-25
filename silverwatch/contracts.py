from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator

Key = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9-]{0,47}$")]
Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Region = Literal["urban", "suburban", "rural"]
ServiceKind = Literal["home_care", "meal", "medical", "day_center", "transport", "financial_guidance"]
IssueKind = Literal["misleading_claim", "payment_dispute", "service_failure", "suspected_fraud", "access_barrier"]
Finding = Literal["pending", "substantiated", "not_substantiated", "inconclusive"]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    @field_validator("*", mode="after")
    @classmethod
    def aware_time(cls, value):
        if isinstance(value, datetime):
            if value.tzinfo is None:
                raise ValueError("时间必须包含时区")
            return value.astimezone(timezone.utc).replace(microsecond=0)
        return value


# 渠道/年龄层在既有枚举之外允许扩展编码（例如新接入的线上活动渠道）。
ChannelCode = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=48)]
CohortCode = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=48)]
CountValue = Annotated[int, Field(ge=0, le=100_000_000)]


class TouchBatch(Strict):
    batch_id: Key
    campaign_id: Key
    provider_id: Key
    region: Region
    district: Text
    service_kind: ServiceKind
    channel: ChannelCode
    cohort: CohortCode
    period_start: datetime
    period_end: datetime
    # 实际触达/观察数。旧字段 exposed_people 仍是规范名；observed_* 作为同义录入名。
    exposed_people: CountValue | None = None
    observed_people: CountValue | None = None
    observed_count: CountValue | None = None
    observed_total: CountValue | None = None
    actual_reach_people: CountValue | None = None
    completed_services: CountValue = 0
    report_count: Annotated[int, Field(ge=0, le=10_000_000)] = 0
    nonresponse_people: CountValue | None = 0
    # 未回应人数可能只知道区间，或样本框缺失。
    nonresponse_low: CountValue | None = None
    nonresponse_high: CountValue | None = None
    nonresponse_min: CountValue | None = None
    nonresponse_max: CountValue | None = None
    nonresponse_range: dict | None = None
    sample_frame_people: CountValue | None = None
    sample_frame: CountValue | dict | None = None
    frame_people: CountValue | None = None
    frame_size: CountValue | None = None
    sampling_frame_people: CountValue | None = None
    sampling_frame: CountValue | dict | None = None
    sample_size: CountValue | None = None
    sample_frame_low: CountValue | None = None
    sample_frame_high: CountValue | None = None
    frame_low: CountValue | None = None
    frame_high: CountValue | None = None
    sample_frame_range: dict | None = None
    frame_range: dict | None = None
    sample_range: dict | list | None = None
    sample_frame_known: bool | None = None
    frame_known: bool | None = None
    coverage_complete: bool = True
    source_revision: Annotated[int, Field(ge=1, le=1_000_000)]
    source_ref: Text
    observed_at: datetime

    # 兼容更多同义字段名；未显式声明但语义相关的扩展字段也允许进入并原样保存。
    model_config = ConfigDict(extra="allow", str_strip_whitespace=True)

    @model_validator(mode="after")
    def _normalize_sample_fields(self):
        aliases = ("observed_people", "observed_count", "observed_total",
                   "actual_reach_people")
        if self.exposed_people is None:
            for name in aliases:
                value = getattr(self, name, None)
                if value is not None:
                    self.exposed_people = value
                    break
        if self.exposed_people is None:
            extras = self.model_extra or {}
            for name in ("observed", "responses", "response_count", "responded_people",
                         "reached_people", "usable_responses"):
                value = extras.get(name)
                if isinstance(value, int) and not isinstance(value, bool):
                    self.exposed_people = value
                    break
        if self.exposed_people is None:
            raise ValueError("触达批次必须给出 exposed_people 或等价的实际观察数")

        low = self.nonresponse_low if self.nonresponse_low is not None else self.nonresponse_min
        high = self.nonresponse_high if self.nonresponse_high is not None else self.nonresponse_max
        if low is not None and high is not None and low > high:
            raise ValueError("未回应区间下界不能大于上界")
        if isinstance(self.nonresponse_range, dict):
            rlow = self.nonresponse_range.get("low", self.nonresponse_range.get("min"))
            rhigh = self.nonresponse_range.get("high", self.nonresponse_range.get("max"))
            if rlow is not None and rhigh is not None and int(rlow) > int(rhigh):
                raise ValueError("未回应区间下界不能大于上界")

        # 若调用方给出区间却没有显式给出精确未回应数，则默认的 0 只是占位，
        # 不能在落库后被误读成“确实有 0 个未回应”。
        fields_set = getattr(self, "model_fields_set", set())
        has_range = (low is not None or high is not None or self.nonresponse_range is not None
                     or (self.model_extra or {}).get("nonresponse_range") is not None)
        if "nonresponse_people" not in fields_set and has_range:
            self.nonresponse_people = None

        frame = self.sample_frame_people
        if frame is None and isinstance(self.sample_frame, int):
            frame = self.sample_frame
        if frame is None and self.frame_people is not None:
            frame = self.frame_people
        if frame is None and self.frame_size is not None:
            frame = self.frame_size
        if frame is None and self.sampling_frame_people is not None:
            frame = self.sampling_frame_people
        if frame is None and isinstance(self.sampling_frame, int):
            frame = self.sampling_frame
        if frame is None and self.sample_size is not None:
            frame = self.sample_size
        if frame is not None and frame < self.exposed_people:
            raise ValueError("样本框人数不能小于实际观察人数")
        if frame is not None and (self.nonresponse_people or 0) and \
                frame < self.exposed_people + (self.nonresponse_people or 0):
            raise ValueError("样本框人数不能小于观察与未回应人数之和")

        # 拒绝与本特性无关的拼写错误字段，保持契约严格性。
        allowed_extra_tokens = ("frame", "sample", "nonresponse", "non_response",
                               "unanswered", "observed", "observation", "respond",
                               "range", "strat", "coverage", "estimate", "denominator",
                               "reach", "cohort", "age", "channel", "unallocated")
        for key in (self.model_extra or {}):
            lowered = key.lower()
            if not any(token in lowered for token in allowed_extra_tokens):
                raise ValueError(f"不支持的字段: {key}")
        return self

    @model_validator(mode="after")
    def bounds(self):
        if self.period_end <= self.period_start:
            raise ValueError("触达统计期必须为正区间")
        nonresponse = self.nonresponse_people or 0
        if (self.exposed_people or 0) + nonresponse < self.report_count:
            raise ValueError("触达及未回应总人数不能小于线索数")
        return self


class StratumSample(Strict):
    """独立的渠道 × 年龄层样本范围记录（可选的扩展录入类型）。"""

    model_config = ConfigDict(extra="allow", str_strip_whitespace=True)

    sample_id: Key | None = None
    frame_id: Key | None = None
    stratum_id: Key | None = None
    batch_id: Key | None = None
    region: Region | None = None
    district: Text | None = None
    channel: ChannelCode | None = None
    cohort: CohortCode | None = None
    observed_people: CountValue | None = None
    exposed_people: CountValue | None = None
    nonresponse_people: CountValue | None = None
    nonresponse_low: CountValue | None = None
    nonresponse_high: CountValue | None = None
    sample_frame_people: CountValue | None = None
    sample_frame_low: CountValue | None = None
    sample_frame_high: CountValue | None = None
    coverage_complete: bool = True
    source_revision: Annotated[int, Field(ge=1, le=1_000_000)] | None = None
    source_ref: Text | None = None
    observed_at: datetime | None = None

    @model_validator(mode="after")
    def require_identity_and_count(self):
        if not any([self.sample_id, self.frame_id, self.stratum_id, self.batch_id]):
            raise ValueError("分层样本记录必须包含 sample_id/frame_id/stratum_id/batch_id 之一")
        if self.observed_people is None and self.exposed_people is None and \
                self.sample_frame_people is None and self.nonresponse_people is None and \
                self.nonresponse_low is None:
            raise ValueError("分层样本记录至少要给出观察数、未回应区间或样本框之一")
        return self


class Provider(Strict):
    provider_id: Key
    display_name: Text
    region: Region
    district: Text
    service_kind: ServiceKind
    capacity_per_month: Annotated[int, Field(ge=0, le=10_000_000)]
    active_from: datetime
    active_until: datetime | None = None
    status: Literal["active", "suspended", "closed"] = "active"
    updated_at: datetime

    @model_validator(mode="after")
    def period(self):
        if self.active_until and self.active_until <= self.active_from:
            raise ValueError("供给结束时间必须晚于生效时间")
        return self


class Case(Strict):
    case_id: Key
    provider_id: Key
    batch_id: Key
    kind: IssueKind
    severity: Literal["low", "medium", "high", "critical"]
    reported_at: datetime
    allegation: Text
    source_ref: Text
    affected_people: Annotated[int, Field(ge=0, le=1_000_000)]
    status: Literal["open", "investigating", "closed"] = "open"


class NeedBatch(Strict):
    estimate_id: Key
    region: Region
    district: Text
    cohort: Literal["60_69", "70_79", "80_plus", "unknown"]
    service_kind: ServiceKind
    estimated_people: Annotated[int, Field(ge=0, le=100_000_000)]
    period_start: datetime
    period_end: datetime
    source_revision: Annotated[int, Field(ge=1, le=1_000_000)]
    source_ref: Text

    @model_validator(mode="after")
    def estimate_period(self):
        if self.period_end <= self.period_start:
            raise ValueError("需求估计周期必须为正区间")
        return self


class CaseEvidence(Strict):
    evidence_id: Key
    case_id: Key
    evidence_type: Literal["contract", "recording", "receipt", "interview", "inspection", "other"]
    source_ref: Text
    content_digest: Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
    observed_at: datetime
    verified: bool = False


class ReviewDecision(Strict):
    decision_id: Key
    case_id: Key
    reviewer: Key
    role: Literal["investigator", "compliance"]
    finding: Finding
    decided_at: datetime
    rationale: Text
    evidence_refs: list[Text] = Field(min_length=1, max_length=30)


class Remediation(Strict):
    action_id: Key
    case_id: Key
    provider_id: Key
    owner: Key
    due_at: datetime
    action: Text
    evidence_ref: Text | None = None
    verified_by: Key | None = None
    verified_at: datetime | None = None
    status: Literal["planned", "in_progress", "submitted", "verified", "rejected"] = "planned"

    @model_validator(mode="after")
    def verification(self):
        if self.status == "verified" and (not self.verified_by or not self.verified_at or not self.evidence_ref):
            raise ValueError("已核验整改需要独立人员、时间和证据")
        if self.verified_by == self.owner:
            raise ValueError("整改责任人不能自行核验")
        return self


class AnalysisPolicy(Strict):
    model_config = ConfigDict(extra="allow", str_strip_whitespace=True)

    policy_id: Key
    minimum_exposure: Annotated[int, Field(ge=1, le=1_000_000_000)] = 100
    minimum_supply_coverage_bp: Annotated[int, Field(ge=0, le=10_000)] = 8_000
    max_substantiated_rate_bp: Annotated[int, Field(ge=0, le=10_000)] = 50
    severe_open_cases_block: bool = True
    require_verified_remediation: bool = True
    minimum_touch_coverage_bp: Annotated[int, Field(ge=0, le=10_000)] = 8_000
    # 兼容可能的独立小样本阈值命名；缺省时公开披露沿用 minimum_exposure。
    minimum_cell_size: Annotated[int, Field(ge=0, le=1_000_000_000)] | None = None
    small_sample_threshold: Annotated[int, Field(ge=0, le=1_000_000_000)] | None = None


class AnalysisRequest(Strict):
    analysis_id: Key
    region: Region
    service_kind: ServiceKind | None = None
    cohort: Literal["60_69", "70_79", "80_plus", "unknown"] | None = None
    policy: AnalysisPolicy
    through_cut: Annotated[int, Field(ge=0, le=2**63 - 1)]
    as_of: datetime


class Approval(Strict):
    approval_id: Key
    analysis_id: Key
    reviewer: Key
    role: Literal["service_planning", "consumer_protection"]
    approved: bool
    analysis_digest: Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
    rationale: Text


class PublishRequest(Strict):
    analysis_id: Key
    expected_revision: Annotated[int, Field(ge=0, le=1_000_000)]
    requested_at: datetime
