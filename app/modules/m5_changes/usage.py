"""M5 超额：把整月用量按套餐分段拆开，额度只随各套餐实际覆盖天数成立。

核心规则（与按日折算同一分母，避免「升级当天整额给足」的双重发放）：

    段包含额度 = 套餐包含额度 × 段天数 ÷ 期间天数
    段分摊用量 = 整月用量   × 段天数 ÷ 期间天数
    段超额     = max(段分摊用量 − 段包含额度, 0)
    段超额金额 = 段超额 × 套餐超额单价

整月用量文件没有时间戳，只能按段天数比例分摊；这样每条超额记录都能追到
``(账单号, 段序, 套餐)``，且 Σ段超额 = 整月超额，不会因分段而多出或少掉额度。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

from ..m4_proration import prorate

ZERO = Decimal("0")
UNIT_SCALE = Decimal("0.00000001")


def quantize_units(value: Decimal) -> Decimal:
    """用量入库精度 8 位小数：与金额 exact 同一尺度，够复核又不铺 34 位精度。"""
    return Decimal(value).quantize(UNIT_SCALE, rounding=ROUND_HALF_UP)


@dataclass
class PlanQuota:
    plan: str
    metric: str
    included: Decimal
    unit_price: Decimal

    def included_for(self, days: int, period_days: int) -> Decimal:
        if days <= 0 or period_days <= 0:
            return ZERO
        return prorate.share(self.included, days, period_days)


@dataclass
class SegmentQuota:
    seq: int
    plan: str
    metric: str
    days: int
    included_units: Decimal
    allocated_units: Decimal
    overage_units: Decimal
    unit_price: Decimal
    exact: Decimal
    flags: list[str] = field(default_factory=list)


def plan_index(plans: list[dict]) -> dict[str, PlanQuota]:
    return {
        row["plan"]: PlanQuota(
            plan=row["plan"], metric=row.get("metric", ""),
            included=Decimal(str(row.get("included", "0"))),
            unit_price=Decimal(str(row.get("unit_price", "0"))),
        )
        for row in plans
    }


def split_usage(segments: list["SegmentView"], usage_units: Decimal,
                period_days: int, plans: dict[str, PlanQuota]) -> list[SegmentQuota]:
    """按段分摊整月用量并逐段算超额；未知套餐按 0 额度 0 单价留痕，不静默跳过。"""
    results: list[SegmentQuota] = []
    for segment in segments:
        plan = plans.get(segment.plan)
        metric = plan.metric if plan else ""
        included = plan.included_for(segment.days, period_days) if plan else ZERO
        allocated = prorate.share(usage_units, segment.days, period_days)
        overage = max(allocated - included, ZERO)
        unit_price = plan.unit_price if plan else ZERO
        flags: list[str] = []
        if segment.days > 0 and allocated > 0:
            flags.append("R_USAGE_ALLOCATED")
        if plan is None and segment.days > 0:
            flags.append("R_PLAN_UNKNOWN")
        results.append(SegmentQuota(
            seq=segment.seq,
            plan=segment.plan,
            metric=metric,
            days=segment.days,
            included_units=quantize_units(included),
            allocated_units=quantize_units(allocated),
            overage_units=quantize_units(overage),
            unit_price=unit_price,
            exact=overage * unit_price,
            flags=flags,
        ))
    return results


@dataclass
class SegmentView:
    """超额计算需要的段字段；与 segments.Segment 解耦，便于单测。"""

    seq: int
    plan: str
    days: int
