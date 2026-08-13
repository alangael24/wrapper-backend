"""Cost estimation for the server-owned DeepSeek API account.

Prices are USD per one million tokens. DeepSeek's context cache is automatic,
so cached input is billed at the cache-hit price and the remainder at the
cache-miss price.
"""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

# (cache miss input, output, cache hit input, cache write) USD / 1M tokens.
_PRICES: dict[str, tuple[float, float, float, float]] = {
    "deepseek-v4-flash": (0.14, 0.28, 0.0028, 0.0),
    "deepseek-v4-pro": (0.435, 0.87, 0.003625, 0.0),
}

DEFAULT_PRICES = _PRICES["deepseek-v4-flash"]

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
    return estimate_cost_microusd(
        model, input_tokens, output_tokens, cached_read, cached_write
    ) / 1_000_000


def estimate_cost_microusd(
    model: str,
    input_tokens: int | None,
    output_tokens: int | None,
    cached_read: int | None,
    cached_write: int | None,
) -> int:
    """Return cost in integer millionths of USD without float balance drift."""
    if input_tokens is None and output_tokens is None:
        return 0
    p_in, p_out, p_cached, p_write = prices_for(model)
    uncached = max(0, (input_tokens or 0) - (cached_read or 0))
    # Prices are USD per 1M tokens. Multiplying tokens by the price yields
    # microUSD directly. Round only the aggregate provider call once.
    cost_microusd = Decimal(uncached) * Decimal(str(p_in))
    cost_microusd += Decimal(output_tokens or 0) * Decimal(str(p_out))
    cost_microusd += Decimal(cached_read or 0) * Decimal(str(p_cached))
    cost_microusd += Decimal(cached_write or 0) * Decimal(str(p_write))
    return max(0, int(cost_microusd.quantize(Decimal("1"), rounding=ROUND_HALF_UP)))
