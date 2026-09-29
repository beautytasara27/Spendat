"""Second vendor adapter: an in-memory store implementing the same interface.

This exists to prove the adapter contract is vendor-agnostic (PRD Phase 2): the same
contract test suite runs against both ``AcpAdapter`` and this class, and the
orchestrator buys from it without any change to policy, approval or orchestration code.
It is also the copy-paste template for a new vendor (see docs/adding-a-vendor.md).
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field

from spenddat.models import Address, Candidate, OrderStatus, Receipt, ResolvedItem
from spenddat.vendors.base import PaymentHandle, VendorAdapter, VendorError, VendorOutcomeUnknown


@dataclass
class _Cart:
    item: ResolvedItem
    status: str = "ready"
    receipt: Receipt | None = None
    idem: dict[str, Receipt] = field(default_factory=dict)


@dataclass(frozen=True)
class StubProduct:
    sku: str
    title: str
    price_minor: int
    in_stock: bool = True


class StubAdapter(VendorAdapter):
    integration = "api"

    def __init__(
        self,
        vendor_id: str,
        catalog: list[StubProduct],
        shipping_minor: int = 0,
        tax_rate_bp: int = 0,
    ) -> None:
        self.vendor_id = vendor_id
        self._catalog = {p.sku: p for p in catalog}
        self._shipping = shipping_minor
        self._tax_bp = tax_rate_bp
        self._carts: dict[str, _Cart] = {}
        self._lock = threading.Lock()
        # Fault injection for tests
        self.fail_next_purchase_with: type[Exception] | None = None
        self.complete_before_failing = False
        self.orders_created = 0

    def search(self, query: str, limit: int = 5) -> list[Candidate]:
        words = {w for w in query.lower().split() if len(w) > 1}
        out = []
        for p in self._catalog.values():
            title = set(p.title.lower().split())
            score = len(words & title) / len(words) if words else 0
            if score >= 0.5 and p.in_stock:
                out.append(
                    Candidate(
                        vendor=self.vendor_id,
                        sku=p.sku,
                        title=p.title,
                        unit_price_minor=p.price_minor,
                        currency="usd",
                        score=round(score, 3),
                    )
                )
        out.sort(key=lambda c: (-c.score, c.unit_price_minor))
        return out[:limit]

    def resolve(self, candidate: Candidate, quantity: int, ship_to: Address) -> ResolvedItem:
        p = self._catalog.get(candidate.sku)
        if p is None or not p.in_stock:
            raise VendorError(f"sku {candidate.sku} unavailable")
        sub = p.price_minor * quantity
        tax = sub * self._tax_bp // 10_000
        item = ResolvedItem(
            vendor=self.vendor_id,
            sku=p.sku,
            title=p.title,
            quantity=quantity,
            unit_price_minor=p.price_minor,
            shipping_minor=self._shipping,
            tax_minor=tax,
            total_minor=sub + tax + self._shipping,
            currency="usd",
            vendor_ref=f"cart_{uuid.uuid4().hex[:12]}",
            merchant_payment_id=f"{self.vendor_id}_acct",
        )
        with self._lock:
            self._carts[item.vendor_ref] = _Cart(item=item)
        return item

    def purchase(
        self, item: ResolvedItem, payment: PaymentHandle, ship_to: Address, idempotency_key: str
    ) -> Receipt:
        with self._lock:
            cart = self._carts.get(item.vendor_ref)
            if cart is None:
                raise VendorError("unknown cart")
            if idempotency_key in cart.idem:
                return cart.idem[idempotency_key]
            if cart.status == "completed":
                raise VendorError("cart already completed with a different idempotency key")
            if cart.status == "canceled":
                raise VendorError("cart was canceled")
            if payment.max_amount_minor < cart.item.total_minor:
                raise VendorError("payment declined: token cap below total")
            fault = self.fail_next_purchase_with
            self.fail_next_purchase_with = None
            if fault and not self.complete_before_failing:
                raise fault("injected failure before order creation")
            receipt = Receipt(
                vendor=self.vendor_id,
                order_id=f"ord_{uuid.uuid4().hex[:10]}",
                vendor_ref=item.vendor_ref,
                amount_minor=cart.item.total_minor,
                currency="usd",
            )
            cart.status, cart.receipt = "completed", receipt
            cart.idem[idempotency_key] = receipt
            self.orders_created += 1
            if fault:
                raise VendorOutcomeUnknown("injected: order created but response lost")
            return receipt

    def order_status(self, vendor_ref: str) -> tuple[OrderStatus, Receipt | None]:
        cart = self._carts.get(vendor_ref)
        if cart is None:
            return OrderStatus.UNKNOWN, None
        if cart.status == "completed":
            return OrderStatus.COMPLETED, cart.receipt
        if cart.status == "canceled":
            return OrderStatus.CANCELED, None
        return OrderStatus.NOT_COMPLETED, None

    def cancel(self, vendor_ref: str) -> None:
        with self._lock:
            cart = self._carts.get(vendor_ref)
            if cart and cart.status == "ready":
                cart.status = "canceled"

    def set_price(self, sku: str, price_minor: int) -> None:
        p = self._catalog[sku]
        self._catalog[sku] = StubProduct(p.sku, p.title, price_minor, p.in_stock)
