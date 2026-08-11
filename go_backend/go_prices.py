"""Precios por modelo de OpenCode Go (USD por 1M tokens) para estimar uso.

Fuente: https://opencode.ai/docs/en/go/ ("prices per 1M tokens").
Los modelos desconocidos usan un default conservador.
"""

from __future__ import annotations

# (input, output, cached_read, cached_write) USD por 1M tokens
_PRICES: dict[str, tuple[float, float, float, float]] = {
    "grok-4.5": (2.00, 6.00, 0.30, 0.0),
    "gpt-5.6-luna": (0.20, 1.20, 0.02, 0.25),          # <=272K tokens
    "glm-5.2": (1.40, 4.40, 0.26, 0.0),
    "glm-5.1": (1.40, 4.40, 0.26, 0.0),
    "glm-5": (1.40, 4.40, 0.26, 0.0),
    "kimi-k3": (3.00, 15.00, 0.30, 0.0),
    "kimi-k2.7-code": (0.95, 4.00, 0.19, 0.0),
    "kimi-k2.6": (0.95, 4.00, 0.16, 0.0),
    "kimi-k2.5": (0.95, 4.00, 0.16, 0.0),
    "mimo-v2.5": (0.14, 0.28, 0.0028, 0.0),
    "mimo-v2.5-pro": (0.435, 0.87, 0.003625, 0.0),
    "minimax-m3": (0.30, 1.20, 0.06, 0.0),
    "minimax-m2.7": (0.30, 1.20, 0.06, 0.375),
    "minimax-m2.5": (0.30, 1.20, 0.06, 0.375),
    "qwen3.8-max": (2.00, 6.00, 0.25, 2.50),
    "qwen3.7-max": (2.50, 7.50, 0.50, 3.125),
    "qwen3.7-plus": (0.40, 1.60, 0.04, 0.50),          # <=256K
    "qwen3.6-plus": (0.50, 3.00, 0.05, 0.625),         # <=256K
    "qwen3.5-plus": (0.50, 3.00, 0.05, 0.625),
    "deepseek-v4-pro": (0.435, 0.87, 0.003625, 0.0),
    "deepseek-v4-flash": (0.14, 0.28, 0.0028, 0.0),
    "hy3": (0.14, 0.58, 0.035, 0.0),
    "hy3-preview": (0.14, 0.58, 0.035, 0.0),
}

# Precios por encima del umbral de contexto largo (por modelo)
_LONG_CONTEXT_PRICES: dict[str, tuple[float, float, float, float]] = {
    "gpt-5.6-luna": (0.40, 1.80, 0.04, 0.50),          # >272K tokens
    "qwen3.7-plus": (1.20, 4.80, 0.12, 1.50),          # >256K
    "qwen3.6-plus": (2.00, 6.00, 0.20, 2.50),          # >256K
}

_LONG_CONTEXT_THRESHOLD: dict[str, int] = {
    "gpt-5.6-luna": 272_000,
    "qwen3.7-plus": 256_000,
    "qwen3.6-plus": 256_000,
}

DEFAULT_PRICES = (0.20, 1.20, 0.02, 0.25)  # conservador (como Luna)

LIMITS = {"5h": 12.0, "week": 30.0, "month": 60.0}
WINDOWS = {"5h": 5 * 3600, "week": 7 * 86400, "month": 30 * 86400}


def prices_for(model: str, total_tokens: int | None = None) -> tuple[float, float, float, float]:
    key = (model or "").lower().strip()
    if key.startswith("openai/"):
        key = key.split("/", 1)[1]
    if key.startswith("deepseek/"):
        key = key.split("/", 1)[1]
    if key not in _PRICES:
        return DEFAULT_PRICES
    base = _PRICES[key]
    if total_tokens is not None and key in _LONG_CONTEXT_THRESHOLD:
        if total_tokens > _LONG_CONTEXT_THRESHOLD[key]:
            return _LONG_CONTEXT_PRICES.get(key, base)
    return base


def estimate_cost_usd(
    model: str,
    input_tokens: int | None,
    output_tokens: int | None,
    cached_read: int | None,
    cached_write: int | None,
) -> float:
    if input_tokens is None and output_tokens is None:
        return 0.0
    total = (input_tokens or 0) + (output_tokens or 0)
    p_in, p_out, p_cached, p_write = prices_for(model, total)
    cost = 0.0
    if input_tokens:
        uncached = max(0, input_tokens - (cached_read or 0))
        cost += uncached / 1_000_000 * p_in
    if output_tokens:
        cost += output_tokens / 1_000_000 * p_out
    if cached_read:
        cost += cached_read / 1_000_000 * p_cached
    if cached_write:
        cost += cached_write / 1_000_000 * p_write
    return round(cost, 6)
