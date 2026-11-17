"""M4 按日折算：把每个分段折成金额，再按最大余额分到「分」。

分段金额与抵扣行各自分配一次，但总额只有一个：两者都必须等于
「精确净额四舍五入到分」，否则账单会出现 1 分钱对不平的尾差。
"""
from __future__ import annotations

from decimal import Decimal, localcontext

from ... import config
from . import prorate, rounding


def _sub_index(store, run_id: str) -> dict[str, dict]:
    return {sub["sub_id"]: sub for sub in store.list_subscriptions(run_id)}


def _group(rows: list[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        grouped.setdefault(row["invoice_id"], []).append(row)
    for rows_in_group in grouped.values():
        rows_in_group.sort(key=lambda item: item["seq"])
    return grouped


def recompute_amounts(store, run_id: str, settings) -> dict:
    """重算分段金额、抵扣行金额与超额金额（幂等：同输入同结果）。

    三组分量各自用最大余额法分配到分，但每张账单只有一个「精确净额」：
    分段分合计 = 抵扣分合计 + 账单额分合计，超额分只作为账单额的组成部分。
    """
    subs = _sub_index(store, run_id)
    segments = store.list_segments(run_id)
    components = store.list_components(run_id)
    overages = store.list_overages(run_id)
    segment_updates: list[dict] = []
    component_updates: list[dict] = []
    exact_total = Decimal("0")
    cents_total = 0

    for invoice_id, rows in _group(segments).items():
        sub = subs[rows[0]["sub_id"]]
        period_days = int(sub["period_days"])
        exacts = [
            prorate.segment_amount(Decimal(row["monthly"]), int(row["days"]),
                                   period_days, row["state"])
            for row in rows
        ]
        cents = rounding.allocate(exacts)
        for row, exact, value in zip(rows, exacts, cents):
            segment_updates.append({
                "invoice_id": invoice_id,
                "seq": row["seq"],
                "amount_exact": prorate.exact_text(exact),
                "amount_cents": value,
            })
            with localcontext() as ctx:
                ctx.prec = config.DECIMAL_PREC
                exact_total += prorate.quantize_exact(exact)
            cents_total += value

    for invoice_id, rows in _group(components).items():
        exacts = [Decimal(row["exact_text"]) for row in rows]
        cents = rounding.allocate(exacts)
        for row, value in zip(rows, cents):
            component_updates.append({
                "invoice_id": invoice_id,
                "seq": row["seq"],
                "sub_id": row["sub_id"],
                "kind": row["kind"],
                "source": row["source"],
                "label": row["label"],
                "days": row["days"],
                "monthly": row["monthly"],
                "exact": row["exact_text"],
                "cents": value,
                "flags": row["flags"],
            })

    overage_by_invoice = _allocate_overages(store, run_id, overages)

    store.update_segment_amounts(run_id, segment_updates)
    store.put_components(run_id, component_updates)
    return {
        "segments": len(segment_updates),
        "components": len(component_updates),
        "overages": len(overages),
        "overage_cents": {key: sum(rows) for key, rows in overage_by_invoice.items()},
        "segment_exact_total": str(exact_total),
        "segment_cents_total": cents_total,
    }


def _allocate_overages(store, run_id: str, overages: list[dict]) -> dict[str, list[int]]:
    """超额的精确金额按最大余额法分到分，写回超额行与对应分段行。

    返回 ``invoice_id → [分...]``，供账单层直接读取，页面与导出同源。
    """
    grouped = _group(overages)
    per_invoice: dict[str, list[int]] = {}
    overage_updates: list[dict] = []
    segment_overage: dict[tuple[str, int], int] = {}
    for invoice_id, rows in grouped.items():
        exacts = [Decimal(row["exact_text"]) for row in rows]
        cents = rounding.allocate(exacts)
        per_invoice[invoice_id] = cents
        for row, value in zip(rows, cents):
            overage_updates.append({
                "invoice_id": invoice_id,
                "seq": row["seq"],
                "sub_id": row["sub_id"],
                "segment_seq": row["segment_seq"],
                "plan": row["plan"],
                "metric": row["metric"],
                "days": row["days"],
                "included_units": row["included_units"],
                "allocated_units": row["allocated_units"],
                "overage_units": row["overage_units"],
                "unit_price": row["unit_price"],
                "exact": row["exact_text"],
                "cents": value,
                "flags": row["flags"],
            })
            key = (invoice_id, int(row["segment_seq"]))
            segment_overage[key] = segment_overage.get(key, 0) + value
    store.put_overages(run_id, overage_updates)
    store.update_segment_overage_cents(run_id, [
        {"invoice_id": invoice_id, "seq": seq, "overage_cents": cents}
        for (invoice_id, seq), cents in segment_overage.items()
    ])
    return per_invoice


def segment_rows(store, run_id: str) -> list[dict]:
    """按账单分组的折算明细，页面与接口共用。"""
    subs = _sub_index(store, run_id)
    rows = []
    for segment in store.list_segments(run_id):
        sub = subs[segment["sub_id"]]
        period_days = int(sub["period_days"])
        monthly = Decimal(segment["monthly"] or "0")
        rows.append({
            **segment,
            "account": sub["account"],
            "period_days": period_days,
            "daily": str(prorate.daily_rate(monthly, period_days)) if period_days else "0",
            "amount": rounding.cents_text(segment["amount_cents"]),
            "overage": rounding.cents_text(segment.get("overage_cents", 0)),
        })
    return rows


def summary(store, run_id: str) -> dict:
    rows = segment_rows(store, run_id)
    return {
        "segments": len(rows),
        "invoices": len({row["invoice_id"] for row in rows}),
        "days": sum(int(row["days"]) for row in rows),
        "amount_cents": sum(int(row["amount_cents"]) for row in rows),
        "amount": rounding.cents_text(sum(int(row["amount_cents"]) for row in rows)),
        "overage_cents": sum(int(row.get("overage_cents", 0)) for row in rows),
        "overage": rounding.cents_text(sum(int(row.get("overage_cents", 0))
                                           for row in rows)),
    }
