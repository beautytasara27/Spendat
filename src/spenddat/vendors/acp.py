"""Vendor A: any merchant that implements the Agentic Commerce Protocol (ACP).

ACP is an open REST standard (co-developed by OpenAI and Stripe) that merchants expose
specifically so that AI agents can buy from them: create / update / complete checkout
sessions, paid with a Stripe Shared Payment Token. Because the merchant publishes these
endpoints for agents, automated purchasing is explicitly sanctioned - no scraping and no
browser automation, so no Terms-of-Service conflict (see docs/adr/0004).

Product discovery uses the merchant's published product feed (OpenAI Product Feed Spec
JSON), since ACP itself starts at checkout.

Sandbox vs live changes only the base URL, feed URL, API key and seller profile passed in
by configuration - this class has no mode-specific branches.
"""

from __future__ import annotations

import re
import time
import uuid
from typing import Any

import httpx

from spenddat.models import Address, Candidate, OrderStatus, Receipt, ResolvedItem
from spenddat.observability import get_logger, log
from spenddat.vendors.base import (
    PaymentHandle,
    VendorAdapter,
    VendorError,
    VendorOutcomeUnknown,
)

_log = get_logger("vendors.acp")
_WORD = re.compile(r"[a-z0-9]+")
_STOP = {"a", "an", "the", "for", "of", "and", "with", "to", "in", "my", "me", "some", "buy"}


def _stem(w: str) -> str:
    # light plural folding so "sambas"/"samba", "cases"/"case", "shoes"/"shoe" match
    if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
        return w[:-1]
    return w


def _tokens(text: str) -> set[str]:
    return {_stem(w) for w in _WORD.findall(text.lower()) if w not in _STOP and len(w) > 1}


def _opt(v: Any) -> str | None:
    return str(v).strip() or None if v not in (None, "") else None


def parse_feed_price(value: Any) -> tuple[int, str]:
    """Feed prices look like '24.99 USD'. Returns (minor units, lowercase currency)."""
    if isinstance(value, dict):  # {"amount": 2499, "currency": "usd"}
        return int(value["amount"]), str(value["currency"]).lower()
    m = re.fullmatch(r"\s*([0-9]+(?:\.[0-9]{1,2})?)\s+([A-Za-z]{3})\s*", str(value))
    if not m:
        raise VendorError(f"unparseable feed price {value!r}")
    amount, cur = m.groups()
    whole, _, frac = amount.partition(".")
    return int(whole) * 100 + int((frac + "00")[:2]), cur.lower()


def _addr(a: Address) -> dict[str, Any]:
    d = {
        "name": a.name,
        "line_one": a.line_one,
        "city": a.city,
        "state": a.state,
        "country": a.country,
        "postal_code": a.postal_code,
    }
    if a.line_two:
        d["line_two"] = a.line_two
    return d


def _total(session: dict[str, Any], kind: str) -> int:
    for t in session.get("totals", []):
        if t.get("type") == kind:
            return int(t["amount"])
    return 0


class AcpAdapter(VendorAdapter):
    integration = "api"

    def __init__(
        self,
        vendor_id: str,
        base_url: str,
        feed_url: str,
        api_key: str,
        seller_profile: str,
        api_version: str = "2025-09-29",
        buyer: dict[str, str] | None = None,
        client: httpx.Client | None = None,
        feed_ttl_s: float = 300.0,
    ) -> None:
        self.vendor_id = vendor_id
        self._base = base_url.rstrip("/")
        self._feed_url = feed_url
        self._key = api_key
        self._seller_profile = seller_profile
        self._api_version = api_version
        self._buyer = buyer
        self._http = client or httpx.Client(timeout=httpx.Timeout(20.0, connect=5.0))
        self._feed: list[dict[str, Any]] | None = None
        self._feed_at = 0.0
        self._feed_ttl = feed_ttl_s

    # ------------------------------------------------------------------ http

    def _headers(self, idempotency_key: str | None = None) -> dict[str, str]:
        h = {
            "Authorization": f"Bearer {self._key}",
            "API-Version": self._api_version,
            "Request-Id": str(uuid.uuid4()),
            "Content-Type": "application/json",
        }
        if idempotency_key:
            h["Idempotency-Key"] = idempotency_key
        return h

    def _call(
        self,
        method: str,
        path: str,
        body: dict | None = None,
        *,
        idempotency_key: str | None = None,
        submit: bool = False,
    ) -> dict[str, Any]:
        """``submit=True`` marks calls where a lost response means 'outcome unknown'."""
        url = f"{self._base}{path}"
        try:
            r = self._http.request(method, url, json=body, headers=self._headers(idempotency_key))
        except httpx.TransportError as e:
            if submit:
                raise VendorOutcomeUnknown(f"{method} {path}: {type(e).__name__}") from e
            raise VendorError(f"{method} {path}: {type(e).__name__}") from e
        if r.status_code >= 500 and submit:
            raise VendorOutcomeUnknown(f"{method} {path}: HTTP {r.status_code}")
        if r.status_code >= 400:
            raise VendorError(f"{method} {path}: HTTP {r.status_code} {r.text[:300]}")
        data = r.json()
        errors = [m for m in data.get("messages", []) if m.get("type") == "error"]
        if errors:
            raise VendorError(f"{method} {path}: merchant error {errors[0].get('content')!r}")
        return data

    # ------------------------------------------------------------------ interface

    def _load_feed(self) -> list[dict[str, Any]]:
        if self._feed is None or time.monotonic() - self._feed_at > self._feed_ttl:
            try:
                r = self._http.get(self._feed_url, headers={"Accept": "application/json"})
                r.raise_for_status()
            except httpx.HTTPError as e:
                raise VendorError(f"product feed unavailable: {type(e).__name__}") from e
            data = r.json()
            self._feed = data["products"] if isinstance(data, dict) else data
            self._feed_at = time.monotonic()
        return self._feed

    def search(self, query: str, limit: int = 5) -> list[Candidate]:
        q = _tokens(query)
        if not q:
            return []
        out: list[Candidate] = []
        for p in self._load_feed():
            if not p.get("enable_checkout", True):
                continue
            text = (
                f"{p.get('title', '')} {p.get('description', '')} {p.get('product_category', '')}"
            )
            score = len(q & _tokens(text)) / len(q)
            if score < 0.5:
                continue
            price, cur = parse_feed_price(p["price"])
            out.append(
                Candidate(
                    vendor=self.vendor_id,
                    sku=str(p["id"]),
                    title=str(p["title"]),
                    unit_price_minor=price,
                    currency=cur,
                    in_stock=p.get("availability", "in_stock") == "in_stock",
                    url=p.get("link"),
                    score=round(score, 3),
                    group_id=_opt(p.get("item_group_id")),
                    size=_opt(p.get("size")),
                    color=_opt(p.get("color")),
                )
            )
        out = [c for c in out if c.in_stock]
        out.sort(key=lambda c: (-c.score, c.unit_price_minor))
        log(_log, 20, "search", query=query, results=len(out))
        return out[:limit]

    def resolve(self, candidate: Candidate, quantity: int, ship_to: Address) -> ResolvedItem:
        session = self._call(
            "POST",
            "/checkout_sessions",
            {
                "items": [{"id": candidate.sku, "quantity": quantity}],
                "fulfillment_address": _addr(ship_to),
            },
        )
        sid = session["id"]
        options = session.get("fulfillment_options") or []
        if options and not session.get("fulfillment_option_id"):
            cheapest = min(options, key=lambda o: int(o.get("total", 0)))
            session = self._call(
                "POST", f"/checkout_sessions/{sid}", {"fulfillment_option_id": cheapest["id"]}
            )
        if session.get("status") != "ready_for_payment":
            self.cancel(sid)
            raise VendorError(f"checkout session not ready: status={session.get('status')}")
        lines = session.get("line_items", [])
        if (
            len(lines) != 1
            or lines[0]["item"]["id"] != candidate.sku
            or int(lines[0]["item"]["quantity"]) != quantity
        ):
            self.cancel(sid)
            raise VendorError("merchant returned a cart that does not match the requested item")
        line = lines[0]
        currency = str(session.get("currency", candidate.currency)).lower()
        unit = int(line["base_amount"]) // quantity
        return ResolvedItem(
            vendor=self.vendor_id,
            sku=candidate.sku,
            title=candidate.title,
            quantity=quantity,
            unit_price_minor=unit,
            shipping_minor=_total(session, "fulfillment"),
            tax_minor=_total(session, "tax"),
            total_minor=_total(session, "total"),
            currency=currency,
            vendor_ref=sid,
            merchant_payment_id=self._seller_profile,
        )

    def purchase(
        self, item: ResolvedItem, payment: PaymentHandle, ship_to: Address, idempotency_key: str
    ) -> Receipt:
        if payment.max_amount_minor < item.total_minor:
            raise VendorError("payment token cap is below the order total")
        body: dict[str, Any] = {
            "payment_data": {
                "token": payment.token,
                "provider": payment.provider,
                "billing_address": _addr(ship_to),
            }
        }
        if self._buyer:
            body["buyer"] = self._buyer
        session = self._call(
            "POST",
            f"/checkout_sessions/{item.vendor_ref}/complete",
            body,
            idempotency_key=idempotency_key,
            submit=True,
        )
        return self._receipt(session, item.vendor_ref)

    def _receipt(self, session: dict[str, Any], sid: str) -> Receipt:
        order = session.get("order") or {}
        if session.get("status") != "completed" or not order.get("id"):
            raise VendorError(f"checkout not completed: status={session.get('status')}")
        return Receipt(
            vendor=self.vendor_id,
            order_id=str(order["id"]),
            vendor_ref=sid,
            amount_minor=_total(session, "total"),
            currency=str(session.get("currency", "usd")),
            permalink_url=order.get("permalink_url"),
        )

    def health_check(self) -> dict[str, Any]:
        self._feed = None  # force a fresh fetch
        feed = self._load_feed()
        return {"ok": True, "products": sum(1 for p in feed if p.get("enable_checkout", True))}

    def order_status(self, vendor_ref: str) -> tuple[OrderStatus, Receipt | None]:
        try:
            s = self._call("GET", f"/checkout_sessions/{vendor_ref}")
        except VendorError:
            return OrderStatus.UNKNOWN, None
        status = s.get("status")
        if status == "completed":
            return OrderStatus.COMPLETED, self._receipt(s, vendor_ref)
        if status == "canceled":
            return OrderStatus.CANCELED, None
        return OrderStatus.NOT_COMPLETED, None

    def cancel(self, vendor_ref: str) -> None:
        try:
            self._call("POST", f"/checkout_sessions/{vendor_ref}/cancel", {})
        except (VendorError, VendorOutcomeUnknown):
            log(_log, 30, "cancel_failed", vendor_ref=vendor_ref)
