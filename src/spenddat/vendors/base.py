"""The vendor-agnostic execution interface.

Every vendor integration - official API or browser automation, sandbox or live -
implements ``VendorAdapter``. The orchestrator, policy engine and approval layer only
ever see this interface, so adding a vendor means writing one class and registering it.

Contract (enforced by ``tests/contract``):

* ``search`` is read-only and may return 0..n candidates, best first.
* ``resolve`` asks the vendor for an authoritative price for one candidate shipped to
  ``ship_to``; it must not charge anything. It returns a ``ResolvedItem`` whose
  ``vendor_ref`` identifies the vendor-side cart/session.
* ``purchase`` completes the resolved order with a scoped payment token. It must be
  idempotent for a given ``idempotency_key``: calling it twice never creates two orders.
* ``order_status`` lets the orchestrator reconcile a purchase whose outcome is unknown
  (e.g. timeout after payment was submitted) without re-submitting it.
* ``cancel`` releases a resolved-but-unpurchased cart. Best effort.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from spenddat.models import Address, Candidate, OrderStatus, Receipt, ResolvedItem, RuntimeMode


class VendorError(RuntimeError):
    """The vendor definitively rejected the operation (safe to treat as not purchased)."""


class VendorOutcomeUnknown(RuntimeError):
    """The request may or may not have taken effect (timeout, 5xx after submit, etc.)."""


@dataclass(frozen=True)
class PaymentHandle:
    """What an adapter receives to pay with. Never contains a card number."""

    token: str  # e.g. Stripe shared payment token id (spt_...)
    provider: str  # e.g. "stripe"
    max_amount_minor: int
    currency: str


class VendorAdapter(ABC):
    vendor_id: str
    integration: str  # "api" | "browser"
    display_name: str | None = None
    # "stripe_spt": Spenddat issues a capped Stripe shared payment token per purchase.
    # "merchant_wallet": the store charges a card saved in your account with that store
    # (e.g. Shop Pay); Spenddat never handles a credential, only caps the approved total.
    payment_kind: str = "stripe_spt"
    # True when the store requires the buyer to approve each order (Shopify does);
    # the policy engine then always escalates purchases from it.
    requires_buyer_confirmation: bool = False

    @property
    def name(self) -> str:
        return self.display_name or self.vendor_id.replace("_", " ").title()

    @abstractmethod
    def search(self, query: str, limit: int = 5) -> list[Candidate]: ...

    @abstractmethod
    def resolve(self, candidate: Candidate, quantity: int, ship_to: Address) -> ResolvedItem: ...

    @abstractmethod
    def purchase(
        self, item: ResolvedItem, payment: PaymentHandle, ship_to: Address, idempotency_key: str
    ) -> Receipt: ...

    @abstractmethod
    def order_status(self, vendor_ref: str) -> tuple[OrderStatus, Receipt | None]: ...

    def health_check(self) -> dict[str, Any]:
        """Cheap reachability probe for the Stores page. Raise VendorError if unreachable."""
        return {"ok": True}

    def cancel(self, vendor_ref: str) -> None:  # noqa: B027 - optional hook
        """Best-effort release of an unpurchased cart."""


AdapterFactory = Callable[[RuntimeMode], VendorAdapter]


class AdapterRegistry:
    """Maps vendor ids to adapter instances. The only place vendors are wired in."""

    def __init__(self) -> None:
        self._adapters: dict[str, VendorAdapter] = {}

    def register(self, adapter: VendorAdapter) -> None:
        if adapter.vendor_id in self._adapters:
            raise ValueError(f"vendor '{adapter.vendor_id}' already registered")
        self._adapters[adapter.vendor_id] = adapter

    def get(self, vendor_id: str) -> VendorAdapter | None:
        return self._adapters.get(vendor_id)

    def vendor_ids(self) -> list[str]:
        return sorted(self._adapters)

    def display_names(self) -> dict[str, str]:
        return {v: self._adapters[v].name for v in self.vendor_ids()}
