"""Hard circuit breakers, independent of the configurable policy layer.

These ceilings come from process configuration (``SPENDDAT_HARD_MAX_*``), not from the
policy file, and are checked immediately before a payment credential is issued - after
the policy decision *and* after any human approval. A human cannot approve past them and
a policy edit cannot raise them. The check and the spend reservation happen in the same
serialised DB transaction, so two concurrent purchases cannot both slip under the limit.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from spenddat.audit import AuditStore
from spenddat.models import RuntimeMode


@dataclass(frozen=True)
class HardLimits:
    max_transaction_minor: int
    max_daily_minor: int
    max_weekly_minor: int

    def __post_init__(self) -> None:
        for name in ("max_transaction_minor", "max_daily_minor", "max_weekly_minor"):
            if getattr(self, name) <= 0:
                raise ValueError(f"hard limit {name} must be positive")
        if self.max_daily_minor > self.max_weekly_minor:
            raise ValueError("hard daily limit cannot exceed hard weekly limit")


class CircuitBreakerTripped(RuntimeError):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


def check_hard_limits(
    limits: HardLimits, store: AuditStore, mode: RuntimeMode, amount_minor: int, now: datetime
) -> None:
    """Raise ``CircuitBreakerTripped`` if committing ``amount_minor`` would breach a ceiling.

    Must be called inside ``store.atomic()`` together with the spend reservation.
    """
    if amount_minor > limits.max_transaction_minor:
        raise CircuitBreakerTripped(
            "hard_transaction_ceiling",
            f"{amount_minor} > absolute max {limits.max_transaction_minor} per transaction",
        )
    day = store.spend_since(mode, now - timedelta(days=1)) + amount_minor
    if day > limits.max_daily_minor:
        raise CircuitBreakerTripped(
            "hard_daily_ceiling", f"{day} would exceed absolute daily max {limits.max_daily_minor}"
        )
    week = store.spend_since(mode, now - timedelta(days=7)) + amount_minor
    if week > limits.max_weekly_minor:
        raise CircuitBreakerTripped(
            "hard_weekly_ceiling",
            f"{week} would exceed absolute weekly max {limits.max_weekly_minor}",
        )
