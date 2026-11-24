from __future__ import annotations

import csv
import hashlib
import io
import json
from datetime import datetime

from silverwatch.errors import invalid
from silverwatch.intake import validate_bundle
from silverwatch.ndjson import MAX_BYTES, MAX_RECORDS


def decode(text: str, *, kind: str, batch_id: str, source_ref: str,
           revision: int, created_at: datetime | None = None) -> dict:
    raw = text.encode("utf-8")
    if len(raw) > MAX_BYTES:
        raise invalid("csv_size", "CSV 输入超过 2 MB")
    stream = io.StringIO(text.lstrip("\ufeff"), newline="")
    reader = csv.DictReader(stream, strict=True)
    if not reader.fieldnames:
        raise invalid("csv_header", "CSV 必须包含字段标题行")
    headers = reader.fieldnames
    if any(not key or key.strip() != key for key in headers):
        raise invalid("csv_header", "CSV 字段名不得为空或包含首尾空格")
    if len(set(headers)) != len(headers):
        raise invalid("csv_header_duplicate", "CSV 字段标题不能重复")
    records = []
    physical_lines = []
    errors = []
    try:
        for number, row in enumerate(reader, 2):
            if None in row or any(value is None for value in row.values()):
                errors.append({"line": number, "code": "csv_column_count"})
                continue
            if not any((value or "").strip() for value in row.values()):
                continue
            records.append({"kind": kind, "data": {key: value if value != "" else None
                                                       for key, value in row.items()}})
            physical_lines.append(number)
            if len(records) > MAX_RECORDS:
                raise invalid("csv_records", "CSV 输入最多包含 500 条记录")
    except csv.Error as exc:
        raise invalid("csv_parse", "CSV 引号或分隔符格式无效", {"line": reader.line_num,
                                                                   "detail": str(exc)}) from exc
    if errors:
        raise invalid("csv_rows", "CSV 列数与标题不一致", {"errors": errors})
    bundle = {"batch_id": batch_id, "source_ref": source_ref, "revision": revision,
              "records": records, "created_at": created_at.isoformat() if created_at else None}
    checked = validate_bundle(bundle)
    if not checked["valid"]:
        by_position = {item["position"]: item for item in checked["errors"]}
        raise invalid("csv_contract", "CSV 行未通过业务字段校验",
                      {"errors": [{**item, "line": physical_lines[position]}
                                  for position, item in sorted(by_position.items())]})
    canonical = json.dumps(bundle, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":")).encode("utf-8")
    return {**bundle, "record_count": len(records), "source_digest": hashlib.sha256(canonical).hexdigest(),
            "byte_count": len(raw), "header": headers, "validation": checked}


def encode(headers: list[str], rows: list[dict]) -> str:
    if not headers or len(set(headers)) != len(headers):
        raise invalid("csv_header", "CSV 导出标题必须非空且不能重复")
    if len(rows) > MAX_RECORDS:
        raise invalid("csv_records", "CSV 导出最多包含 500 行")
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=headers, extrasaction="raise", lineterminator="\n")
    writer.writeheader()
    for index, row in enumerate(rows):
        if set(row) - set(headers):
            raise invalid("csv_row_shape", "导出行包含标题未声明的字段", {"row": index})
        writer.writerow(row)
    return "\ufeff" + output.getvalue()
