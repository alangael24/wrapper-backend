"""Agent Genia product tiers and server-funded model budgets."""

from __future__ import annotations

TIERS: dict[str, dict] = {
    "free": {
        "label": "Free",
        "multiplier": 0.0,
        "model_access": False,
    },
    "basic": {
        "label": "Básico",
        "multiplier": 0.5,
        "model_access": True,
    },
    "pro": {
        "label": "Pro",
        "multiplier": 1.0,
        "model_access": True,
    },
}

DEFAULT_TIER = "free"


def is_valid(tier: str) -> bool:
    return tier in TIERS


def has_model_access(tier: str) -> bool:
    t = TIERS.get(tier)
    return bool(t and t.get("model_access", False))


def multiplier(tier: str) -> float:
    t = TIERS.get(tier)
    return float(t["multiplier"]) if t else 0.0


def effective_limits(tier: str) -> dict[str, float]:
    """Devuelve los limites por ventana ajustados al tier."""
    from .deepseek_prices import LIMITS

    m = multiplier(tier)
    return {k: round(v * m, 2) for k, v in LIMITS.items()}


def tier_label(tier: str) -> str:
    t = TIERS.get(tier)
    return t["label"] if t else tier
