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


PLANS: dict[str, Plan] = {
    "free": Plan("Free Trial", 0, 0, 1),
    "basic": Plan("Starter", 29, 300_000, 1),
    "pro": Plan("Pro", 79, 1_000_000, 2),
    "business": Plan("Business", 199, 3_000_000, 4),
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
    """Deprecated response compatibility for one client migration cycle."""
    legacy = {
        "free": {"5h": 0.0, "week": 0.0, "month": 0.0},
        "basic": {"5h": 6.0, "week": 15.0, "month": 30.0},
        "pro": {"5h": 12.0, "week": 30.0, "month": 60.0},
        "business": {"5h": 24.0, "week": 60.0, "month": 120.0},
    }
    return legacy.get(tier, legacy[DEFAULT_TIER]).copy()
