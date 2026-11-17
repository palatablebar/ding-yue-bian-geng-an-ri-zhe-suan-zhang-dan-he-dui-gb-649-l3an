"""超额定稿：额度随套餐分段天数成立、逐条可追、页面与导出同源。

财务口径：包含额度不整额发给升级日后的新套餐，而是按该套餐实际覆盖天数折算；
整月用量按同样天数比例分摊到各段，因此每条超额记录都能追到 ``(账单号, 段序)``。
"""
from __future__ import annotations

import csv
import io
from decimal import Decimal

import expected
import overage_expected as oe
from app import config
from app.modules.m1_ingest import service as ingest_service
from app.modules.m5_changes import usage as usage_mod


def _seed_with_usage(client):
    response = client.post("/api/subscriptions/import",
                           json={"usage": "usage_dirty.csv"})
    assert response.status_code == 200
    return response.json()


def test_quota_prorates_by_segment_days(client):
    data = _seed_with_usage(client)
    assert data["totals"]["overage_cents"] == str(oe.OVERAGE_TOTAL_CENTS)
    client_data = client.get("/api/invoices").json()
    by_id = {row["invoice_id"]: row for row in client_data["invoices"]}
    for invoice_id, cents in oe.OVERAGE_CENTS_BY_INVOICE.items():
        assert by_id[invoice_id]["overage_cents"] == cents, invoice_id
        # 账单额 = 套餐费 − 抵扣 + 超额
        assert (by_id[invoice_id]["total_cents"]
                == by_id[invoice_id]["charge_cents"] - by_id[invoice_id]["credit_cents"]
                + by_id[invoice_id]["overage_cents"])
    # 四本账在含超额的批次里仍必须全平
    assert all(row["status"] == "ok" for row in client_data["ledgers"])


def test_upgrade_day_does_not_grant_full_quota(client):
    # 运营的诉求若按「升级当天整额给足」实现，新套餐段不会产生超额；
    # 按天数成立时，专业版整额 2000 只覆盖 18/28，段 3 才会出现超额
    _seed_with_usage(client)
    rows = client.get("/api/overages").json()["overages"]
    third = [row for row in rows
             if row["invoice_id"] == "INV-202602-SUB-1001"
             and row["segment_seq"] == 2]
    assert len(third) == 1
    assert third[0]["plan"] == "专业版"
    assert third[0]["days"] == 18
    # 段包含额度 = 2000 × 18 / 28（34 位精度），而不是整额 2000
    included = Decimal(third[0]["included_units"])
    assert abs(included - Decimal("2000") * 18 / 28) < Decimal("0.00000001")
    assert included < Decimal("2000")


def test_every_overage_traces_to_segment(client):
    _seed_with_usage(client)
    rows = client.get("/api/overages").json()["overages"]
    assert len(rows) == oe.OVERAGE_ROWS
    assert sum(1 for row in rows if Decimal(row["overage_units"]) > 0) == (
        oe.OVERAGE_POSITIVE_ROWS)
    detail_cache: dict[str, dict] = {}
    for row in rows:
        invoice_id = row["invoice_id"]
        if invoice_id not in detail_cache:
            detail_cache[invoice_id] = client.get(
                f"/api/invoices/{invoice_id}").json()
        detail = detail_cache[invoice_id]
        segment = [item for item in detail["segments"]
                   if item["seq"] == row["segment_seq"]]
        assert segment, row
        assert segment[0]["plan"] == row["plan"]
        assert segment[0]["days"] == row["days"]
        # 超额行合计必须等于账单上的超额
        assert sum(item["cents"] for item in detail["overages"]) == (
            detail["invoice"]["overage_cents"])


def test_segment_overage_cents_conservation(client):
    data = _seed_with_usage(client)
    run_id = data["run_id"]
    store = client.app.state.store
    invoices = {row["invoice_id"]: row for row in store.list_invoices(run_id)}
    segment_overage: dict[str, int] = {}
    for segment in store.list_segments(run_id):
        segment_overage[segment["invoice_id"]] = (
            segment_overage.get(segment["invoice_id"], 0)
            + int(segment["overage_cents"]))
    assert sum(segment_overage.values()) == oe.OVERAGE_TOTAL_CENTS
    for invoice_id, cents in segment_overage.items():
        assert cents == invoices[invoice_id]["overage_cents"], invoice_id


def test_overage_csv_matches_api_and_page(client):
    _seed_with_usage(client)
    response = client.get("/api/export/overages.csv")
    assert response.status_code == 200
    assert response.headers["x-row-count"] == str(oe.OVERAGE_ROWS)
    rows = list(csv.DictReader(io.StringIO(response.text.lstrip("﻿"))))
    api = client.get("/api/overages").json()["overages"]
    assert len(rows) == len(api)
    total = sum((Decimal(row["超额金额(元)"]) for row in rows), Decimal("0"))
    assert total == Decimal(oe.OVERAGE_TOTAL_CENTS) / 100
    # 账单 CSV 的超额列与账单页同源
    invoices_csv = client.get("/api/export/invoices.csv").text
    invoice_rows = list(csv.DictReader(io.StringIO(invoices_csv.lstrip("﻿"))))
    by_id = {row["账单号"]: row for row in invoice_rows}
    for invoice_id, cents in oe.OVERAGE_CENTS_BY_INVOICE.items():
        assert Decimal(by_id[invoice_id]["超额费(元)"]) == Decimal(cents) / 100
    page = client.get("/invoices").text
    assert "超额明细" in page
    assert "INV-202602-SUB-1001" in page


def test_usage_import_is_idempotent(client):
    first = _seed_with_usage(client)
    before = client.get("/api/invoices").json()["totals"]
    # 同一份用量文件重试：按 sha256 命中，不重复累计
    again = client.post("/api/changes/usage/import",
                        json={"usage": "usage_dirty.csv"})
    assert again.status_code == 200
    assert again.json()["reused"] is True
    after = client.get("/api/invoices").json()["totals"]
    assert after["overage_cents"] == before["overage_cents"] == str(
        oe.OVERAGE_TOTAL_CENTS)
    assert after["invoice_cents"] == before["invoice_cents"]
    store = client.app.state.store
    assert len(store.list_usage_rows(first["run_id"])) == oe.USAGE_ROWS_READ - 2


def test_usage_quarantine_unknown_subscription_and_missing(client):
    data = _seed_with_usage(client)
    store = client.app.state.store
    codes = {row["code"] for row in store.list_quarantine(data["run_id"])}
    for code in oe.USAGE_QUARANTINE_CODES:
        assert code in codes
    counters = store.get_run(data["run_id"])["counters"]
    assert counters["usage_rows"] == oe.USAGE_ROWS_READ
    assert counters["usage_kept"] == oe.USAGE_ROWS_READ - 2


def test_failed_recompute_restores_previous_invoice(settings, monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import create_app

    app = create_app(settings)
    # 用不抛服务端异常的客户端，验证「失败返回 5xx 且旧账单完整」
    with TestClient(app, raise_server_exceptions=False) as client:
        _seed_with_usage(client)
        store = app.state.store
        run_id = client.get("/api/invoices").json()["run_id"]
        before = {row["invoice_id"]: dict(row) for row in store.list_invoices(run_id)}
        before_totals = dict(store.list_totals(run_id))
        before_manifest = store.get_manifest(run_id)
        before_overages = store.list_overages(run_id)

        from app.modules.m4_proration import service as proration_service

        calls = {"n": 0}
        real = proration_service.recompute_amounts

        def boom(*args, **kwargs):
            calls["n"] += 1
            raise RuntimeError("模拟重算中途失败")

        monkeypatch.setattr(proration_service, "recompute_amounts", boom)
        response = client.post("/api/proration")
        assert response.status_code == 500
        assert calls["n"] == 1

        # 失败后派生表与清单必须原样恢复，不留半批
        after = {row["invoice_id"]: dict(row) for row in store.list_invoices(run_id)}
        assert after == before
        assert store.list_totals(run_id) == before_totals
        assert store.get_manifest(run_id) == before_manifest
        assert store.list_overages(run_id) == before_overages
        assert store.list_reconciliations(run_id)

        monkeypatch.setattr(proration_service, "recompute_amounts", real)
        # 页面与导出仍读到完整旧版账单
        assert client.get("/invoices").status_code == 200
        assert client.get("/api/export/invoices.csv").headers["x-row-count"] == str(
            len(before))
        assert client.post("/api/proration").status_code == 200


def test_failed_usage_import_restores_and_allows_retry(settings, monkeypatch, tmp_path):
    """用量上传失败不能留下「已登记」的文件，否则重试会命中 sha256 永不重算。"""
    import shutil
    from pathlib import Path

    from fastapi.testclient import TestClient

    from app.main import create_app
    from app.modules.m5_changes import service as change_service

    source = Path(config.Settings().sample_dir) / "usage_dirty.csv"
    sample_dir = tmp_path / "samples"
    sample_dir.mkdir()
    for name in ("subscriptions_dirty.csv", "changes_dirty.csv"):
        shutil.copy(Path(config.Settings().sample_dir) / name, sample_dir / name)
    shutil.copy(source, sample_dir / "usage_v1.csv")
    shutil.copy(source, sample_dir / "usage_v2.csv")
    with open(sample_dir / "usage_v2.csv", "a", encoding="utf-8") as handle:
        handle.write("SUB-1006,GB,50\n")

    settings = config.Settings(db_path=settings.db_path, sample_dir=str(sample_dir))
    app = create_app(settings)
    with TestClient(app, raise_server_exceptions=False) as client:
        assert client.post("/api/subscriptions/import",
                           json={"usage": "usage_v1.csv"}).status_code == 200
        store = app.state.store
        run_id = client.get("/api/invoices").json()["run_id"]
        before = {table: [dict(row) for row in rows]
                  for table, rows in store._snapshot_derived(run_id).items()}
        before_manifest = store.get_manifest(run_id)
        before_totals = dict(store.list_totals(run_id))

        real = change_service.rebuild

        def boom(*args, **kwargs):
            raise RuntimeError("模拟用量重算失败")

        monkeypatch.setattr(change_service, "rebuild", boom)
        assert client.post("/api/changes/usage/import",
                           json={"usage": "usage_v2.csv"}).status_code == 500
        after = {table: [dict(row) for row in rows]
                 for table, rows in store._snapshot_derived(run_id).items()}
        assert after == before
        assert store.get_manifest(run_id) == before_manifest
        assert store.list_totals(run_id) == before_totals
        assert [row["name"] for row in store.list_usage_files(run_id)] == ["usage_v1.csv"]

        monkeypatch.setattr(change_service, "rebuild", real)

        # 修复后重试同一份文件：必须真的重算，而不是命中失败留下的登记
        retry = client.post("/api/changes/usage/import", json={"usage": "usage_v2.csv"})
        assert retry.status_code == 200
        assert retry.json()["reused"] is False
        names = [row["name"] for row in store.list_usage_files(run_id)]
        assert names == ["usage_v1.csv", "usage_v2.csv"]
        manifest = store.get_manifest(run_id)
        assert {item["name"] for item in manifest["inputs"]} == {
            "subscriptions_dirty.csv", "changes_dirty.csv",
            "usage_v1.csv", "usage_v2.csv"}
        assert client.get("/api/export/invoices.csv").status_code == 200


def test_overage_segment_seq_matches_stored_segment(client):
    """零天段不落库；超额行的段序必须与落库分段（而不是原始切段）一致。

    SUB-1010 在期间首日就暂停，``build_segments`` 会产生一个零天首段；
    若超额行沿用原始 seq，就会指向不存在的分段。
    """
    from pathlib import Path
    import shutil

    from fastapi.testclient import TestClient

    from app.main import create_app

    sample_dir = Path(config.Settings().sample_dir)
    tmp = Path(client.app.state.settings.data_dir) / "samples"
    tmp.mkdir()
    for name in ("subscriptions_dirty.csv", "changes_dirty.csv"):
        shutil.copy(sample_dir / name, tmp / name)
    (tmp / "usage_zero_seg.csv").write_text(
        "订阅号,计量单位,用量\nSUB-1010,GB,900\n", encoding="utf-8")
    settings = config.Settings(db_path=str(Path(client.app.state.settings.data_dir)
                                          / "z.sqlite"), sample_dir=str(tmp))
    app = create_app(settings)
    with TestClient(app) as sub_client:
        assert sub_client.post("/api/subscriptions/import",
                               json={"usage": "usage_zero_seg.csv"}).status_code == 200
        invoice_id = "INV-202611-SUB-1010"
        segments = sub_client.get(f"/api/invoices/{invoice_id}").json()["segments"]
        overages = sub_client.get("/api/invoices/{invoice_id}".format(
            invoice_id=invoice_id)).json()["overages"]
        assert [row["days"] for row in segments] == [14, 16]
        # 暂停段如实留 0 额证据；段序 0/1 都能对应到落库分段
        assert [row["segment_seq"] for row in overages] == [0, 1]
        for row in overages:
            assert row["seq"] == row["segment_seq"]
            assert row["segment_seq"] < len(segments)
            assert segments[row["segment_seq"]]["plan"] == row["plan"]


def test_empty_usage_file_keeps_row_ledger_balanced(tmp_path):
    """只有表头的用量文件：文件级隔离条目不计入行数账的「隔离行」。"""
    import shutil
    from pathlib import Path

    from fastapi.testclient import TestClient

    from app.main import create_app

    sample_dir = Path(config.Settings().sample_dir)
    for name in ("subscriptions_dirty.csv", "changes_dirty.csv"):
        shutil.copy(sample_dir / name, tmp_path / name)
    (tmp_path / "usage_header_only.csv").write_text(
        "订阅号,计量单位,用量\n", encoding="utf-8")
    settings = config.Settings(db_path=str(tmp_path / "t.sqlite"),
                               sample_dir=str(tmp_path))
    app = create_app(settings)
    with TestClient(app) as client:
        data = client.post("/api/subscriptions/import",
                           json={"usage": "usage_header_only.csv"}).json()
        assert data["totals"]["overage_cents"] == "0"
        assert all(row["status"] == "ok" for row in data["ledgers"]), data["ledgers"]
        codes = {row["code"] for row in app.state.store.list_quarantine(
            data["run_id"])}
        assert "R_HEADER_ONLY" in codes


def test_without_usage_legacy_totals_unchanged(client):
    # 没有用量文件时，原有金额与账单完全不受影响（向后兼容）
    client.post("/api/subscriptions/import", json={})
    totals = client.get("/api/invoices").json()["totals"]
    assert totals["invoice_cents"] == str(expected.TOTAL_CENTS)
    assert totals["overage_cents"] == "0"
    assert totals["overage_rows"] == "0"
    assert client.get("/api/overages").json()["overages"] == []


def test_plan_catalog_falls_back_to_builtin(tmp_path):
    plans, source = __import__(
        "app.modules.m3_quality.usage", fromlist=["load_plan_catalog"]
    ).load_plan_catalog(str(tmp_path))
    assert source == "builtin"
    assert {row["plan"] for row in plans} == set(config.DEFAULT_PLAN_CATALOG)


def test_plan_catalog_from_file_overrides(tmp_path):
    (tmp_path / "plans.csv").write_text(
        "套餐,计量单位,包含额度,超额单价(元)\n"
        "标准版,GB,1000,0.10\n", encoding="utf-8")
    plans, source = __import__(
        "app.modules.m3_quality.usage", fromlist=["load_plan_catalog"]
    ).load_plan_catalog(str(tmp_path))
    assert source == "file"
    assert plans == [{"plan": "标准版", "metric": "GB", "included": "1000",
                      "unit_price": "0.10", "source": "file"}]


def test_split_usage_ignores_included_beyond_usage():
    plan = usage_mod.PlanQuota(plan="标准版", metric="GB", included=Decimal("500.00"),
                               unit_price=Decimal("0.30"))
    segments = [usage_mod.SegmentView(seq=0, plan="标准版", days=14),
                usage_mod.SegmentView(seq=1, plan="标准版", days=14)]
    quotas = usage_mod.split_usage(segments, Decimal("400"), 28,
                                   {"标准版": plan})
    assert all(quota.overage_units == 0 for quota in quotas)
    assert sum((quota.allocated_units for quota in quotas), Decimal("0")) == Decimal("400")


def test_import_service_usage_argument(store, settings):
    data = ingest_service.import_sample(store, settings, usage="usage_dirty.csv")
    assert data["totals"]["overage_cents"] == str(oe.OVERAGE_TOTAL_CENTS)
    # 再采信同一组合：命中同一批次，不新增行
    again = ingest_service.import_sample(store, settings, usage="usage_dirty.csv")
    assert again["reused"] is True
    assert again["run_id"] == data["run_id"]
    assert len(store.list_runs()) == 1
