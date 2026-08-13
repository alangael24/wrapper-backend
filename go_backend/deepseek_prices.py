"""Cost estimation for the server-owned DeepSeek API account.

Prices are USD per one million tokens. DeepSeek's context cache is automatic,
so cached input is billed at the cache-hit price and the remainder at the
cache-miss price.
"""

from __future__ import annotations

# (cache miss input, output, cache hit input, cache write) USD / 1M tokens.
_PRICES: dict[str, tuple[float, float, float, float]] = {
    "deepseek-v4-flash": (0.14, 0.28, 0.0028, 0.0),
    "deepseek-v4-pro": (0.435, 0.87, 0.003625, 0.0),
}

DEFAULT_PRICES = _PRICES["deepseek-v4-flash"]

# Product usage budgets. These are Agent Genia entitlements, not provider
# subscriptions; the provider account is owned and paid by Agent Genia.
LIMITS = {"5h": 12.0, "week": 30.0, "month": 60.0}
WINDOWS = {"5h": 5 * 3600, "week": 7 * 86400, "month": 30 * 86400}


def prices_for(model: str, total_tokens: int | None = None) -> tuple[float, float, float, float]:
    del total_tokens
    key = (model or "").lower().strip()
    if key.startswith("deepseek/"):
        key = key.split("/", 1)[1]
    return _PRICES.get(key, DEFAULT_PRICES)


def estimate_cost_usd(
    model: str,
    input_tokens: int | None,
    output_tokens: int | None,
    cached_read: int | None,
    cached_write: int | None,
) -> float:
    if input_tokens is None and output_tokens is None:
        return 0.0
    p_in, p_out, p_cached, p_write = prices_for(model)
    uncached = max(0, (input_tokens or 0) - (cached_read or 0))
    cost = uncached / 1_000_000 * p_in
    cost += (output_tokens or 0) / 1_000_000 * p_out
    cost += (cached_read or 0) / 1_000_000 * p_cached
    cost += (cached_write or 0) / 1_000_000 * p_write
    return round(cost, 6)
