"""Integer credit economics for Agent Genia runs."""

from __future__ import annotations

from dataclasses import dataclass
import time

from .tiers import TRIAL_CREDIT_MILLI

CREDIT_MILLI_PER_CREDIT = 1_000
MICROUSD_PER_CREDIT = 10_000


def ceil_div(numerator: int, denominator: int) -> int:
    if denominator <= 0:
        raise ValueError("denominator must be positive")
    return -(-numerator // denominator)


def billable_credit_milli(
    *,
    llm_cost_microusd: int,
    extra_cost_microusd: int = 0,
    llm_multiplier_bps: int = 12_500,
    display_increment_milli: int = 100,
) -> int:
    """Normalize a whole run and round only once at final settlement."""
    if llm_multiplier_bps < 0 or display_increment_milli <= 0:
        raise ValueError("invalid credit conversion configuration")
    normalized_llm = ceil_div(max(0, llm_cost_microusd) * llm_multiplier_bps, 10_000)
    normalized_total = normalized_llm + max(0, extra_cost_microusd)
    raw_milli = ceil_div(normalized_total, 10)
    return ceil_div(raw_milli, display_increment_milli) * display_increment_milli


def credits_float(milli: int) -> float:
    return round(max(0, milli) / CREDIT_MILLI_PER_CREDIT, 3)


@dataclass(frozen=True)
class CreditConfig:
    mode: str = "shadow"
    llm_multiplier_bps: int = 12_500
    display_increment_milli: int = 100
    trial_credit_milli: int = TRIAL_CREDIT_MILLI
    trial_ttl_days: int = 30
    default_run_max_milli: int = 25_000
    deep_run_max_milli: int = 50_000
    reservation_ttl_seconds: int = 3_900

    def validate(self) -> None:
        if self.mode not in {"off", "shadow", "enforce"}:
            raise ValueError("CREDITS_MODE debe ser off, shadow o enforce")
        if not 10_000 <= self.llm_multiplier_bps <= 100_000:
            raise ValueError("CREDIT_LLM_MULTIPLIER_BPS fuera de rango")
        if self.display_increment_milli <= 0:
            raise ValueError("CREDIT_DISPLAY_INCREMENT_MILLI debe ser positivo")
        if self.trial_credit_milli < 0 or self.trial_ttl_days <= 0:
            raise ValueError("Configuración de trial inválida")
        if not 100 <= self.default_run_max_milli <= self.deep_run_max_milli:
            raise ValueError("DEFAULT_RUN_MAX_CREDITS inválido")
        if self.reservation_ttl_seconds < 60:
            raise ValueError("CREDIT_RESERVATION_TTL_SECONDS demasiado bajo")


class CreditError(RuntimeError):
    def __init__(self, message: str, *, status: int = 400, code: str = "credit_error"):
        super().__init__(message)
        self.status = status
        self.code = code


class CreditService:
    def __init__(self, store, config: CreditConfig, *, now=time.time):
        self.store = store
        self.config = config
        self._now = now

    def ensure_trial(self, user_id: str) -> dict | None:
        if self.config.trial_credit_milli <= 0:
            return None
        source_key = f"trial:{user_id}"
        existing = self.store.get_credit_grant_by_source(source_key)
        if existing:
            return existing
        now = self._now()
        return self.store.grant_credits(
            user_id=user_id,
            amount_milli=self.config.trial_credit_milli,
            source_type="trial",
            source_key=source_key,
            starts_at=now,
            expires_at=now + self.config.trial_ttl_days * 86_400,
            metadata={"ttl_days": self.config.trial_ttl_days},
        )

    def billable_milli(self, llm_cost_microusd: int, extra_cost_microusd: int = 0) -> int:
        return billable_credit_milli(
            llm_cost_microusd=llm_cost_microusd,
            extra_cost_microusd=extra_cost_microusd,
            llm_multiplier_bps=self.config.llm_multiplier_bps,
            display_increment_milli=self.config.display_increment_milli,
        )
