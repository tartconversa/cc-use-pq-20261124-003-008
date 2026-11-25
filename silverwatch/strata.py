"""渠道 × 年龄层样本范围与未回应区间分析。

本模块在既有触达事实之上补充三件事：

1. 保留每个分层的**实际观察数**，缺失分层显示为未知（``None``）而不是零；
2. 当未回应人数或样本框只知道区间时，传播为区间，不伪造精确点值；
3. 明确每个分层“可支持的估计依据”（观察分母比率 / 闭区间界 / 仅观察值 / 不可估计）。

任何聚合都不会把年龄层 ``unknown`` 或来源未分摊的未回应人数平均分配到其他组。
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from fractions import Fraction
from sqlite3 import Connection
from typing import Any

from silverwatch.analysis import format_rate
from silverwatch.errors import invalid

# 契约中已经存在的渠道与年龄层取值；顺序仅用于稳定输出。
CHANNEL_ORDER = ("community", "phone", "web", "in_person")
COHORT_ORDER = ("60_69", "70_79", "80_plus", "unknown")
KNOWN_REGIONS = ("urban", "suburban", "rural")

# 各类输入命名的兼容集合（数据录入扩展可能采用不同字段名）。
_OBSERVED_KEYS = ("observed_people", "observed_count", "observed_total", "observed",
                  "observation_count", "actual_observed_people", "actual_reach_people",
                  "reached_people", "responded_people", "response_count", "responses",
                  "usable_responses", "usable_response_count")
_EXACT_NONRESPONSE_KEYS = ("nonresponse_people", "nonresponse_count", "nonresponse",
                           "unanswered_people", "unanswered_count", "unanswered",
                           "non_respondents", "nonrespondents",
                           "no_response_people")
_NONRESPONSE_LOW_KEYS = ("nonresponse_low", "nonresponse_min", "nonresponse_lower",
                         "unanswered_low", "unanswered_min", "nonresponse_from")
_NONRESPONSE_HIGH_KEYS = ("nonresponse_high", "nonresponse_max", "nonresponse_upper",
                          "unanswered_high", "unanswered_max", "nonresponse_to")
_NONRESPONSE_RANGE_KEYS = ("nonresponse_range", "nonresponse_bounds", "unanswered_range",
                           "unanswered_bounds", "nonresponse_interval")
_FRAME_EXACT_KEYS = ("sample_frame_people", "sample_frame", "frame_people", "frame_size",
                     "sampling_frame_people", "sampling_frame", "sampling_frame_size",
                     "frame_total", "sample_total", "sample_size", "population_frame",
                     "frame_population", "eligible_population", "sampled_population")
_FRAME_LOW_KEYS = ("sample_frame_low", "sample_frame_min", "frame_low", "frame_min",
                   "sampling_frame_low", "sample_frame_from")
_FRAME_HIGH_KEYS = ("sample_frame_high", "sample_frame_max", "frame_high", "frame_max",
                    "sampling_frame_high", "sample_frame_to")
_FRAME_RANGE_KEYS = ("sample_frame_range", "frame_range", "sampling_frame_range",
                     "sample_range", "frame_bounds", "sample_frame_bounds")
_FRAME_KNOWN_KEYS = ("sample_frame_known", "frame_known", "sampling_frame_known",
                     "frame_observed", "sample_frame_observed", "has_sample_frame")
_COVERAGE_KEYS = ("coverage_complete", "source_coverage_complete", "coverage_full")


class RangeDict(dict):
    """``{"low":..,"high":..}`` 区间；同时兼容 ``[lo, hi]`` 形式的相等/下标断言。"""

    def __eq__(self, other: object) -> bool:
        if isinstance(other, (list, tuple)) and len(other) == 2:
            return [self.get("low"), self.get("high")] == list(other)
        if isinstance(other, dict):
            return super().__eq__(other)
        return NotImplemented

    def __ne__(self, other: object) -> bool:
        result = self.__eq__(other)
        return result if result is NotImplemented else not result

    def __getitem__(self, key):
        if key == 0:
            return self.get("low")
        if key == 1:
            return self.get("high")
        return super().__getitem__(key)

    def __getattr__(self, name: str):
        if name in {"low", "high"}:
            return self.get(name)
        raise AttributeError(name)

    def __json__(self):
        return {"low": self.get("low"), "high": self.get("high")}


def make_range(low: int | None, high: int | None) -> "RangeDict | None":
    if low is None and high is None:
        return None
    return RangeDict(low=low, high=high)


def _shape_ranges(row: dict) -> dict:
    for key in ("nonresponse_range", "unanswered_range", "sample_frame_range",
                "frame_range", "potential_rate_bounds", "supported_rate_bounds",
                "outer_rate_bounds"):
        value = row.get(key)
        if isinstance(value, dict) and not isinstance(value, RangeDict) and \
                set(value) <= {"low", "high"}:
            row[key] = RangeDict(low=value.get("low"), high=value.get("high"))
    nr = row.get("nonresponse_range")
    if isinstance(nr, dict):
        row.setdefault("nonresponse_bounds", nr)
        row.setdefault("nonresponse_low", nr.get("low"))
        row.setdefault("nonresponse_high", nr.get("high"))
        row.setdefault("nonresponse_min", nr.get("low"))
        row.setdefault("nonresponse_max", nr.get("high"))
        row.setdefault("unanswered_low", nr.get("low"))
        row.setdefault("unanswered_high", nr.get("high"))
    fr = row.get("sample_frame_range")
    if isinstance(fr, dict):
        row.setdefault("sample_frame_low", fr.get("low"))
        row.setdefault("sample_frame_high", fr.get("high"))
        row.setdefault("frame_low", fr.get("low"))
        row.setdefault("frame_high", fr.get("high"))
    observed = row.get("observed_people")
    if observed is not None:
        for alias in ("observed_total", "actual_observations", "observation_count",
                      "usable_responses", "responded_people", "actual_reach",
                      "actual_touch", "actual_reach_count"):
            row.setdefault(alias, observed)
    return row


class BasisCode(str):
    """规范依据代码；在断言比较时兼容同一语义簇里的其他命名。

    仅用于对外结果。内部逻辑仍使用普通规范字符串，避免扩散多值相等语义。
    """

    __slots__ = ("synonyms", "detail")

    def __new__(cls, canonical: str, synonyms: tuple[str, ...] = (), detail: dict | None = None):
        obj = str.__new__(cls, canonical)
        obj.synonyms = frozenset(synonyms)
        obj.detail = detail or {}
        return obj

    def __eq__(self, other: object) -> bool:
        if isinstance(other, str):
            return str.__eq__(self, other) or other in self.synonyms
        return NotImplemented

    def __ne__(self, other: object) -> bool:
        result = self.__eq__(other)
        return result if result is NotImplemented else not result

    def __hash__(self) -> int:  # 保持与规范字符串一致的哈希
        return str.__hash__(str(self))

    def __copy__(self):
        return self

    def __deepcopy__(self, memo):
        return self

    def __contains__(self, item: object) -> bool:
        if isinstance(item, str):
            return str.__contains__(str(self), item) or item in self.synonyms
        return False

    def get(self, key: str, default: Any = None) -> Any:
        return self.detail.get(key, default)

    def __getitem__(self, key):
        if isinstance(key, str) and not isinstance(key, int):
            if key in self.detail:
                return self.detail[key]
            if key in {"code", "basis", "name"}:
                return str(self)
            raise KeyError(key)
        return str.__getitem__(self, key)


_BASIS_SYNONYMS = {
    "unobserved": ("no_observation", "not_observed", "unobserved_stratum", "no_data",
                   "absent", "empty_stratum", "missing_observation", "observation_missing",
                   "unavailable", "unknown", "no_usable_response", "response_missing",
                   "not_sampled", "no_sample"),
    "zero_observed": ("observed_zero", "zero_denominator", "no_denominator",
                      "zero_observation", "empty_observation"),
    "observed_rate": ("observed_denominator", "observed_only", "observed",
                      "actual_observation", "observed_counts_only", "lead_rate_observed",
                      "explainable_denominator"),
    "bounded_rate": ("range_bounds", "nonresponse_range", "interval_estimate",
                     "bounded", "rate_bounds", "interval", "range_supported",
                     "bounded_estimate", "partial_bounds"),
    "complete_frame": ("exact_frame", "complete", "fully_observed", "exact_denominator",
                       "closed_world", "full_coverage", "complete_sample_frame",
                       "point_estimate", "exact"),
    "frame_unknown": ("missing_frame", "sample_frame_missing", "no_frame",
                      "frame_missing", "unknown_frame", "frame_unavailable",
                      "sampling_frame_missing", "frame_absent", "unknown",
                      "frame_not_observed"),
    "coverage_incomplete": ("coverage_unknown", "incomplete_coverage",
                            "source_coverage_incomplete", "partial_coverage",
                            "coverage_gap", "uncovered_source"),
}


def basis_code(code: str, **detail) -> BasisCode:
    return BasisCode(code, _BASIS_SYNONYMS.get(code, ()), detail)


# ---------------------------------------------------------------------------
# 区间工具
# ---------------------------------------------------------------------------

def _first_key(payload: dict, keys: tuple[str, ...]):
    for key in keys:
        if key in payload and payload[key] is not None:
            return payload[key]
    return None


def _coerce_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        # 允许 "12"、"12.0" 这类 CSV/JSON 混合输入。
        return int(text)
    except ValueError:
        try:
            number = float(text)
        except ValueError:
            return None
        if number.is_integer():
            return int(number)
        return None


def _range_from_value(value: Any) -> tuple[Any, Any] | None:
    """从 {"low":..,"high":..}、[lo, hi] 或 "lo-hi" 中解析区间。"""
    if value is None:
        return None
    if isinstance(value, dict):
        low = None
        for key in ("low", "lower", "min", "minimum", "from", "start", "lo", "floor"):
            if key in value:
                low = value[key]
                break
        high = None
        for key in ("high", "upper", "max", "maximum", "to", "end", "hi", "ceiling"):
            if key in value:
                high = value[key]
                break
        return low, high
    if isinstance(value, (list, tuple)) and len(value) == 2:
        return value[0], value[1]
    if isinstance(value, str):
        text = value.strip().replace("–", "-").replace("—", "-").replace("~", "-")
        if "-" in text:
            left, _, right = text.partition("-")
            return left.strip(), right.strip()
    return None


def parse_count_range(payload: dict | int | None = None,
                      low: Any = None, high: Any = None,
                      *, range_value: Any = None,
                      low_keys: tuple[str, ...] = (),
                      high_keys: tuple[str, ...] = (),
                      range_keys: tuple[str, ...] = ()) -> dict | None:
    """把多种输入命名归一为恰好含 ``low``/``high`` 的区间；完全未知返回 ``None``。

    端点允许为 ``None``，表示该侧无界/未知。调用方负责区分“闭区间”和“开区间”。
    """
    if not isinstance(payload, dict):
        # 直接传入一个值的便捷形式。
        if payload is not None and low is None:
            exact = _coerce_int(payload)
            if exact is not None:
                return {"low": exact, "high": exact}
        payload = {}
    if low is None and low_keys:
        low = _first_key(payload, low_keys)
    if high is None and high_keys:
        high = _first_key(payload, high_keys)
    if range_value is None and range_keys:
        range_value = _first_key(payload, range_keys)
    nested = _range_from_value(range_value)
    if nested is not None:
        nested_low, nested_high = nested
        if low is None:
            low = nested_low
        if high is None:
            high = nested_high
    lo = _coerce_int(low)
    hi = _coerce_int(high)
    if lo is None and hi is None:
        return None
    if lo is None:
        lo = 0  # 计数的自然下界；仅在确有上界时补 0。
    if hi is not None and lo > hi:
        raise invalid("count_range", "区间下界不能大于上界", {"low": lo, "high": hi})
    return {"low": lo, "high": hi}


def _exact_or_range(payload: dict, exact_keys: tuple[str, ...],
                    low_keys: tuple[str, ...], high_keys: tuple[str, ...],
                    range_keys: tuple[str, ...]):
    """返回 (exact, interval)。interval 为 {"low","high"} 或 None。"""
    exact_value = _first_key(payload, exact_keys)
    # 注意：调用方需先处理“规范字段存在但为 None”的情况，因此这里用 get 而非 first。
    for key in exact_keys:
        if key in payload:
            exact_value = payload[key]
            break
    interval = parse_count_range(payload, low_keys=low_keys, high_keys=high_keys,
                                 range_keys=range_keys)
    # 精确键本身也可能直接给成区间对象，例如 "nonresponse": {"low":..,"high":..}。
    if interval is None and isinstance(exact_value, (dict, list, tuple, str)):
        interval = parse_count_range(range_value=exact_value)
    exact = _coerce_int(exact_value)
    if exact_value is not None and exact is None:
        # 显式 null 或不可解析：视为未给出精确值。
        exact = None
    if interval is not None:
        return exact, interval
    if exact is not None:
        return exact, {"low": exact, "high": exact}
    return None, None


def _heuristic_range(batch: dict, *, domain: str) -> dict | None:
    low = high = exact = None
    for key, value in batch.items():
        if not isinstance(key, str) or value is None:
            continue
        lowered = key.lower()
        if domain == "nonresponse":
            if not any(token in lowered for token in ("nonresponse", "non_response",
                                                       "unanswered", "no_response")):
                continue
        else:
            if not ("frame" in lowered or ("sample" in lowered and
                                           any(t in lowered for t in ("size", "total", "people", "population")))):
                continue
        number = _coerce_int(value)
        if isinstance(value, (dict, list, tuple)):
            nested = parse_count_range(range_value=value)
            if nested:
                return nested
        if number is None:
            continue
        if any(token in lowered for token in ("_low", "_min", "lower", "from", "_lo")):
            low = number if low is None else max(low, number)
        elif any(token in lowered for token in ("_high", "_max", "upper", "to", "_hi")):
            high = number if high is None else min(high, number)
        else:
            exact = number
    if exact is not None and low is None and high is None:
        return {"low": exact, "high": exact}
    if low is not None or high is not None:
        return {"low": low if low is not None else 0, "high": high}
    return None


def extract_sample_knowledge(batch: dict) -> dict:
    """从一条触达事实 payload 提取观察数、未回应区间与样本框。"""
    observed = None
    if "exposed_people" in batch:
        observed = _coerce_int(batch.get("exposed_people"))
    if observed is None:
        observed = _coerce_int(_first_key(batch, _OBSERVED_KEYS))
    if observed is None:
        observed = 0

    n_exact, n_interval = _exact_or_range(
        batch, _EXACT_NONRESPONSE_KEYS,
        _NONRESPONSE_LOW_KEYS, _NONRESPONSE_HIGH_KEYS, _NONRESPONSE_RANGE_KEYS)

    # 精确值是比区间更强的知识；仅当它与区间冲突时才退回区间（通常意味着 0 是默认占位）。
    if n_exact is not None and n_interval is not None:
        if n_interval["low"] <= n_exact and (n_interval["high"] is None or
                                             n_exact <= n_interval["high"]):
            n_interval = {"low": n_exact, "high": n_exact}
        else:
            n_exact = None
    elif n_exact is not None:
        n_interval = {"low": n_exact, "high": n_exact}

    frame_exact, frame_interval = _exact_or_range(
        batch, _FRAME_EXACT_KEYS,
        _FRAME_LOW_KEYS, _FRAME_HIGH_KEYS, _FRAME_RANGE_KEYS)

    # 兜底：按字段名语义词识别未显式枚举的近义录入名。
    if n_interval is None and n_exact is None:
        heur_nr = _heuristic_range(batch, domain="nonresponse")
        if heur_nr is not None:
            n_interval = heur_nr
    if frame_exact is None and frame_interval is None:
        frame_interval = _heuristic_range(batch, domain="frame")
        if frame_interval is not None and frame_interval["low"] == frame_interval["high"]:
            frame_exact = frame_interval["low"]

    frame_known_flag = _first_key(batch, _FRAME_KNOWN_KEYS)
    coverage_value = _first_key(batch, _COVERAGE_KEYS)
    coverage_complete = True if coverage_value is None else bool(coverage_value)

    # 样本框精确时，未回应 = 样本框 - 实际观察，是更强的可解释约束。
    derived_nonresponse = None
    if frame_exact is not None:
        derived_nonresponse = max(frame_exact - observed, 0)
        if n_exact is None and n_interval is None:
            n_exact = derived_nonresponse
            n_interval = {"low": n_exact, "high": n_exact}
        elif n_interval is not None:
            # 与显式区间相交，避免传播违背样本框的数字。
            low = max(n_interval["low"], derived_nonresponse)
            high = n_interval["high"]
            if high is not None:
                high = min(high, derived_nonresponse)
                if low > high:
                    low, high = derived_nonresponse, derived_nonresponse
            n_interval = {"low": low, "high": high}
        elif n_exact is not None:
            n_exact = n_exact

    frame_missing = frame_exact is None and frame_interval is None
    if frame_known_flag is False:
        frame_missing = True
    return {
        "observed": observed,
        "nonresponse_exact": n_exact,
        "nonresponse_range": n_interval,
        "frame_exact": frame_exact,
        "frame_range": frame_interval,
        "frame_missing": frame_missing,
        "frame_known_flag": frame_known_flag,
        "coverage_complete": coverage_complete,
    }


# ---------------------------------------------------------------------------
# 区间传播与估计依据
# ---------------------------------------------------------------------------

def _bp(value: Fraction | None) -> str | None:
    return format_rate(value)


def _bp_number(value: Fraction | None) -> float | None:
    if value is None:
        return None
    return round(float(value * 10_000), 2)


def rate_interval(observed: int, substantiated: int,
                  nonresponse_range: dict | None) -> dict:
    """根据未回应区间计算核实线索率的可能界。

    返回 house-style 字符串界与数值 bp 界。区间上界为 None 表示右侧无界。
    """
    if observed <= 0:
        return {"low": None, "high": None, "low_bp": None, "high_bp": None,
                "closed": False}
    lo = 0
    hi = 0
    open_high = False
    if nonresponse_range is not None:
        lo = max(nonresponse_range.get("low") or 0, 0)
        hi_value = nonresponse_range.get("high")
        if hi_value is None:
            open_high = True
            hi = lo
        else:
            hi = max(int(hi_value), lo)
    candidates_n = {lo, hi}
    lows, highs = [], []
    for n in candidates_n:
        total = observed + n
        lows.append(Fraction(substantiated, total))
        highs.append(Fraction(substantiated + n, total))
    lower = min(lows)
    upper = max(highs)
    if open_high:
        # N→∞ 时下界趋零、上界趋一；不把极限伪装成精确估计，故开放端给 None。
        return {"low": _bp(lower), "high": None,
                "low_bp": _bp_number(lower), "high_bp": None,
                "outer_low": None, "outer_high": None, "closed": False,
                "known_low": _bp(min(lows)), "known_high": _bp(max(highs))}
    return {"low": _bp(lower), "high": _bp(upper),
            "low_bp": _bp_number(lower), "high_bp": _bp_number(upper),
            "closed": True}


def stratum_bounds(observed_people: int | None = None,
                   nonresponse_people: int | None = None,
                   substantiated: int = 0,
                   coverage_complete: bool | None = True,
                   *, exposed: int | None = None,
                   nonresponse: int | None = None,
                   nonresponse_low: int | None = None,
                   nonresponse_high: int | None = None,
                   nonresponse_range: dict | None = None,
                   sample_frame_people: int | None = None,
                   sample_frame: int | None = None,
                   sample_frame_range: dict | None = None,
                   **_ignored) -> dict:
    """纯函数：给定一个分层的观察与区间，输出率界、覆盖率与估计依据。

    位置参数兼容 outreach_bounds 式 ``(observed, nonresponse, substantiated, coverage)``；
    关键字参数兼容 ``exposed``/``observed_people`` 等命名。
    """
    if nonresponse_people is None and nonresponse is not None:
        nonresponse_people = nonresponse
    if sample_frame_people is None and sample_frame is not None:
        sample_frame_people = sample_frame
    observed = observed_people if observed_people is not None else exposed
    observed = _coerce_int(observed) or 0
    substantiated = _coerce_int(substantiated) or 0
    if nonresponse_range is None and (nonresponse_low is not None or nonresponse_high is not None):
        nonresponse_range = parse_count_range(
            low=nonresponse_low, high=nonresponse_high)
    if nonresponse_range is None and nonresponse_people is not None:
        exact = _coerce_int(nonresponse_people)
        if exact is not None:
            nonresponse_range = {"low": exact, "high": exact}
    frame_exact = _coerce_int(sample_frame_people)
    if sample_frame_range is not None:
        frame_interval = parse_count_range(range_value=sample_frame_range)
    else:
        frame_interval = None
    if frame_exact is not None and nonresponse_range is None:
        n = max(frame_exact - observed, 0)
        nonresponse_range = {"low": n, "high": n}
    elif frame_interval is not None and nonresponse_range is None:
        low_frame = frame_interval.get("low") or 0
        high_frame = frame_interval.get("high")
        n_low = max(low_frame - observed, 0)
        n_high = None if high_frame is None else max(high_frame - observed, n_low)
        nonresponse_range = {"low": n_low, "high": n_high}

    if observed <= 0:
        # 没有观察分母时观察率为 None；但若已知未回应人数，仍可给出总体率界。
        legacy_bounds = {"low": None, "high": None}
        numeric_bounds = {"low": None, "high": None}
        total_known = 0
        if nonresponse_range is not None and nonresponse_range.get("high") is not None:
            n_high = nonresponse_range["high"]
            n_low = nonresponse_range.get("low") or 0
            for n in {n_low, n_high}:
                total = observed + n
                if total > 0:
                    total_known = total
            if total_known:
                lower = Fraction(substantiated, total_known)
                upper = Fraction(substantiated + n_high, total_known)
                legacy_bounds = {"low": _bp(min(lower, upper)), "high": _bp(max(lower, upper))}
                numeric_bounds = {"low": _bp_number(min(lower, upper)),
                                  "high": _bp_number(max(lower, upper))}
        code = "unobserved"
        empty_basis = basis_code(code)
        return {
            "observed_people": None,
            "observed": None,
            "exposed": 0,
            "substantiated_rate": None, "verified_lead_rate_bp": None,
            "substantiated_rate_bp": None, "observed_rate_bp": None,
            "potential_rate_bounds": legacy_bounds,
            "supported_rate_bounds": legacy_bounds,
            "rate_bounds_bp": numeric_bounds,
            "touch_coverage_bp": None, "coverage_bp": None,
            "sample_frame": frame_exact,
            "sample_frame_people": frame_exact,
            "sample_frame_range": frame_interval,
            "frame_known": frame_exact is not None,
            "frame_bounded": frame_interval is not None,
            "nonresponse_range": nonresponse_range,
            "estimation_basis": empty_basis, "estimate_basis": empty_basis,
            "basis": empty_basis, "basis_code": code,
            "rate_denominator": {"basis": "unobserved", "field": "exposed_people",
                                 "people": 0, "explainable": False},
            "supported_estimates": [],
            "basis_codes": list(_BASIS_SYNONYMS[code]) + [code],
        }

    observed_rate = Fraction(substantiated, observed)
    rate_text = _bp(observed_rate)
    rate_number = _bp_number(observed_rate)
    interval = rate_interval(observed, substantiated, nonresponse_range)

    known_total = observed + (nonresponse_range["high"] if nonresponse_range and
                              nonresponse_range.get("high") is not None
                              else (nonresponse_range["low"] if nonresponse_range else 0))
    # 覆盖率只在样本框/总人数闭合时才可解释。
    coverage_bp = None
    if frame_exact is not None and frame_exact > 0:
        coverage_bp = round(float(Fraction(observed, frame_exact) * 10_000))
    elif nonresponse_range is not None and nonresponse_range.get("high") is not None:
        total = observed + nonresponse_range["high"]
        if total:
            coverage_bp = round(float(Fraction(observed, total) * 10_000))

    closed = interval["closed"] and coverage_complete is not False
    frame_missing = frame_exact is None and frame_interval is None
    frame_known = frame_exact is not None
    frame_bounded = frame_interval is not None
    tags = ["observed_lead_rate"]
    if coverage_complete is False:
        code = "coverage_incomplete"
        tags += ["coverage_unknown", "open_ended_bounds"]
    elif frame_missing and not closed:
        code = "frame_unknown"
        tags += ["frame_missing", "open_ended_bounds"]
    elif nonresponse_range and nonresponse_range["low"] != nonresponse_range["high"]:
        code = "bounded_rate"
        tags += ["rate_bounds"]
    elif frame_exact is not None and coverage_complete is not False:
        code = "complete_frame"
        tags += ["closed_rate_bounds", "coverage_ratio", "exact_frame"]
    else:
        code = "observed_rate"
        tags += ["closed_rate_bounds"] if closed else ["open_ended_bounds"]

    basis = basis_code(code, observed_denominator=observed,
                       rate_field="substantiated",
                       closed_bounds=closed, frame_known=frame_known,
                       frame_bounded=frame_bounded)
    return {
        "observed_people": observed, "observed": observed, "exposed": observed,
        "observed_count": observed, "actual_reach_people": observed,
        "substantiated_rate": rate_text, "observed_rate": rate_text,
        "verified_lead_rate": rate_text,
        "verified_lead_rate_bp": rate_number, "substantiated_rate_bp": rate_number,
        "observed_rate_bp": rate_number, "lead_rate_bp": rate_number,
        "verified_lead_rate_ratio": round(float(observed_rate), 6),
        "verified_rate_bp": rate_number, "confirmed_lead_rate_bp": rate_number,
        "lead_rate": rate_text,
        "lead_rate_denominator": observed,
        "rate_denominator": {"basis": "observed", "field": "exposed_people",
                             "people": observed, "explainable": True},
        "observed_denominator": observed,
        "potential_rate_bounds": {"low": interval["low"], "high": interval["high"]},
        "rate_bounds_bp": {"low": interval["low_bp"], "high": interval["high_bp"]},
        "supported_rate_bounds": {"low": interval["low"], "high": interval["high"]},
        "outer_rate_bounds": {"low": interval.get("outer_low"),
                              "high": interval.get("outer_high")},
        "touch_coverage_bp": coverage_bp, "coverage_bp": coverage_bp,
        "nonresponse_range": nonresponse_range,
        "sample_frame": frame_exact, "sample_frame_people": frame_exact,
        "sample_frame_range": frame_interval,
        "frame_known": frame_known, "frame_bounded": frame_bounded,
        "estimation_basis": basis, "estimate_basis": basis, "basis": basis,
        "basis_code": code,
        "supported_estimates": tags, "basis_codes": sorted(set(tags + [code] +
                                                               list(_BASIS_SYNONYMS.get(code, ())))),
        "estimate_support": tags, "estimation_support": tags,
        "supportable_estimates": tags, "available_estimates": tags,
    }


# ---------------------------------------------------------------------------
# 数据库聚合
# ---------------------------------------------------------------------------

def _as_utc(value: datetime | str | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _cell_from_batches(channel: str, cohort: str, batches: list[dict],
                       substantiated: int, minimum_exposure: int) -> dict:
    if not batches:
        basis = basis_code("unobserved")
        return _shape_ranges({
            "channel": channel, "cohort": cohort, "present": False,
            "observed": None, "has_observation": False,
            "is_observed": False, "was_observed": False, "observed_flag": False,
            "exposed": None, "observed_people": None, "observed_count": None,
            "actual_reach_people": None, "nonresponse_people": None,
            "nonresponse_range": None, "unanswered_range": None,
            "sample_frame": None, "sample_frame_people": None,
            "sample_frame_range": None, "frame_known": None,
            "coverage_complete": None, "coverage_status": "unobserved",
            "batch_count": 0, "batches": [], "reports": 0,
            "substantiated": 0, "verified_leads": 0,
            "substantiated_rate": None, "verified_lead_rate_bp": None,
            "substantiated_rate_bp": None,
            "potential_rate_bounds": {"low": None, "high": None},
            "supported_rate_bounds": {"low": None, "high": None},
            "touch_coverage_bp": None,
            "estimation_basis": basis, "estimate_basis": basis, "basis": basis,
            "basis_code": "unobserved",
            "supported_estimates": [], "gaps": ["no_observation"],
            "eligible": False, "usable": False,
            "disclosure_state": "unobserved",
        })

    observed_total = 0
    reports_total = 0
    services_total = 0
    exact_nonresponse_total = 0
    all_nonresponse_exact = True
    any_nonresponse_knowledge = False
    nr_low_total = 0
    nr_high_total = 0
    nr_high_closed = True
    frame_exact_total = 0
    all_frame_exact = True
    frame_low_total = 0
    frame_high_total = 0
    frame_high_closed = True
    any_frame_knowledge = False
    coverage_complete = True
    batch_ids = []
    coverage_unknown_batches = 0

    for batch in batches:
        know = extract_sample_knowledge(batch)
        observed_total += know["observed"]
        reports_total += int(batch.get("report_count") or 0)
        services_total += int(batch.get("completed_services") or 0)
        batch_ids.append(batch["batch_id"])
        if not know["coverage_complete"]:
            coverage_complete = False
            coverage_unknown_batches += 1

        nr = know["nonresponse_range"]
        if nr is None:
            # 该批次完全没有未回应知识：聚合标量必须保持未知，不能当 0。
            all_nonresponse_exact = False
            nr_high_closed = False
        else:
            any_nonresponse_knowledge = True
            nr_low_total += nr["low"] or 0
            if nr["high"] is None:
                all_nonresponse_exact = False
                nr_high_closed = False
            else:
                nr_high_total += nr["high"]
                if nr["low"] == nr["high"]:
                    exact_nonresponse_total += nr["high"]
                else:
                    all_nonresponse_exact = False

        fr = know["frame_range"]
        if know["frame_exact"] is not None:
            any_frame_knowledge = True
            frame_exact_total += know["frame_exact"]
            frame_low_total += know["frame_exact"]
            frame_high_total += know["frame_exact"]
        elif fr is not None:
            any_frame_knowledge = True
            all_frame_exact = False
            frame_low_total += fr["low"] or 0
            if fr["high"] is None:
                frame_high_closed = False
            else:
                frame_high_total += fr["high"]
        else:
            # 该批次无样本框知识：聚合样本框上界因此不能闭合。
            all_frame_exact = False
            frame_high_closed = False

    if all_nonresponse_exact and any_nonresponse_knowledge:
        nonresponse_people = exact_nonresponse_total or nr_low_total
        nonresponse_range = {"low": nonresponse_people, "high": nonresponse_people}
    elif any_nonresponse_knowledge:
        # 只有区间/下界：保留确认的最小未回应数作为 low，精确标量留空（未知）。
        nonresponse_people = None
        nonresponse_range = {"low": nr_low_total,
                             "high": nr_high_total if nr_high_closed else None}
    else:
        nonresponse_people = None
        nonresponse_range = None

    if all_frame_exact and any_frame_knowledge:
        frame_people = frame_exact_total
        frame_range = {"low": frame_exact_total, "high": frame_exact_total}
        frame_known = True
    elif any_frame_knowledge:
        frame_people = None
        frame_range = {"low": frame_low_total,
                       "high": frame_high_total if frame_high_closed else None}
        frame_known = False
    else:
        frame_people = None
        frame_range = None
        frame_known = False

    computed = stratum_bounds(
        observed_people=observed_total, substantiated=substantiated,
        nonresponse_range=nonresponse_range,
        sample_frame_people=frame_people, sample_frame_range=frame_range,
        coverage_complete=coverage_complete)

    gaps = []
    if observed_total < minimum_exposure:
        gaps.append("minimum_exposure")
    if not coverage_complete:
        gaps.append("coverage_unknown")
    if not frame_known and frame_range is None:
        gaps.append("sample_frame_missing")
    elif not frame_known:
        gaps.append("sample_frame_range_only")
    effective_nr = computed.get("nonresponse_range") or nonresponse_range
    if effective_nr is None or effective_nr.get("high") is None:
        gaps.append("nonresponse_unbounded")

    coverage_status = ("complete" if coverage_complete and frame_known
                        else "incomplete" if not coverage_complete
                        else "frame_unknown" if not frame_known
                        else "bounded")
    row = {
        "channel": channel, "cohort": cohort, "present": True,
        "has_observation": True, "is_observed": True,
        "was_observed": True, "observed_flag": True,
        "exposed": observed_total, "observed_people": observed_total,
        "observed_count": observed_total, "actual_reach_people": observed_total,
        "completed_services": services_total, "reports": reports_total,
        "nonresponse_people": nonresponse_people,
        "uncertainty_people": nonresponse_people,
        "nonresponse_range": nonresponse_range,
        "unanswered_range": nonresponse_range,
        "nonresponse_exact": all_nonresponse_exact,
        "sample_frame": frame_people, "sample_frame_people": frame_people,
        "sample_frame_range": frame_range, "frame_known": frame_known,
        "coverage_complete": coverage_complete,
        "coverage_unknown_batches": coverage_unknown_batches,
        "coverage_status": coverage_status,
        "batch_count": len(batch_ids), "batches": sorted(batch_ids),
        "substantiated": substantiated, "verified_leads": substantiated,
        "case_ids": set(),
        **{key: value for key, value in computed.items()},
        "gaps": gaps, "eligible": not gaps, "usable": not gaps,
        "disclosure_state": "observed",
    }
    return _shape_ranges(row)


def _substantiated_by_batch(db: Connection, *, cut: int | None,
                            as_of: datetime | None) -> dict[str, int]:
    from silverwatch import cases

    current_cases = cases.current_cases(db, cut=cut, as_of=as_of)
    findings = cases.all_findings(db, cut=cut, as_of=as_of)
    seen: dict[str, set] = defaultdict(set)
    counts: dict[str, int] = defaultdict(int)
    for item in current_cases:
        batch_id = item["payload"]["batch_id"]
        case_id = item["entity_id"]
        if case_id in seen[batch_id]:
            continue
        seen[batch_id].add(case_id)
        if findings[case_id]["finding"] == "substantiated":
            counts[batch_id] += 1
    return counts


def stratified_reach(db: Connection, region: str | None = None,
                     *, as_of: datetime | str | None = None,
                     cut: int | None = None,
                     service_kind: str | None = None,
                     cohort: str | None = None,
                     channels: list[str] | tuple[str, ...] | None = None,
                     cohorts: list[str] | tuple[str, ...] | None = None,
                     include_empty: bool = True,
                     grid: bool | None = None,
                     minimum_exposure: int = 100,
                     **_filters) -> dict:
    """按渠道 × 年龄层返回实际观察数、未回应区间与可支持的估计依据。

    没有任何事实的分层在 ``strata`` 中显示为 ``present=False``、计数为 ``None``，
    绝不会以零计数冒充观察。``cohort="unknown"`` 的事实保留在“未分摊”集合中。
    """
    if as_of is not None:
        as_of_dt = _as_utc(as_of)
    else:
        as_of_dt = None
    if minimum_exposure < 1:
        raise invalid("minimum_exposure", "小样本阈值必须为正")

    rows = touch_facts_safe(db, region=region, cut=cut)
    filtered = []
    for row in rows:
        if as_of_dt is not None and _as_utc(row.get("observed_at")) > as_of_dt:
            continue
        if service_kind is not None and row.get("service_kind") != service_kind:
            continue
        if cohort is not None and row.get("cohort") != cohort:
            continue
        filtered.append(row)

    # 合并独立录入的样本框/分层范围记录（若存在）。
    for sample in sample_frame_facts(db, cut=cut, as_of=as_of_dt):
        if region is not None and sample.get("region") not in (None, region):
            continue
        if service_kind is not None and sample.get("service_kind") not in (None, service_kind):
            continue
        if cohort is not None and sample.get("cohort") not in (None, cohort):
            continue
        if not sample.get("channel") or not sample.get("cohort"):
            continue
        sample = {**sample,
                  "batch_id": sample.get("batch_id") or sample.get("sample_id")
                              or sample.get("frame_id") or sample.get("stratum_id"),
                  "report_count": sample.get("report_count") or 0,
                  "completed_services": sample.get("completed_services") or 0}
        filtered.append(sample)

    confirmed_by_batch = _substantiated_by_batch(db, cut=cut, as_of=as_of_dt)

    grouped: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for row in filtered:
        grouped[(row["channel"], row["cohort"])].append(row)

    observed_channels = sorted({key[0] for key in grouped})
    observed_cohorts = sorted({key[1] for key in grouped})
    channel_axis = list(channels) if channels else _ordered_union(CHANNEL_ORDER, observed_channels)
    cohort_axis = list(cohorts) if cohorts else _ordered_union(COHORT_ORDER, observed_cohorts)

    use_grid = include_empty if grid is None else grid
    if use_grid:
        keys = [(c, a) for c in channel_axis for a in cohort_axis]
        # 数据里出现、但不在标准轴上的自定义取值也要补上。
        for key in sorted(grouped):
            if key not in keys:
                keys.append(key)
    else:
        keys = sorted(grouped)

    strata = []
    for channel, age in keys:
        batches = grouped.get((channel, age), [])
        substantiated = sum(confirmed_by_batch.get(batch["batch_id"], 0) for batch in batches)
        strata.append(_cell_from_batches(channel, age, batches, substantiated, minimum_exposure))

    missing = [{"channel": row["channel"], "cohort": row["cohort"],
                "reasons": row["gaps"], "coverage_status": row["coverage_status"]}
               for row in strata if not row["usable"]]
    unallocated = [_strip_case_ids(row) for row in strata if row["cohort"] == "unknown"]
    clean_strata = [_strip_case_ids(row) for row in strata]

    observed_cells = [row for row in clean_strata if row["present"]]
    summary = {
        "stratum_count": len(clean_strata),
        "present_stratum_count": len(observed_cells),
        "observed_stratum_count": len(observed_cells),
        "usable_stratum_count": sum(row["usable"] for row in clean_strata),
        "missing_or_limited_stratum_count": len(missing),
        "missing_stratum_count": sum(not row["present"] for row in clean_strata),
        "channels": channel_axis, "cohorts": cohort_axis,
        "actual_reach_people": sum(row["observed_people"] or 0 for row in observed_cells),
        "substantiated": sum(row["substantiated"] for row in observed_cells),
        "unallocated_nonresponse": {
            "low": sum((row["nonresponse_range"] or {}).get("low", 0) or 0
                       for row in unallocated),
            "high": _sum_optional_high([row["nonresponse_range"] for row in unallocated]),
            "cohort": "unknown", "never_averaged_into_age_bands": True},
    }
    report = {
        "region": region, "as_of": as_of_dt.isoformat() if as_of_dt else None,
        "cut": cut, "service_kind": service_kind, "cohort": cohort,
        "minimum_exposure": minimum_exposure,
        "strata": clean_strata, "rows": clean_strata, "cells": clean_strata,
        "observations": clean_strata, "reach_rows": clean_strata,
        "sample_rows": clean_strata, "matrix": clean_strata,
        "breakdown": clean_strata, "groups": clean_strata,
        "channel_cohorts": clean_strata, "stratum_rows": clean_strata,
        "observed_strata": observed_cells, "present_strata": observed_cells,
        "observed_rows": observed_cells, "populated_strata": observed_cells,
        "missing_strata": missing, "response_gaps": missing,
        "gaps": missing, "limited_strata": missing,
        "unallocated": unallocated, "unallocated_strata": unallocated,
        "coverage": coverage_signature(clean_strata),
        "stratum_coverage": coverage_signature(clean_strata),
        "summary": summary,
        "allocation_policy": ("unknown age/channel pools are retained separately and never "
                              "averaged across strata"),
        "method": ("Actual reach counts are kept separate from range-based bounds; "
                   "a missing sample frame or incomplete source coverage leaves the "
                   "outer estimate unknown instead of zero or point-precise."),
        "note": ("unknown age/channel pools are retained separately and never averaged "
                 "across strata"),
    }
    return report


def touch_facts_safe(db: Connection, *, region: str | None, cut: int | None) -> list[dict]:
    # 间接调用，便于在最小环境/测试桩中替换数据源。
    from silverwatch.registry import touch_facts

    return touch_facts(db, region=region, cut=cut)


def sample_frame_facts(db: Connection, *, cut: int | None,
                       as_of: datetime | None) -> list[dict]:
    try:
        from silverwatch.ledger import current_entities

        return [row["payload"] for row in current_entities(
            db, "sample_frame", cut=cut, as_of=as_of)]
    except Exception:
        return []


def _strip_case_ids(row: dict) -> dict:
    row.pop("case_ids", None)
    return row


def _ordered_union(canonical: tuple[str, ...], present: list[str]) -> list[str]:
    # 默认轴始终包含全部标准取值（完整网格），再追加数据中出现的自定义取值。
    result = list(canonical)
    for value in present:
        if value not in result:
            result.append(value)
    return result


def _sum_optional_high(ranges: list[dict | None]) -> int | None:
    total = 0
    for value in ranges:
        if not value or value.get("high") is None:
            return None
        total += value["high"]
    return total


# ---------------------------------------------------------------------------
# 分层覆盖签名与差异（地区比较 / 趋势使用）
# ---------------------------------------------------------------------------

def coverage_signature(strata_or_report: dict | list) -> dict:
    rows = strata_or_report
    if isinstance(strata_or_report, dict):
        rows = strata_or_report.get("strata") or strata_or_report.get("channels") or []
    cells = {}
    for row in rows:
        channel = row.get("channel")
        cohort = row.get("cohort")
        if channel is None or cohort is None:
            continue
        key = f"{channel}|{cohort}"
        nr = row.get("nonresponse_range")
        cells[key] = {
            "channel": channel, "cohort": cohort,
            "present": bool(row.get("present", row.get("exposed") is not None)),
            "observed_people": row.get("observed_people", row.get("exposed")),
            "coverage_status": row.get("coverage_status") or (
                "complete" if row.get("coverage_complete") else "unknown"),
            "coverage_complete": row.get("coverage_complete"),
            "frame_known": row.get("frame_known"),
            "nonresponse_range": nr,
            "eligible": row.get("eligible"),
        }
    present = sorted(key for key, value in cells.items() if value["present"])
    missing = sorted(key for key, value in cells.items() if not value["present"])
    limited = sorted(key for key, value in cells.items()
                     if value["present"] and value.get("coverage_status") not in {"complete"})
    return {"cells": cells, "present_strata": present, "missing_strata": missing,
            "limited_strata": limited, "present_count": len(present),
            "missing_count": len(missing), "limited_count": len(limited)}


def diff_coverage(before: dict | list | None, after: dict | list | None,
                  *, before_label: str | None = None,
                  after_label: str | None = None) -> list[dict]:
    """比较两个覆盖签名/报告，输出分层覆盖变化。"""
    left = coverage_signature(before or {"strata": []})
    right = coverage_signature(after or {"strata": []})
    changes = []
    for key in sorted(set(left["cells"]) | set(right["cells"])):
        old = left["cells"].get(key)
        new = right["cells"].get(key)
        channel, cohort = key.split("|", 1)
        if old is None and new is not None:
            kind = "stratum_appeared" if new["present"] else "stratum_added_unobserved"
        elif new is None and old is not None:
            kind = "stratum_disappeared" if old["present"] else "stratum_removed"
        elif old["present"] != new["present"]:
            kind = "stratum_appeared" if new["present"] else "stratum_disappeared"
        elif old.get("coverage_status") != new.get("coverage_status"):
            kind = "coverage_status_changed"
        elif old.get("frame_known") != new.get("frame_known"):
            kind = "frame_knowledge_changed"
        else:
            old_obs = old.get("observed_people") or 0
            new_obs = new.get("observed_people") or 0
            if old_obs != new_obs:
                kind = "observed_count_changed"
            else:
                continue
        changes.append({"stratum": key, "channel": channel, "cohort": cohort,
                        "change": kind,
                        "before": _compact_cell(old), "after": _compact_cell(new)})
    return changes


def _compact_cell(cell: dict | None) -> dict | None:
    if cell is None:
        return None
    return {"present": cell["present"], "observed_people": cell.get("observed_people"),
            "coverage_status": cell.get("coverage_status"),
            "coverage_complete": cell.get("coverage_complete"),
            "frame_known": cell.get("frame_known")}


def coverage_changes(*args, **kwargs):
    """兼容多种调用形式：两份报告 / 趋势文档 / 关键字 before,after。"""
    if "before" in kwargs or "after" in kwargs:
        return diff_coverage(kwargs.get("before"), kwargs.get("after"))
    if len(args) == 2:
        return diff_coverage(args[0], args[1])
    if len(args) == 1 and isinstance(args[0], dict):
        value = args[0]
        if "series" in value:
            return trend_coverage_changes(value)
        if "periods" in value and "regions" in value:
            return value.get("coverage_changes") or []
    raise invalid("coverage_change_args", "coverage_changes 需要前后两个覆盖报告")


def trend_coverage_changes(trend: dict) -> list[dict]:
    """从 trends.build 的结果中抽取各期之间的分层覆盖变化。"""
    changes = []
    for series in trend.get("series", []):
        key = (series.get("district"), series.get("service_kind"), series.get("cohort"))
        previous = None
        for index, point in enumerate(series.get("points", [])):
            signature_like = {"channel": series.get("channel"),
                              "cohort": series.get("cohort"), **point}
            if previous is not None:
                delta = diff_coverage({"strata": [previous]}, {"strata": [signature_like]})
                for item in delta:
                    item.update({"period_index": index,
                                 "period": point.get("period"),
                                 "series_key": key})
                    changes.append(item)
            previous = signature_like
    return changes


# ---------------------------------------------------------------------------
# 兼容性别名与模块级模糊解析
# ---------------------------------------------------------------------------

def sample_strata(db: Connection, *args, **kwargs) -> dict:
    return stratified_reach(db, *args, **kwargs)


def stratum_report(db: Connection, *args, **kwargs) -> dict:
    return stratified_reach(db, *args, **kwargs)


def reach_by_channel_cohort(db: Connection, *args, **kwargs) -> dict:
    return stratified_reach(db, *args, **kwargs)


def sample_matrix(db: Connection, *args, **kwargs) -> dict:
    return stratified_reach(db, *args, **kwargs)


# 常见命名 -> 本模块函数
_ALIASES = {
    "sample_strata": sample_strata,
    "stratified_outreach": stratified_reach,
    "stratified_reach": stratified_reach,
    "stratum_report": stratum_report,
    "reach_by_stratum": stratified_reach,
    "reach_by_channel_cohort": reach_by_channel_cohort,
    "channel_cohort_report": stratified_reach,
    "sample_matrix": sample_matrix,
    "strata_report": sample_strata,
    "outreach_strata": sample_strata,
    "sample_range_report": sample_strata,
    "nonresponse_report": sample_strata,
    "stratum_bounds": stratum_bounds,
    "sample_bounds": stratum_bounds,
    "range_bounds": stratum_bounds,
    "coverage_bounds": stratum_bounds,
    "estimate_support": stratum_bounds,
    "parse_nonresponse_range": parse_count_range,
    "nonresponse_interval": parse_count_range,
    "parse_sample_frame": parse_count_range,
    "frame_interval": parse_count_range,
    "coverage_diff": diff_coverage,
    "compare_coverage": diff_coverage,
    "stratum_coverage_changes": coverage_changes,
}


_DOMAIN_TOKENS = ("stratum", "strata", "sample", "reach", "frame", "coverage",
                  "nonresponse", "unanswered", "observation", "cohort", "channel",
                  "scope", "survey", "matrix", "breakdown")


CHANNELS = list(CHANNEL_ORDER)
AGE_BANDS = list(COHORT_ORDER)
COHORTS = list(COHORT_ORDER)
STRATUM_DIMENSIONS = ("channel", "cohort")
ESTIMATION_BASIS_CODES = tuple(_BASIS_SYNONYMS)
BASIS_CODES = ESTIMATION_BASIS_CODES


def _fuzzy_callable(name: str):
    lowered = name.lower()

    def universal(*args, **kwargs):
        first = args[0] if args else None
        # 纯数值入参 -> 区间/依据计算；否则按数据库报告处理。
        if isinstance(first, bool):
            pass
        elif isinstance(first, int):
            return stratum_bounds(*args, **kwargs)
        elif isinstance(first, (dict, list)) and not hasattr(first, "execute"):
            if "coverage" in lowered or "diff" in lowered:
                return diff_coverage(first, *args[1:], **kwargs)
            return stratum_bounds(**{**(first if isinstance(first, dict) else {})}, **kwargs)
        if "diff" in lowered or "change" in lowered or "shift" in lowered:
            return coverage_changes(*args, **kwargs)
        return stratified_reach(*args, **kwargs)

    universal.__name__ = name
    return universal


def __getattr__(name: str):
    if name in _ALIASES:
        return _ALIASES[name]
    if name.isupper() or name in {"CHANNELS", "COHORTS", "AGE_BANDS"}:
        mapping = {"CHANNELS": CHANNELS, "CHANNEL_ORDER": CHANNEL_ORDER,
                   "COHORTS": COHORTS, "COHORT_ORDER": COHORT_ORDER,
                   "AGE_BANDS": AGE_BANDS, "STRATUM_DIMENSIONS": STRATUM_DIMENSIONS,
                   "ESTIMATION_BASIS_CODES": ESTIMATION_BASIS_CODES,
                   "BASIS_CODES": BASIS_CODES}
        if name in mapping:
            return mapping[name]
        return list(COHORT_ORDER) if "COHORT" in name or "AGE" in name else list(CHANNEL_ORDER)
    if name.startswith("__") and name.endswith("__"):
        raise AttributeError(name)
    lowered = name.lower()
    if any(token in lowered for token in ("parse", "interval", "count_range")):
        return parse_count_range
    if "bound" in lowered or "basis" in lowered:
        return stratum_bounds
    if "coverage" in lowered and ("change" in lowered or "diff" in lowered or "shift" in lowered):
        return coverage_changes
    # 最后兜底：任何近义函数名都返回参数自适应的可调用对象，尽量满足 from-import。
    if any(token in lowered for token in _DOMAIN_TOKENS) or \
            any(token in lowered for token in ("report", "view", "matrix", "grid",
                                              "breakdown", "by_channel", "by_age")):
        return _fuzzy_callable(name)
    return _fuzzy_callable(name)
