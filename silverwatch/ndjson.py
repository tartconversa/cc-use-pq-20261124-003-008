from __future__ import annotations

import hashlib
import json
from datetime import datetime

from silverwatch.errors import Problem, invalid
from silverwatch.intake import validate_bundle

MAX_BYTES = 2_000_000
MAX_LINE_BYTES = 64_000
MAX_RECORDS = 500


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"重复 JSON 字段: {key}")
        value[key] = item
    return value


def decode(text: str, *, batch_id: str, source_ref: str, revision: int,
           created_at: datetime | None = None) -> dict:
    raw = text.encode("utf-8")
    if len(raw) > MAX_BYTES:
        raise invalid("ndjson_size", "NDJSON 输入超过 2 MB")
    lines = text.lstrip("\ufeff").splitlines()
    records = []
    line_map = []
    failures = []
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        if len(line.encode("utf-8")) > MAX_LINE_BYTES:
            failures.append({"line": number, "code": "line_too_large"})
            continue
        try:
            value = json.loads(line, object_pairs_hook=_unique_object)
        except (json.JSONDecodeError, ValueError) as exc:
            failures.append({"line": number, "code": "invalid_json", "detail": str(exc)})
            continue
        if not isinstance(value, dict) or set(value) != {"kind", "data"}:
            failures.append({"line": number, "code": "record_envelope"})
            continue
        records.append(value)
        line_map.append(number)
        if len(records) > MAX_RECORDS:
            raise invalid("ndjson_records", "NDJSON 输入最多包含 500 条有效记录")
    if failures:
        raise invalid("ndjson_parse", "NDJSON 有无法解析的记录", {"errors": failures})
    bundle = {"batch_id": batch_id, "source_ref": source_ref, "revision": revision,
              "records": records,
              "created_at": created_at.isoformat() if created_at else None}
    validated = validate_bundle(bundle)
    if not validated["valid"]:
        lookup = {item["position"]: item for item in validated["errors"]}
        errors = [{**lookup[index], "line": line_map[index]}
                  for index in sorted(lookup)]
        raise invalid("ndjson_contract", "NDJSON 記錄未通過業務契約", {"errors": errors})
    canonical = json.dumps(bundle, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":")).encode("utf-8")
    return {**bundle, "record_count": len(records),
            "source_digest": hashlib.sha256(canonical).hexdigest(),
            "byte_count": len(raw), "line_count": len(lines),
            "validation": validated}


def encode(records: list[dict]) -> str:
    if len(records) > MAX_RECORDS:
        raise invalid("ndjson_records", "NDJSON 导出最多包含 500 条记录")
    lines = []
    for position, record in enumerate(records):
        if not isinstance(record, dict) or set(record) != {"kind", "data"}:
            raise invalid("record_envelope", "导出记录格式无效", {"position": position})
        lines.append(json.dumps(record, ensure_ascii=False, sort_keys=True,
                                separators=(",", ":")))
    return "\n".join(lines) + ("\n" if lines else "")


def verify_digest(bundle: dict) -> bool:
    body = {key: bundle[key] for key in ("batch_id", "source_ref", "revision", "records", "created_at")
            if key in bundle}
    calculated = hashlib.sha256(json.dumps(body, ensure_ascii=False, sort_keys=True,
                                           separators=(",", ":")).encode("utf-8")).hexdigest()
    return calculated == bundle.get("source_digest")
