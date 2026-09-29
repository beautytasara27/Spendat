"""Sandbox demo wiring for the UI: everything in-process, no credentials needed.

* ACP adapter -> bundled reference merchant (in-process transport)
* stub vendor  -> second vendor (not pre-approved, so it escalates)
* payments     -> FakePaymentProvider (sandbox-only)
* intent       -> Anthropic if SPENDDAT_SANDBOX_ANTHROPIC_API_KEY is set, else the
                  offline keyword parser (sandbox-only)

The demo refuses to run in live mode. It optionally seeds a short, realistic history so
the dashboard has something to show on first launch.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from spenddat.approval import ApprovalService
from spenddat.audit import AuditStore
from spenddat.guards import HardLimits
from spenddat.intent import AnthropicClient, IntentParser, LlmClient
from spenddat.models import Address, ApprovalAction, Modification, RuntimeMode
from spenddat.orchestrator import Orchestrator
from spenddat.payments import FakePaymentProvider
from spenddat.policy import PolicyEngine, load_policy
from spenddat.sandbox.reference_merchant import ReferenceMerchant
from spenddat.ui.offline_llm import OfflineIntentLlm
from spenddat.vendors.acp import AcpAdapter
from spenddat.vendors.base import AdapterRegistry
from spenddat.vendors.stub import StubAdapter, StubProduct

DEMO_SHIP_TO = Address(
    name="Demo Buyer",
    line_one="123 Example St",
    city="New York",
    state="NY",
    postal_code="10001",
    country="US",
)


class _Clock:
    """Real time, except while seeding history into the past."""

    def __init__(self) -> None:
        self.offset = timedelta(0)

    def __call__(self) -> datetime:
        return datetime.now(UTC) + self.offset


def build_demo(db_path: str, policy_file: str, *, seed: bool = True) -> tuple[Orchestrator, str]:
    if os.environ.get("SPENDDAT_MODE", "sandbox").lower() == "live":
        raise RuntimeError("the UI demo runs in sandbox mode only; unset SPENDDAT_MODE=live")
    mode = RuntimeMode.SANDBOX
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    store = AuditStore(db_path)
    policy = load_policy(policy_file)

    merchant = ReferenceMerchant(api_key="demo_key")
    base = "https://reference-merchant.test"
    registry = AdapterRegistry()
    registry.register(
        AcpAdapter(
            vendor_id="acp_merchant",
            base_url=base,
            feed_url=f"{base}/feed.json",
            api_key="demo_key",
            seller_profile="profile_test_demo",
            client=httpx.Client(transport=merchant.mock_transport(base)),
        )
    )
    registry.register(
        StubAdapter(
            "stub_vendor",
            [
                StubProduct("STUB-PEN-10", "Gel Pen 10 Pack", 899),
                StubProduct("STUB-DESK-MAT", "Felt Desk Mat", 2299),
                StubProduct("STUB-MONITOR-27", "27 inch 4K Monitor", 32999),
            ],
        )
    )

    key = os.environ.get("SPENDDAT_SANDBOX_ANTHROPIC_API_KEY")
    llm: LlmClient
    if key:
        llm = AnthropicClient(key, os.environ.get("SPENDDAT_LLM_MODEL", "claude-sonnet-5-5"))
        label = "Claude (Anthropic API)"
    else:
        llm = OfflineIntentLlm(mode, registry.vendor_ids() + sorted(policy.vendors.deny))
        label = "offline demo parser"

    parser = IntentParser(
        llm,
        supported_vendors=set(registry.vendor_ids()) | policy.vendors.deny,
        autonomous_vendors=policy.vendors.allow,
        supported_categories=policy.categories.allow | policy.categories.deny,
    )
    clock = _Clock()
    orch = Orchestrator(
        mode=mode,
        store=store,
        parser=parser,
        engine=PolicyEngine(policy),
        approvals=ApprovalService(store, [], timedelta(minutes=policy.approval.timeout_minutes)),
        registry=registry,
        payments=FakePaymentProvider(mode),
        hard_limits=HardLimits(20000, 30000, 75000),
        ship_to=DEMO_SHIP_TO,
        clock=clock,
    )
    if seed and not store.list_txns(mode, 1):
        _seed(orch, clock, offline=not key)
    return orch, label


def _seed(orch: Orchestrator, clock: _Clock, *, offline: bool) -> None:
    """Replay a week of plausible activity through the real pipeline."""
    if not offline:
        return  # don't spend real LLM calls on seeding
    script: list[tuple[float, str, str | None]] = [
        (9.0, "buy a standard office stapler", None),
        (8.2, "buy a usb-c charging cable", None),
        (7.1, "buy an a5 dot grid notebook pack", None),
        (6.3, "buy a $50 gift card", None),
        (5.4, "buy noise cancelling wireless headphones", "approve"),
        (4.2, "buy a clear phone case for iphone 15 under $25", None),
        (3.5, "buy a felt desk mat from stub vendor", "approve"),
        (2.6, "buy a stapler from sketchy mart", None),
        (1.8, "buy 3 led desk lamps", "approve_low"),
        (1.1, "buy a usb-c charging cable", None),
        (0.4, "buy a 27 inch 4k monitor from stub vendor", "deny"),
        (0.0, "buy a wireless mouse", None),  # left pending for the dashboard
    ]
    for days_ago, text, action in script:
        clock.offset = -timedelta(days=days_ago)
        res = orch.submit(text)
        if res.approval_id and action:
            clock.offset += timedelta(minutes=7)
            if action == "approve":
                orch.decide_approval(res.approval_id, ApprovalAction.APPROVE, "demo reviewer")
            elif action == "approve_low":
                orch.decide_approval(
                    res.approval_id,
                    ApprovalAction.APPROVE_WITH_MODIFICATION,
                    "demo reviewer",
                    Modification(quantity=1),
                )
            elif action == "deny":
                orch.decide_approval(res.approval_id, ApprovalAction.DENY, "demo reviewer")
    clock.offset = timedelta(0)
