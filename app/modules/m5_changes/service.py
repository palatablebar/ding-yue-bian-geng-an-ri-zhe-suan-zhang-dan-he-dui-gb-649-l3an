"""M5 变更与超额：重建分段、抵扣行、超额记录并落库。

重算入口只做一件事：从已归一的订阅、变更、用量重建派生结构，
金额留给 M4（按日折算）与超额（按段额度），账单留给 M6；重跑结果与上次逐行一致。
"""
from __future__ import annotations

from decimal import Decimal

from ... import config, reasons
from ..m1_ingest import reader
from ..m3_quality import usage as usage_ingest
from ..m4_proration import prorate
from ..m6_invoices import invoice as invoice_mod
from . import credit, segments as segments_mod
from . import usage as usage_mod


def _usage_index(store, run_id: str) -> dict[str, dict]:
    return {row["sub_id"]: row for row in store.list_usage_rows(run_id)}


def _attach_quota(sub: dict, built: list[segments_mod.Segment], usage_row: dict | None,
                  plans: dict[str, usage_mod.PlanQuota]) -> list[usage_mod.SegmentQuota]:
    """把整月用量按段分摊到每个可计费段，返回与 ``billable_segments`` 对齐的行。

    段序一律用「可计费段内的序号」，与 ``segment_rows`` 落库的 seq 一致；
    零天段不落库，因此不能用 ``build_segments`` 里的原始 seq，否则超额行追不到分段。
    """
    period_days = int(sub["period_days"])
    billable = segments_mod.billable_segments(built)
    usage_units = Decimal(usage_row["units_exact"]) if usage_row else Decimal("0")
    # 未上传用量时不做超额计算：额度保留事实，但不把「没用量」当「零用量」扣款
    quotas = usage_mod.split_usage(
        [usage_mod.SegmentView(seq=index, plan=segment.plan, days=segment.days)
         for index, segment in enumerate(billable)],
        usage_units, period_days, plans)
    for segment, quota in zip(billable, quotas):
        segment.seq = quota.seq
        segment.metric = quota.metric
        segment.included_units = quota.included_units
        segment.allocated_units = quota.allocated_units if usage_row else Decimal("0")
        segment.overage_units = quota.overage_units if usage_row else Decimal("0")
        segment.unit_price = quota.unit_price
        segment.overage_exact = quota.exact if usage_row else Decimal("0")
        if usage_row:
            for flag in quota.flags:
                if flag not in segment.flags:
                    segment.flags.append(flag)
    return quotas


def rebuild(store, run_id: str, settings) -> dict:
    subs = store.list_subscriptions(run_id)
    changes = store.list_changes(run_id)
    plans = usage_mod.plan_index(store.list_plans(run_id))
    usage_by_sub = _usage_index(store, run_id)
    by_sub: dict[str, list[dict]] = {}
    for change in changes:
        by_sub.setdefault(change["sub_id"], []).append(change)

    segment_rows: list[dict] = []
    component_rows: list[dict] = []
    overage_rows: list[dict] = []
    for sub in subs:
        invoice_id = invoice_mod.invoice_id_for(sub)
        built = segments_mod.build_segments(sub, by_sub.get(sub["sub_id"], []))
        usage_row = usage_by_sub.get(sub["sub_id"])
        quotas = _attach_quota(sub, built, usage_row, plans)
        rows = segments_mod.segment_rows(invoice_id, sub, built)
        segment_rows.extend(rows)
        period_days = int(sub["period_days"])
        for component in credit.build_components(built, period_days):
            component_rows.append({
                "invoice_id": invoice_id,
                "sub_id": sub["sub_id"],
                "seq": component.seq,
                "kind": component.kind,
                "source": component.source,
                "label": component.label,
                "days": component.days,
                "monthly": str(component.monthly),
                "exact": prorate.exact_text(component.exact),
                "cents": 0,
                "flags": component.flags,
            })
        for seq, quota in enumerate(quotas):
            if usage_row is None or quota.days <= 0:
                continue
            flags = list(quota.flags)
            overage_rows.append({
                "invoice_id": invoice_id,
                "sub_id": sub["sub_id"],
                "seq": seq,
                "segment_seq": quota.seq,
                "plan": quota.plan,
                "metric": quota.metric,
                "days": quota.days,
                "included_units": str(quota.included_units),
                "allocated_units": str(quota.allocated_units),
                "overage_units": str(quota.overage_units),
                "unit_price": str(quota.unit_price),
                "exact": prorate.exact_text(quota.exact),
                "cents": 0,
                "flags": flags,
            })

    store.put_segments(run_id, segment_rows)
    store.put_components(run_id, component_rows)
    store.put_overages(run_id, overage_rows)
    return {
        "segments": len(segment_rows),
        "components": len(component_rows),
        "overages": len(overage_rows),
        "changes_applied": sum(1 for change in changes if change["applied"]),
        "changes_superseded": sum(1 for change in changes if not change["applied"]),
        "invoices": len({row["invoice_id"] for row in segment_rows}),
    }


def import_usage(store, run_id: str, settings, name: str) -> dict:
    """采信一份整月用量并重算。

    幂等：文件 sha256 已在本批次登记过就直接返回，不再次累计；
    失败：整批派生数据（含账单与清单）原样恢复，页面看到的还是旧版账单。
    """
    table = reader.load(settings.sample_dir, name)
    existing = store.get_usage_file(run_id, table.sha256)
    if existing is not None:
        run = store.get_run(run_id) or {}
        return {
            "run_id": run_id,
            "reused": True,
            "usage_file": existing,
            "summary": summary(store, run_id),
            "invoices": store.list_totals(run_id),
            "counters": run.get("counters", {}),
        }

    from ..m3_quality import service as quality_service
    from ..m4_proration import service as proration_service
    from ..m6_invoices import service as invoice_service

    with store.rebuild_transaction(run_id):
        subs = store.list_subscriptions(run_id)
        rows, entries, counts = usage_ingest.normalize_usage(table)
        rows, unknown = usage_ingest.annotate_unknown_subscriptions(rows, subs)
        entries = entries + unknown
        counts["kept"] = len(rows)
        counts["quarantined_rows"] = usage_ingest.count_error_rows(entries)
        counts["dropped_dup"] = sum(
            1 for item in entries if item["code"] == "R_DUP_USAGE_ROW")

        store.replace_usage_rows(run_id, rows)
        store.add_usage_file(run_id, {
            "name": table.name, "sha256": table.sha256, "encoding": table.encoding,
            "bytes": table.size, "rows": table.data_rows,
        })
        # 补录的用量文件要进清单的 inputs，否则复算入口少了这次用量
        known = {item["sha256"] for item in store.list_inputs(run_id)}
        if table.sha256 not in known:
            store.put_inputs(run_id, store.list_inputs(run_id) + [{
                "name": table.name, "encoding": table.encoding, "bytes": table.size,
                "sha256": table.sha256, "rows": table.data_rows,
                "header": table.header,
            }])
        current = [item for item in store.list_quarantine(run_id)
                   if item["code"] not in _USAGE_CODES]
        decorated = [quality_service.decorate(item) for item in current + entries]
        store.put_quarantine(run_id, decorated)

        run = store.get_run(run_id) or {}
        counters = dict(run.get("counters", {}))
        counters.update({
            "rows_read": int(counters.get("rows_read", 0)) + counts["rows"],
            "usage_rows": counts["rows"],
            "usage_kept": counts["kept"],
            "usage_quarantined_rows": counts["quarantined_rows"],
            "blank_rows": int(counters.get("blank_rows", 0)) + counts["blank_rows"],
            "repeated_headers": (int(counters.get("repeated_headers", 0))
                                 + counts["repeated_headers"]),
            "truncated_rows": (int(counters.get("truncated_rows", 0))
                               + counts["truncated"]),
            "dropped_dup": int(counters.get("dropped_dup", 0)) + counts["dropped_dup"],
        })
        counters["quarantined_rows"] = _sub_quarantine_total(decorated)
        counters["quarantine_entries"] = len(decorated)
        counters["quarantine_by_level"] = {
            level: sum(1 for item in decorated if item["level"] == level)
            for level in ("error", "warn", "info")
        }
        counters["quarantine_by_code"] = _count_by_code(decorated)
        store.update_run(run_id, counters_json=counters)

        built = rebuild(store, run_id, settings)
        amounts = proration_service.recompute_amounts(store, run_id, settings)
        invoices = invoice_service.rebuild(store, run_id, settings)
        _refresh_manifest(store, run_id, settings, counters, invoices, amounts)

    run = store.get_run(run_id) or {}
    return {
        "run_id": run_id,
        "reused": False,
        "usage_file": {"name": table.name, "sha256": table.sha256,
                       "rows": table.data_rows},
        "changes": built,
        "summary": summary(store, run_id),
        "counters": counters,
        "invoices": invoices["summary"],
        "reconciliations": invoices["ledgers"],
    }


_USAGE_CODES = frozenset(code for code in reasons.REASONS
                         if code.startswith("R_USAGE") or code.startswith("R_DUP_USAGE")
                         or code == "R_PLAN_UNKNOWN")


def _sub_quarantine_total(entries: list[dict]) -> int:
    """行数账的「隔离行」：只数资源行（文件级条目行号为 0）。"""
    return sum(1 for item in entries
               if item["level"] == "error" and int(item["row_no"]) > 0)


def _count_by_code(entries: list[dict]) -> dict:
    counts: dict[str, int] = {}
    for item in entries:
        counts[item["code"]] = counts.get(item["code"], 0) + 1
    return dict(sorted(counts.items()))


def _refresh_manifest(store, run_id: str, settings, counters: dict,
                      invoices: dict, amounts: dict) -> None:
    """重算后刷新清单：同一 run_id，输入摘要补上用量文件，合计与四账同步。"""
    from ... import manifest as manifest_mod

    data = store.get_manifest(run_id) or {}
    inputs = store.list_inputs(run_id)
    totals = dict(invoices["totals"])
    totals["segment_exact_total"] = amounts["segment_exact_total"]
    data.update({
        "inputs": [{
            "name": item["name"], "sha256": item["sha256"], "bytes": item["bytes"],
            "encoding": item["encoding"], "rows": item["rows"],
        } for item in inputs],
        "input_digest": manifest_mod.digest_parts([item["sha256"] for item in inputs]),
        "counters": counters,
        "totals": {key: str(value) for key, value in sorted(totals.items())},
        "reconciliations": invoices["ledgers"],
        "stages": _stages_with_usage(counters, totals, data.get("stages", [])),
        "usage_files": store.list_usage_files(run_id),
    })
    store.put_manifest(run_id, data)
    store.update_run(run_id, input_digest=data["input_digest"], counters_json=counters)


def _stages_with_usage(counters: dict, totals: dict, stages: list[dict]) -> list[dict]:
    for stage in stages:
        if stage.get("stage") == "invoices":
            stage["note"] = (f"账单合计 {totals.get('invoice_cents', 0)} 分"
                             f"（超额 {totals.get('overage_cents', 0)} 分）")
        if stage.get("stage") == "ingest" and counters.get("usage_rows"):
            stage["in_count"] = (int(stage.get("in_count", 0))
                                 + int(counters.get("usage_rows", 0)))
    return stages


def change_rows(store, run_id: str) -> list[dict]:
    """变更序列 + 抵扣行 + 超额行，页面与接口共用同一份数据。"""
    changes = store.list_changes(run_id)
    components = store.list_components(run_id)
    overages = store.list_overages(run_id)
    by_invoice: dict[str, list[dict]] = {}
    for component in components:
        by_invoice.setdefault(component["invoice_id"], []).append(component)
    overage_by_invoice: dict[str, list[dict]] = {}
    for row in overages:
        overage_by_invoice.setdefault(row["invoice_id"], []).append(row)
    subs = {sub["sub_id"]: sub for sub in store.list_subscriptions(run_id)}
    rows = []
    for change in changes:
        sub = subs.get(change["sub_id"], {})
        invoice_id = invoice_mod.invoice_id_for(sub) if sub else ""
        rows.append({
            **change,
            "account": sub.get("account", ""),
            "invoice_id": invoice_id,
            "kind_label": config.KIND_LABELS.get(change["kind"], change["kind"]),
            "components": by_invoice.get(invoice_id, []) if change["applied"] else [],
            "overages": overage_by_invoice.get(invoice_id, []) if change["applied"] else [],
        })
    return rows


def summary(store, run_id: str) -> dict:
    changes = store.list_changes(run_id)
    components = store.list_components(run_id)
    overages = store.list_overages(run_id)
    credits = [row for row in components if row["kind"] == "credit"]
    return {
        "changes": len(changes),
        "applied": sum(1 for change in changes if change["applied"]),
        "superseded": sum(1 for change in changes if not change["applied"]),
        "flagged": sum(1 for change in changes if change["flags"]),
        "components": len(components),
        "credit_rows": len(credits),
        "credit_cents": sum(-int(row["cents"]) for row in credits),
        "overage_rows": sum(1 for row in overages if Decimal(row["overage_units"]) > 0),
        "overage_cents": sum(int(row["cents"]) for row in overages),
    }
