"""用量采信与套餐额度目录：整月用量 + 套餐包含额度/超额单价。

两条边界：
- 用量文件只说「这个订阅本月用了多少」，没有时间戳；按段分摊由 M5 负责；
- 额度目录可以随样例目录提供 ``plans.csv``，也可以用内置目录，来源写进 ``plans.source``。
"""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from ... import config
from ...reasons import level_of
from ..m1_ingest import columns as column_mod
from ..m1_ingest import reader
from . import quarantine, rules

USAGE_REQUIRED = column_mod.USAGE_REQUIRED
PLAN_REQUIRED = config.PLAN_CATALOG_REQUIRED


def _entry(file: str, row_no: int, code: str, *, field: str = "", raw: str = "",
           detail: str = "") -> dict:
    return quarantine.entry(file=file, row_no=row_no, code=code, field=field,
                            raw_value=raw, detail=detail)


def _signature(record: dict) -> tuple:
    return (str(record["units"]), record["metric"])


def normalize_usage(table) -> tuple[list[dict], list[dict], dict]:
    """用量表归一：必需列、列数、订阅号、用量格式、重复键。"""
    mapping = column_mod.usage_header(table.header)
    counts = {
        "rows": table.data_rows,
        "kept": 0,
        "quarantined_rows": 0,
        "dropped_dup": 0,
        "blank_rows": table.blank_rows,
        "repeated_headers": table.repeated_headers,
        "truncated": table.truncated_rows,
    }
    entries: list[dict] = []
    if table.state_code:
        entries.append(_entry(table.name, 0, table.state_code,
                              detail=f"编码 {table.encoding}，字节 {table.size}"))
        counts["quarantined_rows"] = len(table.rows) + table.truncated_rows
        return [], entries, counts
    missing = column_mod.required_missing(mapping, USAGE_REQUIRED)
    if missing:
        entries.append(_entry(table.name, 0, "R_HEADER_MISSING",
                              field=",".join(missing), raw=",".join(table.header)))
        counts["quarantined_rows"] = len(table.rows) + table.truncated_rows
        return [], entries, counts
    if mapping.unknown:
        entries.append(_entry(table.name, 0, "R_HEADER_UNKNOWN",
                              field=",".join(mapping.unknown), detail="未登记列已忽略"))
    if table.repeated_headers:
        entries.append(_entry(table.name, 0, "R_HEADER_REPEAT",
                              detail=f"重复表头 {table.repeated_headers} 行已剔除"))
    if table.truncated_rows:
        entries.append(_entry(table.name, table.truncated_row_no, "R_ROW_TRUNCATED",
                              detail="引号未闭合，半行不参与计算"))
    if table.blank_rows and table.rows:
        entries.append(_entry(table.name, 0, "R_BLANK_ROWS",
                              detail=f"空白行 {table.blank_rows} 行"))

    header_len = len(table.header)
    records: list[dict] = []
    seen: dict[str, dict] = {}
    error_rows: set[int] = set()

    def reject(row_no: int, code: str, field: str = "", raw: str = "", detail: str = ""):
        entries.append(_entry(table.name, row_no, code, field=field, raw=raw,
                              detail=detail))
        error_rows.add(row_no)

    for row in table.rows:
        if not column_mod.column_count_ok(row.fields, header_len):
            reject(row.row_no, "R_COLUMN_COUNT", field="列数",
                   raw=f"{len(row.fields)}列/表头{header_len}列")
            continue
        sub_id = mapping.value(row.fields, "sub_id").strip()
        if not sub_id:
            reject(row.row_no, "R_KEY_MISSING", field="订阅号", raw=row.raw)
            continue
        parsed = rules.normalize_units(mapping.value(row.fields, "units"))
        if parsed.code:
            reject(row.row_no, parsed.code, field="用量",
                   raw=mapping.value(row.fields, "units"))
            continue
        record = {
            "sub_id": sub_id,
            "metric": mapping.value(row.fields, "metric").strip(),
            "units": parsed.value,
            "units_exact": parsed.value,
            "source_file": table.name,
            "row_no": row.row_no,
            "flags": list(parsed.flags),
        }
        if sub_id in seen:
            kept = seen[sub_id]
            if _signature(kept) == _signature(record):
                entries.append(_entry(table.name, row.row_no, "R_DUP_USAGE_ROW",
                                      field="订阅号", raw=sub_id,
                                      detail=f"与第 {kept['row_no']} 行完全相同"))
                counts["dropped_dup"] += 1
            else:
                entries.append(_entry(table.name, row.row_no, "R_DUP_USAGE_KEY",
                                      field="订阅号", raw=sub_id,
                                      detail=f"与第 {kept['row_no']} 行取值冲突，保留前者"))
                error_rows.add(row.row_no)
            continue
        seen[sub_id] = record
        records.append(record)

    counts["kept"] = len(records)
    counts["quarantined_rows"] = len(error_rows)
    return records, entries, counts


def load_plan_catalog(sample_dir: str) -> tuple[list[dict], str]:
    """套餐额度目录：样例目录有 plans.csv 就用它，否则用内置目录。"""
    path = Path(sample_dir) / "plans.csv"
    if not path.is_file():
        return ([{"plan": plan, "metric": metric, "included": str(included),
                  "unit_price": str(price), "source": "builtin"}
                 for plan, (metric, included, price) in sorted(
                     config.DEFAULT_PLAN_CATALOG.items())], "builtin")
    table = reader.parse_table("plans.csv", reader.read_bytes(path))
    mapping = column_mod.plan_header(table.header)
    if table.state_code or column_mod.required_missing(mapping, PLAN_REQUIRED):
        return ([{"plan": plan, "metric": metric, "included": str(included),
                  "unit_price": str(price), "source": "builtin"}
                 for plan, (metric, included, price) in sorted(
                     config.DEFAULT_PLAN_CATALOG.items())], "builtin")
    rows: list[dict] = []
    header_len = len(table.header)
    for row in table.rows:
        if not column_mod.column_count_ok(row.fields, header_len):
            continue
        plan = mapping.value(row.fields, "plan").strip()
        included = rules.normalize_units(mapping.value(row.fields, "included"))
        price = rules.normalize_money(mapping.value(row.fields, "unit_price"))
        if not plan or included.value is None or price.value is None:
            continue
        rows.append({
            "plan": plan,
            "metric": mapping.value(row.fields, "metric").strip() or "GB",
            "included": str(included.value),
            "unit_price": str(price.value),
            "source": "file",
        })
    if not rows:
        return ([{"plan": plan, "metric": metric, "included": str(included),
                  "unit_price": str(price), "source": "builtin"}
                 for plan, (metric, included, price) in sorted(
                     config.DEFAULT_PLAN_CATALOG.items())], "builtin")
    return rows, "file"


def build_plans(items: list[dict]) -> list[dict]:
    return [{
        "plan": item["plan"],
        "metric": item.get("metric", ""),
        "included": str(Decimal(str(item["included"]))),
        "unit_price": str(Decimal(str(item["unit_price"]))),
        "source": item.get("source", "builtin"),
    } for item in items]


def annotate_unknown_subscriptions(records: list[dict], subs: list[dict]) -> list[dict]:
    """用量引用了不存在的订阅：不能静默算到别人账上，整行进隔离。"""
    known = {sub["sub_id"] for sub in subs}
    entries: list[dict] = []
    kept: list[dict] = []
    for record in records:
        if record["sub_id"] in known:
            kept.append(record)
        else:
            entries.append(_entry(record["source_file"], record["row_no"],
                                  "R_USAGE_UNKNOWN_SUB", field="订阅号",
                                  raw=record["sub_id"]))
    return kept, entries


def merge_entries(*groups: list[dict]) -> list[dict]:
    return sorted((item for group in groups for item in group),
                  key=lambda item: (item["file"], item["row_no"], item["code"]))


def count_error_rows(entries: list[dict]) -> int:
    """行数账的「隔离行」：只数资源行，文件级条目（行号 0）不计行。"""
    return sum(1 for item in entries
               if level_of(item["code"]) == "error" and int(item["row_no"]) > 0)
