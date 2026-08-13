"""Canonical Agent Genia plan catalog.

``basic`` remains the stable API identifier but is presented as Starter.
Credits and concurrency are product entitlements; model-provider credentials
remain entirely server-owned.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class Plan:
    label: str
    monthly_price_usd: int
    monthly_credit_milli: int
    max_concurrent_runs: int
    five_hour_credit_milli: int
    seven_day_credit_milli: int


PLANS: dict[str, Plan] = {
    "free": Plan("Free Trial", 0, 30_000, 1, 15_000, 30_000),
    "basic": Plan("Starter", 29, 300_000, 1, 60_000, 150_000),
    "pro": Plan("Pro", 79, 1_000_000, 2, 200_000, 500_000),
    "business": Plan("Business", 199, 3_000_000, 4, 600_000, 1_500_000),
}

DEFAULT_TIER = "free"
PAID_TIERS = frozenset({"basic", "pro", "business"})
TRIAL_CREDIT_MILLI = 30_000


def is_valid(tier: str) -> bool:
    return tier in PLANS


def plan_for(tier: str) -> Plan:
    return PLANS.get(tier, PLANS[DEFAULT_TIER])


def has_model_access(tier: str) -> bool:
    """Legacy entitlement helper; free access is decided by credit balance."""
    return tier in PAID_TIERS


def tier_label(tier: str) -> str:
    return plan_for(tier).label


def plan_payload(tier: str) -> dict:
    return {"tier": tier, **asdict(plan_for(tier))}


def effective_limits(tier: str) -> dict[str, float]:
    """Credit budgets for the rolling windows and the billing cycle."""
    plan = plan_for(tier)
    return {
        "5h": plan.five_hour_credit_milli / 1_000,
        "week": plan.seven_day_credit_milli / 1_000,
        "month": plan.monthly_credit_milli / 1_000,
    }
