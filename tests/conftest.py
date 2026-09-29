"""Shared fixtures. Everything runs offline in SANDBOX mode: no credentials, no network.

The ACP adapter talks to the reference merchant through ``httpx.MockTransport`` and
payments go to ``FakePaymentProvider`` (which refuses to be constructed in live mode).
The optional ``stripe_sandbox`` tests hit real Stripe *test mode* and are skipped unless
test credentials are provided.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from spenddat.approval import ApprovalService, RecordingNotifier
from spenddat.audit import AuditStore
from spenddat.guards import HardLimits
from spenddat.intent import IntentParser, ScriptedLlm
from spenddat.models import Address, RuntimeMode
from spenddat.orchestrator import Orchestrator
from spenddat.payments import FakePaymentProvider
from spenddat.policy import PolicyEngine, load_policy
from spenddat.sandbox.reference_merchant import ReferenceMerchant
from spenddat.vendors.acp import AcpAdapter
from spenddat.vendors.base import AdapterRegistry
from spenddat.vendors.stub import StubAdapter, StubProduct

ROOT = Path(__file__).resolve().parents[1]
POLICY_FILE = ROOT / "policies" / "default.yaml"
VENDOR = "acp_merchant"
BASE = "https://merchant.test"

SHIP_TO = Address(
    name="Test Buyer",
    line_one="1 Test St",
    city="New York",
    state="NY",
    postal_code="10001",
    country="US",
)


def intent(**kw: Any) -> dict[str, Any]:
    """A tool-call payload as the LLM would return it."""
    base = {
        "status": "ok",
        "vendor": VENDOR,
        "product_description": "clear phone case iphone 15",
        "category": "electronics_accessories",
        "quantity": 1,
        "max_price_usd": None,
        "constraints": [],
    }
    base.update(kw)
    return base


class Clock:
    def __init__(self) -> None:
        self.t = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.t

    def advance(self, **kw: float) -> None:
        self.t += timedelta(**kw)


class Harness:
    def __init__(
        self,
        tmp: Path,
        *,
        ship_to: Address | None = SHIP_TO,
        limits: HardLimits | None = None,
        mode: RuntimeMode = RuntimeMode.SANDBOX,
        payments: Any = None,
    ) -> None:
        self.mode = mode
        self.clock = Clock()
        self.store = AuditStore(tmp / "audit.db")
        self.policy = load_policy(POLICY_FILE)
        self.llm = ScriptedLlm()
        self.notifier = RecordingNotifier()
        self.payments = payments or FakePaymentProvider(RuntimeMode.SANDBOX)
        self.merchant = ReferenceMerchant(api_key="k_test")
        self.acp = AcpAdapter(
            vendor_id=VENDOR,
            base_url=BASE,
            feed_url=f"{BASE}/feed.json",
            api_key="k_test",
            seller_profile="profile_test_seller",
            client=httpx.Client(transport=self.merchant.mock_transport(BASE)),
        )
        self.stub = StubAdapter(
            "stub_vendor",
            [
                StubProduct("STUB-PEN-10", "Gel Pen 10 Pack", 899),
                StubProduct("STUB-DESK-MAT", "Felt Desk Mat", 2299),
            ],
            shipping_minor=0,
        )
        self.registry = AdapterRegistry()
        self.registry.register(self.acp)
        self.registry.register(self.stub)
        self.approvals = ApprovalService(
            self.store, [self.notifier], timedelta(minutes=self.policy.approval.timeout_minutes)
        )
        self.orch = Orchestrator(
            mode=mode,
            store=self.store,
            parser=IntentParser(
                self.llm,
                supported_vendors=set(self.registry.vendor_ids()) | self.policy.vendors.deny,
                autonomous_vendors=self.policy.vendors.allow,
                supported_categories=self.policy.categories.allow | self.policy.categories.deny,
            ),
            engine=PolicyEngine(self.policy),
            approvals=self.approvals,
            registry=self.registry,
            payments=self.payments,
            hard_limits=limits or HardLimits(20000, 30000, 75000),
            ship_to=ship_to,
            clock=self.clock,
        )

    def buy(self, text: str = "buy a clear phone case for my iphone 15", **kw: Any):
        self.llm.push(intent(**kw))
        return self.orch.submit(text)

    def event_types(self, txn_id: str) -> list[str]:
        return [e.event_type for e in self.store.events(txn_id)]


@pytest.fixture
def h(tmp_path: Path) -> Harness:
    return Harness(tmp_path)
