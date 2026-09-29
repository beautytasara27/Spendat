"""Typed domain models shared by every component.

All money is represented as integer minor units (cents) to avoid float rounding.
These models are deliberately free of behaviour: components exchange them across
boundaries, and the policy engine consumes them as pure input.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

ANY_ALLOWED_VENDOR = "any_allowed_vendor"


class Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class RuntimeMode(StrEnum):
    SANDBOX = "sandbox"
    LIVE = "live"


# --------------------------------------------------------------------------- intent


class PurchaseIntent(Frozen):
    """Structured, schema-validated output of the intent parser."""

    vendor: str = Field(description="Vendor id, or 'any_allowed_vendor' if unspecified")
    product_description: str = Field(min_length=2, max_length=300)
    category: str = Field(min_length=2, max_length=64)
    quantity: int = Field(ge=1, le=100)
    max_price_minor: int | None = Field(default=None, ge=1, description="Per-order ceiling")
    currency: str = Field(default="usd", pattern=r"^[a-z]{3}$")
    constraints: tuple[str, ...] = ()

    @field_validator("vendor", "category")
    @classmethod
    def _slug(cls, v: str) -> str:
        return v.strip().lower().replace(" ", "_").replace("-", "_")


class Clarification(Frozen):
    kind: Literal["clarification"] = "clarification"
    question: str
    missing_fields: tuple[str, ...] = ()


class UnsupportedRequest(Frozen):
    kind: Literal["unsupported"] = "unsupported"
    reason_code: str
    explanation: str


# --------------------------------------------------------------------------- vendor data


class Address(Frozen):
    name: str
    line_one: str
    line_two: str | None = None
    city: str
    state: str
    postal_code: str
    country: str = Field(pattern=r"^[A-Z]{2}$")


class Candidate(Frozen):
    vendor: str
    sku: str
    title: str
    unit_price_minor: int = Field(ge=0)
    currency: str
    in_stock: bool = True
    url: str | None = None
    score: float = 0.0


class ResolvedItem(Frozen):
    """A product the vendor has confirmed, with an authoritative price."""

    vendor: str
    sku: str
    title: str
    quantity: int = Field(ge=1)
    unit_price_minor: int = Field(ge=0)
    shipping_minor: int = Field(ge=0)
    tax_minor: int = Field(ge=0)
    total_minor: int = Field(ge=0)
    currency: str
    vendor_ref: str = Field(description="Vendor-side handle, e.g. ACP checkout_session_id")
    merchant_payment_id: str | None = Field(
        default=None, description="Seller identifier the payment token is scoped to"
    )


class Receipt(Frozen):
    vendor: str
    order_id: str
    vendor_ref: str
    amount_minor: int
    currency: str
    permalink_url: str | None = None


class OrderStatus(StrEnum):
    COMPLETED = "completed"
    NOT_COMPLETED = "not_completed"
    CANCELED = "canceled"
    UNKNOWN = "unknown"


# --------------------------------------------------------------------------- policy


class Outcome(StrEnum):
    ALLOW = "ALLOW"
    ESCALATE = "ESCALATE"
    DENY = "DENY"


class DataQuality(Frozen):
    """Facts about how cleanly the transaction resolved. Computed by the orchestrator."""

    candidate_count: int = Field(ge=0)
    has_shipping_address: bool
    has_price: bool
    ambiguous: bool = False


class TransactionFacts(Frozen):
    """Everything the policy engine needs about one proposed transaction."""

    vendor: str
    category: str
    quantity: int
    unit_price_minor: int | None
    total_minor: int | None
    currency: str
    user_max_price_minor: int | None = None
    sku: str | None = None
    data: DataQuality


class SpendContext(Frozen):
    """Point-in-time aggregates from the audit store (computed outside the engine)."""

    now: datetime
    daily_spend_minor: int = 0
    weekly_spend_minor: int = 0
    recent_purchase_count: int = 0  # within policy velocity window
    reference_price_minor: int | None = None  # rolling average for sku/category


class Reason(Frozen):
    code: str
    detail: str


class Decision(Frozen):
    outcome: Outcome
    reasons: tuple[Reason, ...]
    policy_version: str

    @property
    def reason_codes(self) -> tuple[str, ...]:
        return tuple(r.code for r in self.reasons)

    def explanation(self) -> str:
        if not self.reasons:
            return "all policy checks passed"
        return "; ".join(r.detail for r in self.reasons)


# --------------------------------------------------------------------------- approvals


class ApprovalAction(StrEnum):
    APPROVE = "approve"
    DENY = "deny"
    APPROVE_WITH_MODIFICATION = "approve_with_modification"


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"
    EXPIRED = "expired"


class Modification(Frozen):
    max_total_minor: int | None = Field(default=None, ge=1)
    quantity: int | None = Field(default=None, ge=1)


# --------------------------------------------------------------------------- results


class TxnState(StrEnum):
    RECEIVED = "received"
    NEEDS_CLARIFICATION = "needs_clarification"
    REJECTED = "rejected"
    DENIED = "denied"
    PENDING_APPROVAL = "pending_approval"
    APPROVAL_DENIED = "approval_denied"
    APPROVAL_EXPIRED = "approval_expired"
    EXECUTING = "executing"
    COMPLETED = "completed"
    FAILED = "failed"
    NEEDS_RECONCILIATION = "needs_reconciliation"
    BLOCKED_BY_CIRCUIT_BREAKER = "blocked_by_circuit_breaker"


TERMINAL_STATES = frozenset(
    {
        TxnState.NEEDS_CLARIFICATION,
        TxnState.REJECTED,
        TxnState.DENIED,
        TxnState.APPROVAL_DENIED,
        TxnState.APPROVAL_EXPIRED,
        TxnState.COMPLETED,
        TxnState.FAILED,
        TxnState.BLOCKED_BY_CIRCUIT_BREAKER,
    }
)


class TxnResult(Frozen):
    txn_id: str
    state: TxnState
    message: str
    decision: Decision | None = None
    receipt: Receipt | None = None
    approval_id: str | None = None
    mode: RuntimeMode
