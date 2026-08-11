"""Tiers de usuarios del wrapper.

Cada tier aplica un porcentaje (multiplier) sobre los limites de la
suscripcion de OpenCode Go ($12 / 5h, $30 / semana, $60 / mes).

  - free : sin suscripcion Go asignada; no puede llamar modelos.
  - basic: 50% de la suscripcion Go ($6 / 5h, $15 / semana, $30 / mes).
  - pro  : 100% de la suscripcion Go ($12 / 5h, $30 / semana, $60 / mes).

Los tiers se pueden ajustar aqui (multiplier, requires_subscription) sin
tocar el resto del codigo.
"""

from __future__ import annotations

TIERS: dict[str, dict] = {
    "free": {
        "label": "Free",
        "multiplier": 0.0,
        "requires_subscription": False,
    },
    "basic": {
        "label": "Básico",
        "multiplier": 0.5,
        "requires_subscription": True,
    },
    "pro": {
        "label": "Pro",
        "multiplier": 1.0,
        "requires_subscription": True,
    },
}

DEFAULT_TIER = "basic"
SIGNUP_TIERS = ("free", "basic", "pro")


def is_valid(tier: str) -> bool:
    return tier in TIERS


def requires_subscription(tier: str) -> bool:
    t = TIERS.get(tier)
    return bool(t and t.get("requires_subscription", True))


def multiplier(tier: str) -> float:
    t = TIERS.get(tier)
    return float(t["multiplier"]) if t else 0.0


def effective_limits(tier: str) -> dict[str, float]:
    """Devuelve los limites por ventana ajustados al tier."""
    from .go_prices import LIMITS

    m = multiplier(tier)
    return {k: round(v * m, 2) for k, v in LIMITS.items()}


def tier_label(tier: str) -> str:
    t = TIERS.get(tier)
    return t["label"] if t else tier
