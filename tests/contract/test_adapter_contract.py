"""Adapter contract: every VendorAdapter must pass these, whatever its integration.

To certify a new adapter, add a factory to ``ADAPTERS`` below. No other test changes.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import httpx
import pytest

from spenddat.models import OrderStatus
from spenddat.sandbox.reference_merchant import ReferenceMerchant
from spenddat.vendors.acp import AcpAdapter, parse_feed_price
from spenddat.vendors.base import PaymentHandle, VendorAdapter, VendorError
from spenddat.vendors.stub import StubAdapter, StubProduct
from tests.conftest import SHIP_TO


@dataclass
class Case:
    adapter: VendorAdapter
    query: str
    orders: Callable[[], int]


def _acp() -> Case:
    m = ReferenceMerchant(api_key="k")
    a = AcpAdapter(
        "acp_merchant",
        "https://m.test",
        "https://m.test/feed.json",
        "k",
        "profile_test_x",
        client=httpx.Client(transport=m.mock_transport()),
    )
    return Case(a, "usb-c charging cable", lambda: m.orders_created)


def _stub() -> Case:
    s = StubAdapter(
        "stub_vendor", [StubProduct("S1", "USB-C Charging Cable", 1299)], shipping_minor=300
    )
    return Case(s, "usb-c charging cable", lambda: s.orders_created)


ADAPTERS = {"acp": _acp, "stub": _stub}


@pytest.fixture(params=sorted(ADAPTERS))
def case(request) -> Case:
    return ADAPTERS[request.param]()


def _pay(total: int) -> PaymentHandle:
    return PaymentHandle("spt_test_contract", "stripe", total, "usd")


def test_search_returns_ranked_candidates(case: Case):
    cands = case.adapter.search(case.query)
    assert cands and cands[0].vendor == case.adapter.vendor_id
    assert cands[0].unit_price_minor > 0 and cands[0].currency == "usd"
    assert case.adapter.search("zzqx nonexistent widget") == []


def test_resolve_prices_without_charging(case: Case):
    item = case.adapter.resolve(case.adapter.search(case.query)[0], 2, SHIP_TO)
    assert item.quantity == 2
    assert item.total_minor == item.unit_price_minor * 2 + item.shipping_minor + item.tax_minor
    assert item.vendor_ref and item.merchant_payment_id
    assert case.orders() == 0


def test_purchase_returns_receipt_and_is_idempotent(case: Case):
    item = case.adapter.resolve(case.adapter.search(case.query)[0], 1, SHIP_TO)
    r1 = case.adapter.purchase(item, _pay(item.total_minor), SHIP_TO, "idem-1")
    r2 = case.adapter.purchase(item, _pay(item.total_minor), SHIP_TO, "idem-1")
    assert r1 == r2 and case.orders() == 1
    assert r1.amount_minor == item.total_minor and r1.vendor_ref == item.vendor_ref
    status, receipt = case.adapter.order_status(item.vendor_ref)
    assert status is OrderStatus.COMPLETED and receipt.order_id == r1.order_id


def test_purchase_refuses_token_capped_below_total(case: Case):
    item = case.adapter.resolve(case.adapter.search(case.query)[0], 1, SHIP_TO)
    with pytest.raises(VendorError):
        case.adapter.purchase(item, _pay(item.total_minor - 1), SHIP_TO, "idem-2")
    assert case.orders() == 0


def test_cancel_before_purchase(case: Case):
    item = case.adapter.resolve(case.adapter.search(case.query)[0], 1, SHIP_TO)
    case.adapter.cancel(item.vendor_ref)
    assert case.adapter.order_status(item.vendor_ref)[0] is OrderStatus.CANCELED
    with pytest.raises(VendorError):
        case.adapter.purchase(item, _pay(item.total_minor), SHIP_TO, "idem-3")


def test_order_status_unknown_ref(case: Case):
    assert case.adapter.order_status("does-not-exist")[0] is OrderStatus.UNKNOWN


# ----------------------------------------------------------------------------- ACP specifics


def test_acp_sends_spec_headers_and_payment_data():
    m = ReferenceMerchant(api_key="k")
    seen: list[httpx.Request] = []
    inner = m.mock_transport()

    def spy(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return inner.handle_request(req)

    a = AcpAdapter(
        "acp_merchant",
        "https://m.test",
        "https://m.test/feed.json",
        "k",
        "profile_test_x",
        client=httpx.Client(transport=httpx.MockTransport(spy)),
    )
    item = a.resolve(a.search("desk lamp")[0], 1, SHIP_TO)
    a.purchase(item, _pay(item.total_minor), SHIP_TO, "idem-9")
    complete = next(r for r in seen if r.url.path.endswith("/complete"))
    assert complete.headers["Idempotency-Key"] == "idem-9"
    assert complete.headers["Authorization"] == "Bearer k"
    assert complete.headers["API-Version"] == "2025-09-29"
    import json

    body = json.loads(complete.content)
    assert body["payment_data"]["token"] == "spt_test_contract"
    assert body["payment_data"]["provider"] == "stripe"
    assert all("card" not in k for k in body["payment_data"])  # no card data, ever


def test_acp_picks_cheapest_shipping():
    c = _acp()
    item = c.adapter.resolve(c.adapter.search("desk lamp")[0], 1, SHIP_TO)
    assert item.shipping_minor == 499


def test_acp_bad_credentials_are_a_definitive_error():
    m = ReferenceMerchant(api_key="right")
    a = AcpAdapter(
        "acp_merchant",
        "https://m.test",
        "https://m.test/feed.json",
        "wrong",
        "p",
        client=httpx.Client(transport=m.mock_transport()),
    )
    with pytest.raises(VendorError, match="401"):
        a.resolve(a.search("desk lamp")[0], 1, SHIP_TO)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("24.99 USD", (2499, "usd")),
        ("5 usd", (500, "usd")),
        ({"amount": 100, "currency": "USD"}, (100, "usd")),
    ],
)
def test_feed_price_parsing(raw, expected):
    assert parse_feed_price(raw) == expected


def test_feed_price_rejects_garbage():
    with pytest.raises(VendorError):
        parse_feed_price("free!")
