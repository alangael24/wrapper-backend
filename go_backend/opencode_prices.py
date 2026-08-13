"""Integer cost estimates for the optional OpenCode Go provider override."""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

# (uncached input, output, cache read, cache write), USD per million tokens.
_PRICES: dict[str, tuple[float, float, float, float]] = {
    "grok-4.5": (2.00, 6.00, 0.30, 0.0),
    "glm-5.2": (1.40, 4.40, 0.26, 0.0),
    "glm-5.1": (1.40, 4.40, 0.26, 0.0),
    "kimi-k3": (3.00, 15.00, 0.30, 0.0),
    "kimi-k2.7-code": (0.95, 4.00, 0.19, 0.0),
    "kimi-k2.6": (0.95, 4.00, 0.16, 0.0),
    "deepseek-v4-pro": (0.435, 0.87, 0.003625, 0.0),
    "deepseek-v4-flash": (0.14, 0.28, 0.0028, 0.0),
    "mimo-v2.5": (0.14, 0.28, 0.0028, 0.0),
    "mimo-v2.5-pro": (0.435, 0.87, 0.003625, 0.0),
    "hy3": (0.14, 0.58, 0.035, 0.0),
    "hy3-preview": (0.14, 0.58, 0.035, 0.0),
}

DEFAULT_PRICES = (2.00, 6.00, 0.30, 0.0)


def prices_for(model: str) -> tuple[float, float, float, float]:
    key = (model or "").lower().strip()
    for prefix in ("opencode-go/", "openai/", "deepseek/"):
        if key.startswith(prefix):
            key = key.split("/", 1)[1]
            break
    return _PRICES.get(key, DEFAULT_PRICES)


def estimate_cost_microusd(
    model: str,
    input_tokens: int | None,
    output_tokens: int | None,
    cached_read: int | None,
    cached_write: int | None,
) -> int:
    if input_tokens is None and output_tokens is None:
        return 0
    p_in, p_out, p_cached, p_write = prices_for(model)
    uncached = max(0, (input_tokens or 0) - (cached_read or 0))
    cost = Decimal(uncached) * Decimal(str(p_in))
    cost += Decimal(output_tokens or 0) * Decimal(str(p_out))
    cost += Decimal(cached_read or 0) * Decimal(str(p_cached))
    cost += Decimal(cached_write or 0) * Decimal(str(p_write))
    return max(0, int(cost.quantize(Decimal("1"), rounding=ROUND_HALF_UP)))
