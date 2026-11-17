"""SQLite 存储：批次、输入摘要、订阅、变更、隔离、分段、抵扣行、账单、对账、清单。

约定：
- 金额列一律 TEXT（Decimal 字符串）加一份「分」整数，不落 float；
- 一个批次的所有派生行在同一个事务里「先删后插」，失败整体回滚，不留半批；
- 页面只读本模块查询结果，不在模板里做业务计算。
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    batch_key TEXT NOT NULL,
    status TEXT NOT NULL,
    source TEXT NOT NULL,
    rule_version TEXT NOT NULL,
    created_at TEXT NOT NULL,
    finished_at TEXT,
    input_digest TEXT NOT NULL DEFAULT '',
    params_json TEXT NOT NULL DEFAULT '{}',
    counters_json TEXT NOT NULL DEFAULT '{}',
    note TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_runs_key ON runs(batch_key, status);

CREATE TABLE IF NOT EXISTS inputs (
    run_id TEXT NOT NULL,
    name TEXT NOT NULL,
    encoding TEXT NOT NULL,
    bytes INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    rows INTEGER NOT NULL,
    header_json TEXT NOT NULL DEFAULT '[]',
    note TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS subscriptions (
    run_id TEXT NOT NULL,
    sub_id TEXT NOT NULL,
    account TEXT NOT NULL DEFAULT '',
    plan TEXT NOT NULL DEFAULT '',
    price_text TEXT NOT NULL DEFAULT '',
    monthly TEXT NOT NULL DEFAULT '',
    period_start TEXT NOT NULL,
    period_end TEXT NOT NULL,
    tz TEXT NOT NULL,
    state TEXT NOT NULL,
    period_days INTEGER NOT NULL DEFAULT 0,
    leap INTEGER NOT NULL DEFAULT 0,
    source_file TEXT NOT NULL DEFAULT '',
    row_no INTEGER NOT NULL DEFAULT 0,
    flags_json TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS changes (
    run_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    change_id TEXT NOT NULL,
    sub_id TEXT NOT NULL,
    effective_utc TEXT NOT NULL,
    effective_local TEXT NOT NULL,
    local_date TEXT NOT NULL,
    kind TEXT NOT NULL,
    new_plan TEXT NOT NULL DEFAULT '',
    new_price_text TEXT NOT NULL DEFAULT '',
    new_price TEXT NOT NULL DEFAULT '',
    source_file TEXT NOT NULL DEFAULT '',
    row_no INTEGER NOT NULL DEFAULT 0,
    applied INTEGER NOT NULL DEFAULT 1,
    superseded_by TEXT NOT NULL DEFAULT '',
    flags_json TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS quarantine (
    run_id TEXT NOT NULL,
    file TEXT NOT NULL,
    row_no INTEGER NOT NULL,
    code TEXT NOT NULL,
    level TEXT NOT NULL,
    field TEXT NOT NULL DEFAULT '',
    raw_value TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_quarantine_run ON quarantine(run_id, code);

CREATE TABLE IF NOT EXISTS segments (
    run_id TEXT NOT NULL,
    invoice_id TEXT NOT NULL,
    sub_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    start_date TEXT NOT NULL,
    end_date TEXT NOT NULL,
    days INTEGER NOT NULL,
    plan TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT 'active',
    monthly TEXT NOT NULL DEFAULT '',
    metric TEXT NOT NULL DEFAULT '',
    included_units TEXT NOT NULL DEFAULT '0',
    allocated_units TEXT NOT NULL DEFAULT '0',
    overage_units TEXT NOT NULL DEFAULT '0',
    overage_cents INTEGER NOT NULL DEFAULT 0,
    amount_exact TEXT NOT NULL DEFAULT '0',
    amount_cents INTEGER NOT NULL DEFAULT 0,
    flags_json TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS components (
    run_id TEXT NOT NULL,
    invoice_id TEXT NOT NULL,
    sub_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    kind TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT '',
    label TEXT NOT NULL DEFAULT '',
    days INTEGER NOT NULL DEFAULT 0,
    monthly TEXT NOT NULL DEFAULT '',
    exact_text TEXT NOT NULL DEFAULT '0',
    cents INTEGER NOT NULL DEFAULT 0,
    flags_json TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS invoices (
    run_id TEXT NOT NULL,
    invoice_id TEXT NOT NULL,
    sub_id TEXT NOT NULL,
    account TEXT NOT NULL DEFAULT '',
    period_start TEXT NOT NULL,
    period_end TEXT NOT NULL,
    period_days INTEGER NOT NULL,
    billed_days INTEGER NOT NULL,
    free_days INTEGER NOT NULL,
    charge_cents INTEGER NOT NULL DEFAULT 0,
    credit_cents INTEGER NOT NULL DEFAULT 0,
    overage_exact TEXT NOT NULL DEFAULT '0',
    overage_cents INTEGER NOT NULL DEFAULT 0,
    total_cents INTEGER NOT NULL DEFAULT 0,
    exact_total TEXT NOT NULL DEFAULT '0',
    overage_total_exact TEXT NOT NULL DEFAULT '0',
    status TEXT NOT NULL DEFAULT 'ok',
    flags_json TEXT NOT NULL DEFAULT '[]',
    source_row INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS plans (
    run_id TEXT NOT NULL,
    plan TEXT NOT NULL,
    metric TEXT NOT NULL DEFAULT '',
    included TEXT NOT NULL DEFAULT '0',
    unit_price TEXT NOT NULL DEFAULT '0',
    source TEXT NOT NULL DEFAULT 'catalog'
);

CREATE TABLE IF NOT EXISTS usage_rows (
    run_id TEXT NOT NULL,
    sub_id TEXT NOT NULL,
    metric TEXT NOT NULL DEFAULT '',
    units TEXT NOT NULL DEFAULT '0',
    units_exact TEXT NOT NULL DEFAULT '0',
    source_file TEXT NOT NULL DEFAULT '',
    row_no INTEGER NOT NULL DEFAULT 0,
    flags_json TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS usage_files (
    run_id TEXT NOT NULL,
    name TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    encoding TEXT NOT NULL DEFAULT '',
    bytes INTEGER NOT NULL DEFAULT 0,
    rows INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'done'
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_usage_file ON usage_files(run_id, sha256);

CREATE TABLE IF NOT EXISTS overages (
    run_id TEXT NOT NULL,
    invoice_id TEXT NOT NULL,
    sub_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    segment_seq INTEGER NOT NULL DEFAULT 0,
    plan TEXT NOT NULL DEFAULT '',
    metric TEXT NOT NULL DEFAULT '',
    days INTEGER NOT NULL DEFAULT 0,
    included_units TEXT NOT NULL DEFAULT '0',
    allocated_units TEXT NOT NULL DEFAULT '0',
    overage_units TEXT NOT NULL DEFAULT '0',
    unit_price TEXT NOT NULL DEFAULT '0',
    exact_text TEXT NOT NULL DEFAULT '0',
    cents INTEGER NOT NULL DEFAULT 0,
    flags_json TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS stages (
    run_id TEXT NOT NULL,
    stage TEXT NOT NULL,
    in_count INTEGER NOT NULL DEFAULT 0,
    out_count INTEGER NOT NULL DEFAULT 0,
    quarantine_count INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'done',
    note TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS totals (
    run_id TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reconciliations (
    run_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    ledger TEXT NOT NULL,
    label TEXT NOT NULL,
    left_text TEXT NOT NULL,
    right_text TEXT NOT NULL,
    diff_text TEXT NOT NULL,
    status TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS manifests (
    run_id TEXT PRIMARY KEY,
    json TEXT NOT NULL
);
"""


def _loads(value: str, fallback):
    try:
        return json.loads(value) if value else fallback
    except (TypeError, ValueError):
        return fallback


class Store:
    def __init__(self, path: str):
        self.path = str(path)
        Path(self.path).resolve().parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            self._migrate(conn)

    @staticmethod
    def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
        return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}

    def _migrate(self, conn: sqlite3.Connection) -> None:
        """老库就地补列：只加不删，已有行用默认值，不重算历史事实。"""
        wanted = {
            "segments": {
                "metric": "TEXT NOT NULL DEFAULT ''",
                "included_units": "TEXT NOT NULL DEFAULT '0'",
                "allocated_units": "TEXT NOT NULL DEFAULT '0'",
                "overage_units": "TEXT NOT NULL DEFAULT '0'",
                "overage_cents": "INTEGER NOT NULL DEFAULT 0",
            },
            "invoices": {
                "overage_exact": "TEXT NOT NULL DEFAULT '0'",
                "overage_cents": "INTEGER NOT NULL DEFAULT 0",
                "overage_total_exact": "TEXT NOT NULL DEFAULT '0'",
            },
        }
        for table, columns in wanted.items():
            existing = self._columns(conn, table)
            for name, decl in columns.items():
                if name not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=15.0)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ---------- 批次 ----------
    def create_run(self, run: dict) -> None:
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO runs (run_id, batch_key, status, source, rule_version,
                        created_at, finished_at, input_digest, params_json, counters_json, note)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(run_id) DO UPDATE SET
                     status=excluded.status, finished_at=excluded.finished_at,
                     input_digest=excluded.input_digest,
                     counters_json=excluded.counters_json""",
                (run["run_id"], run["batch_key"], run["status"], run["source"],
                 run["rule_version"], run["created_at"], run.get("finished_at"),
                 run.get("input_digest", ""),
                 json.dumps(run.get("params", {}), ensure_ascii=False),
                 json.dumps(run.get("counters", {}), ensure_ascii=False),
                 run.get("note", "")),
            )

    def update_run(self, run_id: str, **fields) -> None:
        allowed = {"status", "finished_at", "input_digest", "counters_json", "note"}
        sets, values = [], []
        for key, value in fields.items():
            if key not in allowed:
                continue
            if key == "counters_json" and not isinstance(value, str):
                value = json.dumps(value, ensure_ascii=False)
            sets.append(f"{key}=?")
            values.append(value)
        if not sets:
            return
        values.append(run_id)
        with self.connect() as conn:
            conn.execute(f"UPDATE runs SET {', '.join(sets)} WHERE run_id=?", values)

    def get_run(self, run_id: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        return self._run_row(row) if row else None

    def find_run_by_key(self, batch_key: str, status: str = "done") -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM runs WHERE batch_key=? AND status=? ORDER BY created_at LIMIT 1",
                (batch_key, status)).fetchone()
        return self._run_row(row) if row else None

    def latest_run(self, status: str | None = None) -> dict | None:
        with self.connect() as conn:
            if status:
                row = conn.execute(
                    "SELECT * FROM runs WHERE status=? ORDER BY created_at DESC, rowid DESC "
                    "LIMIT 1", (status,)).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM runs ORDER BY created_at DESC, rowid DESC LIMIT 1"
                ).fetchone()
        return self._run_row(row) if row else None

    def list_runs(self, limit: int = 30) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM runs ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (limit,)).fetchall()
        return [self._run_row(row) for row in rows]

    @staticmethod
    def _run_row(row: sqlite3.Row) -> dict:
        data = dict(row)
        data["params"] = _loads(data.pop("params_json", "{}"), {})
        data["counters"] = _loads(data.pop("counters_json", "{}"), {})
        return data

    # ---------- 输入摘要 ----------
    def put_inputs(self, run_id: str, items: list[dict]) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM inputs WHERE run_id=?", (run_id,))
            conn.executemany(
                """INSERT INTO inputs (run_id, name, encoding, bytes, sha256, rows,
                        header_json, note) VALUES (?,?,?,?,?,?,?,?)""",
                [(run_id, item["name"], item["encoding"], int(item["bytes"]),
                  item["sha256"], int(item["rows"]),
                  json.dumps(item.get("header", []), ensure_ascii=False),
                  item.get("note", "")) for item in items],
            )

    def list_inputs(self, run_id: str) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM inputs WHERE run_id=? ORDER BY name", (run_id,)).fetchall()
        out = []
        for row in rows:
            data = dict(row)
            data["header"] = _loads(data.pop("header_json", "[]"), [])
            out.append(data)
        return out

    # ---------- 订阅 ----------
    def put_subscriptions(self, run_id: str, items: list[dict]) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM subscriptions WHERE run_id=?", (run_id,))
            conn.executemany(
                """INSERT INTO subscriptions (run_id, sub_id, account, plan, price_text,
                        monthly, period_start, period_end, tz, state, period_days, leap,
                        source_file, row_no, flags_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                [(run_id, item["sub_id"], item["account"], item["plan"],
                  item["price_text"], str(item["monthly"]), item["period_start"],
                  item["period_end"], item["tz"], item["state"], int(item["period_days"]),
                  int(item["leap"]), item.get("source_file", ""), int(item.get("row_no", 0)),
                  json.dumps(item.get("flags", []), ensure_ascii=False)) for item in items],
            )

    def list_subscriptions(self, run_id: str) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM subscriptions WHERE run_id=? ORDER BY sub_id", (run_id,)
            ).fetchall()
        out = []
        for row in rows:
            data = dict(row)
            data["flags"] = _loads(data.pop("flags_json", "[]"), [])
            out.append(data)
        return out

    # ---------- 变更 ----------
    def put_changes(self, run_id: str, items: list[dict]) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM changes WHERE run_id=?", (run_id,))
            conn.executemany(
                """INSERT INTO changes (run_id, seq, change_id, sub_id, effective_utc,
                        effective_local, local_date, kind, new_plan, new_price_text,
                        new_price, source_file, row_no, applied, superseded_by, flags_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                [(run_id, int(item["seq"]), item["change_id"], item["sub_id"],
                  item["effective_utc"], item["effective_local"], item["local_date"],
                  item["kind"], item.get("new_plan", ""), item.get("new_price_text", ""),
                  str(item.get("new_price", "")), item.get("source_file", ""),
                  int(item.get("row_no", 0)), int(item.get("applied", 1)),
                  item.get("superseded_by", ""),
                  json.dumps(item.get("flags", []), ensure_ascii=False)) for item in items],
            )

    def list_changes(self, run_id: str) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM changes WHERE run_id=? ORDER BY seq", (run_id,)).fetchall()
        out = []
        for row in rows:
            data = dict(row)
            data["flags"] = _loads(data.pop("flags_json", "[]"), [])
            data["applied"] = bool(data["applied"])
            out.append(data)
        return out

    # ---------- 隔离 ----------
    def put_quarantine(self, run_id: str, items: list[dict]) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM quarantine WHERE run_id=?", (run_id,))
            conn.executemany(
                """INSERT INTO quarantine (run_id, file, row_no, code, level, field,
                        raw_value, detail) VALUES (?,?,?,?,?,?,?,?)""",
                [(run_id, item["file"], int(item["row_no"]), item["code"], item["level"],
                  item.get("field", ""), item.get("raw_value", ""),
                  item.get("detail", "")) for item in items],
            )

    def list_quarantine(self, run_id: str, limit: int | None = None) -> list[dict]:
        sql = ("SELECT * FROM quarantine WHERE run_id=? ORDER BY file, row_no, code")
        if limit:
            sql += f" LIMIT {int(limit)}"
        with self.connect() as conn:
            rows = conn.execute(sql, (run_id,)).fetchall()
        return [dict(row) for row in rows]

    # ---------- 分段 ----------
    def put_segments(self, run_id: str, items: list[dict]) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM segments WHERE run_id=?", (run_id,))
            conn.executemany(
                """INSERT INTO segments (run_id, invoice_id, sub_id, seq, start_date,
                        end_date, days, plan, state, monthly, metric, included_units,
                        allocated_units, overage_units, overage_cents, amount_exact,
                        amount_cents, flags_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                [(run_id, item["invoice_id"], item["sub_id"], int(item["seq"]),
                  item["start_date"], item["end_date"], int(item["days"]), item["plan"],
                  item["state"], str(item.get("monthly", "")),
                  item.get("metric", ""), str(item.get("included_units", "0")),
                  str(item.get("allocated_units", "0")),
                  str(item.get("overage_units", "0")),
                  int(item.get("overage_cents", 0)),
                  str(item.get("amount_exact", "0")), int(item.get("amount_cents", 0)),
                  json.dumps(item.get("flags", []), ensure_ascii=False)) for item in items],
            )

    def list_segments(self, run_id: str, invoice_id: str | None = None) -> list[dict]:
        with self.connect() as conn:
            if invoice_id:
                rows = conn.execute(
                    "SELECT * FROM segments WHERE run_id=? AND invoice_id=? "
                    "ORDER BY invoice_id, seq", (run_id, invoice_id)).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM segments WHERE run_id=? ORDER BY invoice_id, seq",
                    (run_id,)).fetchall()
        out = []
        for row in rows:
            data = dict(row)
            data["flags"] = _loads(data.pop("flags_json", "[]"), [])
            out.append(data)
        return out

    def update_segment_amounts(self, run_id: str, items: list[dict]) -> None:
        with self.connect() as conn:
            conn.executemany(
                "UPDATE segments SET amount_exact=?, amount_cents=? "
                "WHERE run_id=? AND invoice_id=? AND seq=?",
                [(str(item["amount_exact"]), int(item["amount_cents"]), run_id,
                  item["invoice_id"], int(item["seq"])) for item in items],
            )

    def update_segment_overage_cents(self, run_id: str, items: list[dict]) -> None:
        with self.connect() as conn:
            conn.executemany(
                "UPDATE segments SET overage_cents=? "
                "WHERE run_id=? AND invoice_id=? AND seq=?",
                [(int(item["overage_cents"]), run_id, item["invoice_id"], int(item["seq"]))
                 for item in items],
            )

    # ---------- 抵扣行 ----------
    def put_components(self, run_id: str, items: list[dict]) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM components WHERE run_id=?", (run_id,))
            conn.executemany(
                """INSERT INTO components (run_id, invoice_id, sub_id, seq, kind, source,
                        label, days, monthly, exact_text, cents, flags_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                [(run_id, item["invoice_id"], item["sub_id"], int(item["seq"]),
                  item["kind"], item.get("source", ""), item.get("label", ""),
                  int(item.get("days", 0)), str(item.get("monthly", "")),
                  str(item["exact"]), int(item["cents"]),
                  json.dumps(item.get("flags", []), ensure_ascii=False)) for item in items],
            )

    def list_components(self, run_id: str, invoice_id: str | None = None) -> list[dict]:
        with self.connect() as conn:
            if invoice_id:
                rows = conn.execute(
                    "SELECT * FROM components WHERE run_id=? AND invoice_id=? "
                    "ORDER BY invoice_id, seq", (run_id, invoice_id)).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM components WHERE run_id=? ORDER BY invoice_id, seq",
                    (run_id,)).fetchall()
        out = []
        for row in rows:
            data = dict(row)
            data["flags"] = _loads(data.pop("flags_json", "[]"), [])
            out.append(data)
        return out

    # ---------- 账单 ----------
    def put_invoices(self, run_id: str, items: list[dict]) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM invoices WHERE run_id=?", (run_id,))
            conn.executemany(
                """INSERT INTO invoices (run_id, invoice_id, sub_id, account, period_start,
                        period_end, period_days, billed_days, free_days, charge_cents,
                        credit_cents, overage_exact, overage_cents, total_cents,
                        exact_total, overage_total_exact, status, flags_json, source_row)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                [(run_id, item["invoice_id"], item["sub_id"], item.get("account", ""),
                  item["period_start"], item["period_end"], int(item["period_days"]),
                  int(item["billed_days"]), int(item["free_days"]),
                  int(item["charge_cents"]), int(item["credit_cents"]),
                  str(item.get("overage_exact", "0")),
                  int(item.get("overage_cents", 0)),
                  int(item["total_cents"]), str(item.get("exact_total", "0")),
                  str(item.get("overage_total_exact", "0")),
                  item.get("status", "ok"),
                  json.dumps(item.get("flags", []), ensure_ascii=False),
                  int(item.get("source_row", 0))) for item in items],
            )

    def list_invoices(self, run_id: str) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM invoices WHERE run_id=? ORDER BY invoice_id", (run_id,)
            ).fetchall()
        out = []
        for row in rows:
            data = dict(row)
            data["flags"] = _loads(data.pop("flags_json", "[]"), [])
            out.append(data)
        return out

    def get_invoice(self, run_id: str, invoice_id: str) -> dict | None:
        for invoice in self.list_invoices(run_id):
            if invoice["invoice_id"] == invoice_id:
                return invoice
        return None

    # ---------- 阶段 / 合计 / 对账 ----------
    def put_stages(self, run_id: str, items: list[dict]) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM stages WHERE run_id=?", (run_id,))
            conn.executemany(
                """INSERT INTO stages (run_id, stage, in_count, out_count,
                        quarantine_count, status, note) VALUES (?,?,?,?,?,?,?)""",
                [(run_id, item["stage"], int(item["in_count"]), int(item["out_count"]),
                  int(item["quarantine_count"]), item.get("status", "done"),
                  item.get("note", "")) for item in items],
            )

    def list_stages(self, run_id: str) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM stages WHERE run_id=? ORDER BY rowid", (run_id,)).fetchall()
        return [dict(row) for row in rows]

    def put_totals(self, run_id: str, totals: dict) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM totals WHERE run_id=?", (run_id,))
            conn.executemany(
                "INSERT INTO totals (run_id, key, value) VALUES (?,?,?)",
                [(run_id, key, str(value)) for key, value in sorted(totals.items())],
            )

    def list_totals(self, run_id: str) -> dict:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT key, value FROM totals WHERE run_id=? ORDER BY key", (run_id,)
            ).fetchall()
        return {row["key"]: row["value"] for row in rows}

    def put_reconciliations(self, run_id: str, items: list[dict]) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM reconciliations WHERE run_id=?", (run_id,))
            conn.executemany(
                """INSERT INTO reconciliations (run_id, seq, ledger, label, left_text,
                        right_text, diff_text, status, note) VALUES (?,?,?,?,?,?,?,?,?)""",
                [(run_id, int(item["seq"]), item["ledger"], item["label"],
                  item["left_text"], item["right_text"], item["diff_text"],
                  item["status"], item.get("note", "")) for item in items],
            )

    def list_reconciliations(self, run_id: str) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM reconciliations WHERE run_id=? ORDER BY seq", (run_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    # ---------- 清单 ----------
    def put_manifest(self, run_id: str, data: dict) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO manifests (run_id, json) VALUES (?,?) "
                "ON CONFLICT(run_id) DO UPDATE SET json=excluded.json",
                (run_id, json.dumps(data, ensure_ascii=False)),
            )

    def get_manifest(self, run_id: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT json FROM manifests WHERE run_id=?", (run_id,)).fetchone()
        return _loads(row["json"], {}) if row else None

    # ---------- 套餐额度目录 ----------
    def put_plans(self, run_id: str, items: list[dict]) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM plans WHERE run_id=?", (run_id,))
            conn.executemany(
                """INSERT INTO plans (run_id, plan, metric, included, unit_price, source)
                   VALUES (?,?,?,?,?,?)""",
                [(run_id, item["plan"], item.get("metric", ""),
                  str(item["included"]), str(item["unit_price"]),
                  item.get("source", "catalog")) for item in items],
            )

    def list_plans(self, run_id: str) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM plans WHERE run_id=? ORDER BY plan", (run_id,)).fetchall()
        return [dict(row) for row in rows]

    # ---------- 用量 ----------
    def get_usage_file(self, run_id: str, sha256: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM usage_files WHERE run_id=? AND sha256=?",
                (run_id, sha256)).fetchone()
        return dict(row) if row else None

    def list_usage_files(self, run_id: str) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM usage_files WHERE run_id=? ORDER BY name, sha256",
                (run_id,)).fetchall()
        return [dict(row) for row in rows]

    def add_usage_file(self, run_id: str, item: dict) -> None:
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO usage_files (run_id, name, sha256, encoding, bytes, rows,
                        status) VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(run_id, sha256) DO NOTHING""",
                (run_id, item["name"], item["sha256"], item.get("encoding", ""),
                 int(item.get("bytes", 0)), int(item.get("rows", 0)),
                 item.get("status", "done")),
            )

    def replace_usage_rows(self, run_id: str, items: list[dict]) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM usage_rows WHERE run_id=?", (run_id,))
            conn.executemany(
                """INSERT INTO usage_rows (run_id, sub_id, metric, units, units_exact,
                        source_file, row_no, flags_json) VALUES (?,?,?,?,?,?,?,?)""",
                [(run_id, item["sub_id"], item.get("metric", ""), str(item["units"]),
                  str(item.get("units_exact", item["units"])), item.get("source_file", ""),
                  int(item.get("row_no", 0)),
                  json.dumps(item.get("flags", []), ensure_ascii=False))
                 for item in items],
            )

    def list_usage_rows(self, run_id: str) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM usage_rows WHERE run_id=? ORDER BY sub_id", (run_id,)
            ).fetchall()
        out = []
        for row in rows:
            data = dict(row)
            data["flags"] = _loads(data.pop("flags_json", "[]"), [])
            out.append(data)
        return out

    # ---------- 超额记录 ----------
    def put_overages(self, run_id: str, items: list[dict]) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM overages WHERE run_id=?", (run_id,))
            conn.executemany(
                """INSERT INTO overages (run_id, invoice_id, sub_id, seq, segment_seq,
                        plan, metric, days, included_units, allocated_units, overage_units,
                        unit_price, exact_text, cents, flags_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                [(run_id, item["invoice_id"], item["sub_id"], int(item["seq"]),
                  int(item.get("segment_seq", 0)), item.get("plan", ""),
                  item.get("metric", ""), int(item.get("days", 0)),
                  str(item.get("included_units", "0")),
                  str(item.get("allocated_units", "0")),
                  str(item.get("overage_units", "0")), str(item.get("unit_price", "0")),
                  str(item["exact"]), int(item.get("cents", 0)),
                  json.dumps(item.get("flags", []), ensure_ascii=False)) for item in items],
            )

    def list_overages(self, run_id: str, invoice_id: str | None = None) -> list[dict]:
        with self.connect() as conn:
            if invoice_id:
                rows = conn.execute(
                    "SELECT * FROM overages WHERE run_id=? AND invoice_id=? "
                    "ORDER BY invoice_id, seq", (run_id, invoice_id)).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM overages WHERE run_id=? ORDER BY invoice_id, seq",
                    (run_id,)).fetchall()
        out = []
        for row in rows:
            data = dict(row)
            data["flags"] = _loads(data.pop("flags_json", "[]"), [])
            out.append(data)
        return out

    # ---------- 整体重算 ----------
    DERIVED_TABLES = ("segments", "components", "invoices", "overages", "stages",
                      "totals", "reconciliations")
    # 失败恢复要覆盖「重算可能改到的全部表」：派生表 + 用量/隔离/订阅/变更/目录。
    # 否则失败的用量上传会留下已登记的文件，重试命中 sha256 就永远不会重算。
    SNAPSHOT_TABLES = DERIVED_TABLES + ("usage_rows", "usage_files", "quarantine",
                                        "plans", "subscriptions", "changes", "inputs")

    @contextmanager
    def rebuild_transaction(self, run_id: str):
        """重算的单一事务语义：失败时把派生表与清单原样写回。

        SQLite 单连接事务无法跨 ``store`` 方法共享，因此用「先快照旧派生数据，
        失败时原样写回」的补偿做法；每个 ``put_*`` 各自整表替换，不在这里预删，
        这样只重算金额的入口（POST /api/proration）不会把分段先清空。
        """
        snapshot = self._snapshot_derived(run_id)
        manifest = self.get_manifest(run_id)
        try:
            yield
        except Exception:
            self._restore_derived(run_id, snapshot, manifest)
            raise

    def _snapshot_derived(self, run_id: str) -> dict[str, list[sqlite3.Row]]:
        snapshot: dict[str, list[sqlite3.Row]] = {}
        with self.connect() as conn:
            for table in self.SNAPSHOT_TABLES:
                snapshot[table] = conn.execute(
                    f"SELECT * FROM {table} WHERE run_id=?", (run_id,)).fetchall()
        return snapshot

    def _restore_derived(self, run_id: str, snapshot: dict[str, list],
                         manifest: dict | None) -> None:
        with self.connect() as conn:
            for table, rows in snapshot.items():
                conn.execute(f"DELETE FROM {table} WHERE run_id=?", (run_id,))
                if not rows:
                    continue
                columns = list(rows[0].keys())
                placeholders = ",".join("?" for _ in columns)
                conn.executemany(
                    f"INSERT INTO {table} ({','.join(columns)}) VALUES ({placeholders})",
                    [tuple(row[column] for column in columns) for row in rows])
        if manifest is not None:
            self.put_manifest(run_id, manifest)

    def clear_derived(self, run_id: str) -> None:
        """重算前清派生表；订阅、变更、隔离、输入摘要、用量保留。"""
        with self.connect() as conn:
            for table in self.DERIVED_TABLES:
                conn.execute(f"DELETE FROM {table} WHERE run_id=?", (run_id,))
