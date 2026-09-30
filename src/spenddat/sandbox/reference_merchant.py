"""Reference Agentic Commerce Protocol merchant for sandbox use.

A small, spec-shaped ACP merchant (product feed + checkout sessions) used by the
automated tests (in-process, via ``httpx.MockTransport``) and for local end-to-end runs
(``spenddat sandbox-merchant``). It is *not* used in live mode - there the adapter points
at a real merchant's ACP endpoint.

If a Stripe **test** secret key is supplied, ``complete`` charges the received shared
payment token with a test-mode PaymentIntent exactly as a real ACP merchant would, so a
local run can exercise real Stripe sandbox money movement end to end.
"""

from __future__ import annotations

import json
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import httpx

DEFAULT_CATALOG: list[dict[str, Any]] = [
    {
        "id": "SKU-CASE-CLR-15",
        "title": "Clear Phone Case for iPhone 15",
        "description": "Slim shock-absorbing clear phone case",
        "price_minor": 1999,
        "product_category": "electronics_accessories",
    },
    {
        "id": "SKU-CABLE-USBC-2M",
        "title": "USB-C Charging Cable 2m",
        "description": "Braided USB-C to USB-C cable, 60W",
        "price_minor": 1299,
        "product_category": "electronics_accessories",
    },
    {
        "id": "SKU-HEADPH-NC700",
        "title": "Noise Cancelling Wireless Headphones",
        "description": "Over-ear bluetooth headphones",
        "price_minor": 8999,
        "product_category": "electronics_accessories",
    },
    {
        "id": "SKU-LAMP-LED-DESK",
        "title": "LED Desk Lamp",
        "description": "Dimmable LED desk lamp with USB port",
        "price_minor": 3999,
        "product_category": "office_supplies",
    },
    {
        "id": "SKU-STAPLER-STD",
        "title": "Standard Office Stapler",
        "description": "Full-strip desktop stapler",
        "price_minor": 949,
        "product_category": "office_supplies",
    },
    {
        "id": "SKU-NOTEBOOK-A5-3",
        "title": "A5 Notebook 3-Pack",
        "description": "Dot grid notebooks",
        "price_minor": 1450,
        "product_category": "office_supplies",
    },
    {
        "id": "SKU-MOUSE-WL-A",
        "title": "Wireless Mouse Silver",
        "description": "Compact wireless mouse",
        "price_minor": 2499,
        "product_category": "electronics_accessories",
    },
    {
        "id": "SKU-MOUSE-WL-B",
        "title": "Wireless Mouse Black",
        "description": "Compact wireless mouse",
        "price_minor": 2599,
        "product_category": "electronics_accessories",
    },
]

FULFILLMENT = [
    {
        "type": "shipping",
        "id": "ship_standard",
        "title": "Standard",
        "subtitle": "4-5 days",
        "subtotal": 499,
        "tax": 0,
        "total": 499,
    },
    {
        "type": "shipping",
        "id": "ship_express",
        "title": "Express",
        "subtitle": "1-2 days",
        "subtotal": 1299,
        "tax": 0,
        "total": 1299,
    },
]


def _price_str(minor: int) -> str:
    return f"{minor // 100}.{minor % 100:02d} USD"


@dataclass
class _Session:
    id: str
    items: list[dict[str, Any]]
    address: dict[str, Any] | None
    fulfillment_option_id: str | None = None
    status: str = "not_ready_for_payment"
    order: dict[str, Any] | None = None
    completions: dict[str, dict[str, Any]] = field(default_factory=dict)


class ReferenceMerchant:
    def __init__(
        self,
        api_key: str = "merchant_test_key",
        catalog: list[dict[str, Any]] | None = None,
        tax_bp: int = 0,
        charger: Callable[[str, int, str], str] | None = None,
    ) -> None:
        self.api_key = api_key
        self.catalog = {p["id"]: dict(p) for p in (catalog or DEFAULT_CATALOG)}
        self.tax_bp = tax_bp
        self.charger = charger
        self.sessions: dict[str, _Session] = {}
        self.orders_created = 0
        self.lock = threading.Lock()
        # fault injection (tests)
        self.drop_next_complete_response = False

    # ------------------------------------------------------------------ helpers

    def feed(self) -> list[dict[str, Any]]:
        return [
            {
                "id": p["id"],
                "title": p["title"],
                "description": p["description"],
                "price": _price_str(p["price_minor"]),
                "availability": p.get("availability", "in_stock"),
                "product_category": p["product_category"],
                "enable_checkout": True,
                "link": f"https://merchant.example/p/{p['id']}",
                **{k: p[k] for k in ("item_group_id", "size", "color") if p.get(k)},
            }
            for p in self.catalog.values()
        ]

    def _render(self, s: _Session) -> dict[str, Any]:
        lines, base = [], 0
        for i, it in enumerate(s.items):
            p = self.catalog[it["id"]]
            amt = p["price_minor"] * it["quantity"]
            tax = amt * self.tax_bp // 10_000
            base += amt
            lines.append(
                {
                    "id": f"li_{i}",
                    "item": dict(it),
                    "base_amount": amt,
                    "discount": 0,
                    "subtotal": amt,
                    "tax": tax,
                    "total": amt + tax,
                }
            )
        tax_total = sum(li["tax"] for li in lines)
        ship = next((f["total"] for f in FULFILLMENT if f["id"] == s.fulfillment_option_id), 0)
        out: dict[str, Any] = {
            "id": s.id,
            "status": s.status,
            "currency": "usd",
            "payment_provider": {"provider": "stripe", "supported_payment_methods": ["card"]},
            "line_items": lines,
            "fulfillment_address": s.address,
            "fulfillment_options": FULFILLMENT if s.address else [],
            "fulfillment_option_id": s.fulfillment_option_id,
            "totals": [
                {"type": "items_base_amount", "display_text": "Items", "amount": base},
                {"type": "subtotal", "display_text": "Subtotal", "amount": base},
                {"type": "fulfillment", "display_text": "Shipping", "amount": ship},
                {"type": "tax", "display_text": "Tax", "amount": tax_total},
                {"type": "total", "display_text": "Total", "amount": base + tax_total + ship},
            ],
            "messages": [],
            "links": [],
        }
        if s.order:
            out["order"] = s.order
        return out

    def _ready(self, s: _Session) -> None:
        if s.status in ("completed", "canceled"):
            return
        s.status = (
            "ready_for_payment"
            if s.address and s.fulfillment_option_id
            else "not_ready_for_payment"
        )

    # ------------------------------------------------------------------ dispatch

    def handle(
        self, method: str, path: str, headers: dict[str, str], body: dict[str, Any] | None
    ) -> tuple[int, dict[str, Any] | list]:
        path = path.split("?")[0]
        if method == "GET" and path == "/feed.json":
            return 200, {"products": self.feed()}
        if headers.get("authorization") != f"Bearer {self.api_key}":
            return 401, {"type": "invalid_request", "code": "unauthorized", "message": "bad key"}
        parts = [p for p in path.split("/") if p]
        if not parts or parts[0] != "checkout_sessions":
            return 404, {"type": "invalid_request", "code": "not_found", "message": path}
        with self.lock:
            if method == "POST" and len(parts) == 1:
                return self._create(body or {})
            s = self.sessions.get(parts[1]) if len(parts) > 1 else None
            if s is None:
                return 404, {"type": "invalid_request", "code": "not_found", "message": "session"}
            if method == "GET" and len(parts) == 2:
                return 200, self._render(s)
            if method == "POST" and len(parts) == 2:
                if s.status in ("completed", "canceled"):
                    return 409, {"type": "invalid_request", "code": "state", "message": s.status}
                fid = (body or {}).get("fulfillment_option_id")
                if fid and any(f["id"] == fid for f in FULFILLMENT):
                    s.fulfillment_option_id = fid
                self._ready(s)
                return 200, self._render(s)
            if method == "POST" and parts[2:] == ["complete"]:
                return self._complete(s, headers, body or {})
            if method == "POST" and parts[2:] == ["cancel"]:
                if s.status == "completed":
                    return 405, {"type": "invalid_request", "code": "state", "message": "done"}
                s.status = "canceled"
                return 200, self._render(s)
        return 404, {"type": "invalid_request", "code": "not_found", "message": path}

    def _create(self, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        items = body.get("items") or []
        for it in items:
            if (
                it.get("id") not in self.catalog
                or int(it.get("quantity", 0)) < 1
                or self.catalog[it["id"]].get("availability", "in_stock") != "in_stock"
            ):
                return 400, {
                    "type": "invalid_request",
                    "code": "invalid_item",
                    "message": f"unknown item {it.get('id')}",
                }
        s = _Session(
            id=f"cs_{uuid.uuid4().hex[:16]}",
            items=[{"id": i["id"], "quantity": int(i["quantity"])} for i in items],
            address=body.get("fulfillment_address"),
        )
        self._ready(s)
        self.sessions[s.id] = s
        return 201, self._render(s)

    def _complete(
        self, s: _Session, headers: dict[str, str], body: dict[str, Any]
    ) -> tuple[int, dict[str, Any]]:
        idem = headers.get("idempotency-key")
        if idem and idem in s.completions:
            return 200, s.completions[idem]
        if s.status != "ready_for_payment":
            return 409, {"type": "invalid_request", "code": "state", "message": s.status}
        pd = body.get("payment_data") or {}
        token = str(pd.get("token", ""))
        if not token.startswith("spt_") or pd.get("provider") != "stripe":
            return 400, {
                "type": "invalid_request",
                "code": "payment_declined",
                "message": "a Stripe shared payment token is required",
            }
        total = self._render(s)["totals"][-1]["amount"]
        charge_id = self.charger(token, total, s.id) if self.charger else f"pi_sim_{s.id}"
        s.status = "completed"
        s.order = {
            "id": f"ord_{uuid.uuid4().hex[:12]}",
            "checkout_session_id": s.id,
            "permalink_url": f"https://merchant.example/orders/{s.id}",
            "payment_reference": charge_id,
        }
        self.orders_created += 1
        resp = self._render(s)
        if idem:
            s.completions[idem] = resp
        return 200, resp

    # ------------------------------------------------------------------ transports

    def mock_transport(self, base_url: str = "https://merchant.test") -> httpx.MockTransport:
        def _h(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            body = json.loads(request.content) if request.content else None
            if "/complete" in path and self.drop_next_complete_response:
                self.drop_next_complete_response = False
                self.handle(
                    request.method, path, {k.lower(): v for k, v in request.headers.items()}, body
                )
                raise httpx.ReadTimeout("injected: response lost", request=request)
            status, payload = self.handle(
                request.method, path, {k.lower(): v for k, v in request.headers.items()}, body
            )
            return httpx.Response(status, json=payload)

        return httpx.MockTransport(_h)


def stripe_test_charger(secret_key: str) -> Callable[[str, int, str], str]:
    """Charge a granted SPT with a Stripe *test-mode* PaymentIntent (seller side)."""
    if not secret_key.startswith(("sk_test_", "rk_test_")):
        raise ValueError("reference merchant only accepts Stripe test keys")

    def charge(token: str, amount: int, ref: str) -> str:
        r = httpx.post(
            "https://api.stripe.com/v1/payment_intents",
            auth=(secret_key, ""),
            headers={"Stripe-Version": "2026-04-22.preview", "Idempotency-Key": f"pi_{ref}"},
            data={
                "amount": str(amount),
                "currency": "usd",
                "confirm": "true",
                "payment_method_data[shared_payment_granted_token]": token,
            },
            timeout=30,
        )
        r.raise_for_status()
        return str(r.json()["id"])

    return charge


def serve(merchant: ReferenceMerchant, host: str = "127.0.0.1", port: int = 8787) -> None:
    class H(BaseHTTPRequestHandler):
        def _go(self) -> None:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n)) if n else None
            status, payload = merchant.handle(
                self.command, self.path, {k.lower(): v for k, v in self.headers.items()}, body
            )
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        do_GET = _go
        do_POST = _go

        def log_message(self, *args: Any) -> None:  # quiet
            pass

    ThreadingHTTPServer((host, port), H).serve_forever()
