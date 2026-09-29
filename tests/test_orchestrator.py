"""End-to-end pipeline tests (sandbox): intent -> policy -> approval -> execution -> audit."""

from __future__ import annotations

import pytest

from spenddat.approval import ApprovalError
from spenddat.guards import HardLimits
from spenddat.models import ApprovalAction, Modification, OrderStatus, RuntimeMode, TxnState
from spenddat.orchestrator import _SEAL, ExecutionPermit, GateViolation
from spenddat.payments import PaymentError
from spenddat.vendors.base import VendorError
from tests.conftest import Harness, intent

# ============================================================================ ALLOW path


def test_allow_executes_autonomously_with_no_human(h: Harness):
    res = h.buy()
    assert res.state is TxnState.COMPLETED, res.message
    assert res.receipt is not None and res.receipt.amount_minor == 2498  # 19.99 + 4.99 ship
    assert h.notifier.requests == []  # no approval ever requested
    assert h.store.pending_approvals() == []
    assert h.store.approval_for_txn(res.txn_id) is None
    assert h.merchant.orders_created == 1
    ev = h.event_types(res.txn_id)
    assert "approval_requested" not in ev
    assert ev.index("execution_started") < ev.index("payment_credential_issued")
    assert ev.index("payment_credential_issued") < ev.index("execution_succeeded")
    assert ev[-1] == "payment_credential_revoked"
    assert h.payments.active_tokens() == set()  # credential closed after use


def test_scoped_credential_is_capped_to_the_transaction(h: Harness):
    h.buy()
    (cred,) = h.payments.issued.values()
    assert cred.max_amount_minor == 2498
    assert cred.seller == "profile_test_seller"
    assert not cred.livemode


def test_every_event_records_the_runtime_mode(h: Harness):
    res = h.buy()
    assert {e.mode for e in h.store.events(res.txn_id)} == {"sandbox"}
    assert h.store.get_txn(res.txn_id)["mode"] == "sandbox"


def test_audit_trail_is_complete_and_chain_verifies(h: Harness):
    res = h.buy()
    ev = h.event_types(res.txn_id)
    for required in (
        "request_received",
        "intent_parsed",
        "candidates_found",
        "item_resolved",
        "policy_decided",
        "execution_started",
        "execution_succeeded",
    ):
        assert required in ev
    assert h.store.verify_chain() > 0


# ============================================================================ ESCALATE path


def test_escalation_waits_for_human_then_executes(h: Harness):
    res = h.buy(
        "buy noise cancelling wireless headphones",
        product_description="noise cancelling wireless headphones",
    )
    assert res.state is TxnState.PENDING_APPROVAL
    assert h.merchant.orders_created == 0 and h.payments.issued == {}
    (req,) = h.notifier.requests
    item = req.presented["item"]
    assert item["sku"] == "SKU-HEADPH-NC700" and item["total_minor"] == 9498
    assert req.presented["ship_to"].startswith("Test Buyer, 1 Test St")
    assert set(req.presented["reason_codes"]) == {"price_cap_exceeded", "category_cap_exceeded"}

    done = h.orch.decide_approval(res.approval_id, ApprovalAction.APPROVE, "alice")
    assert done.state is TxnState.COMPLETED
    assert h.merchant.orders_created == 1
    ev = h.store.events(res.txn_id)
    decided = next(e for e in ev if e.event_type == "approval_decided")
    assert decided.payload["decided_by"] == "alice"
    assert decided.payload["presented"]["item"]["sku"] == "SKU-HEADPH-NC700"
    started = next(e for e in ev if e.event_type == "execution_started")
    assert started.payload["basis"] == "human_approval"


def test_human_denial_blocks_purchase(h: Harness):
    res = h.buy("buy headphones", product_description="noise cancelling wireless headphones")
    out = h.orch.decide_approval(res.approval_id, ApprovalAction.DENY, "alice")
    assert out.state is TxnState.APPROVAL_DENIED
    assert h.merchant.orders_created == 0 and h.payments.issued == {}


def test_approve_with_lower_ceiling_below_price_does_not_buy(h: Harness):
    res = h.buy("buy headphones", product_description="noise cancelling wireless headphones")
    out = h.orch.decide_approval(
        res.approval_id,
        ApprovalAction.APPROVE_WITH_MODIFICATION,
        "alice",
        Modification(max_total_minor=6000),
    )
    assert out.state is TxnState.FAILED and "exceeds approved ceiling" in out.message
    assert h.merchant.orders_created == 0 and h.payments.issued == {}


def test_approve_with_modification_within_ceiling_caps_token(h: Harness):
    res = h.buy(
        "buy 3 led desk lamps",
        product_description="led desk lamp",
        quantity=3,
        category="office_supplies",
    )
    assert res.state is TxnState.PENDING_APPROVAL
    out = h.orch.decide_approval(
        res.approval_id,
        ApprovalAction.APPROVE_WITH_MODIFICATION,
        "alice",
        Modification(quantity=1, max_total_minor=5000),
    )
    assert out.state is TxnState.COMPLETED
    (cred,) = h.payments.issued.values()
    assert cred.max_amount_minor == 3999 + 499


def test_modification_cannot_raise_limits(h: Harness):
    res = h.buy("buy headphones", product_description="noise cancelling wireless headphones")
    with pytest.raises(ApprovalError, match="only lower"):
        h.orch.decide_approval(
            res.approval_id,
            ApprovalAction.APPROVE_WITH_MODIFICATION,
            "alice",
            Modification(max_total_minor=100000),
        )


def test_approval_cannot_be_decided_twice(h: Harness):
    res = h.buy("buy headphones", product_description="noise cancelling wireless headphones")
    h.orch.decide_approval(res.approval_id, ApprovalAction.APPROVE, "alice")
    with pytest.raises(ApprovalError, match="already"):
        h.orch.decide_approval(res.approval_id, ApprovalAction.APPROVE, "bob")
    assert h.merchant.orders_created == 1


def test_escalation_times_out_to_denial(h: Harness):
    res = h.buy("buy headphones", product_description="noise cancelling wireless headphones")
    h.clock.advance(minutes=61)
    expired = h.orch.sweep_expired()
    assert [r.txn_id for r in expired] == [res.txn_id]
    assert h.store.get_txn(res.txn_id)["state"] == TxnState.APPROVAL_EXPIRED
    assert any("timed out" in m for _, m in h.notifier.updates)
    with pytest.raises(ApprovalError):
        h.orch.decide_approval(res.approval_id, ApprovalAction.APPROVE, "alice")
    assert h.merchant.orders_created == 0


def test_late_approval_after_expiry_is_rejected_even_without_sweep(h: Harness):
    res = h.buy("buy headphones", product_description="noise cancelling wireless headphones")
    h.clock.advance(minutes=90)
    with pytest.raises(ApprovalError, match="expired"):
        h.orch.decide_approval(res.approval_id, ApprovalAction.APPROVE, "alice")
    assert h.store.get_txn(res.txn_id)["state"] == TxnState.APPROVAL_EXPIRED
    assert h.merchant.orders_created == 0


def test_ambiguous_match_escalates_with_resolved_best_candidate(h: Harness):
    res = h.buy("buy a wireless mouse", product_description="wireless mouse")
    assert res.state is TxnState.PENDING_APPROVAL
    (req,) = h.notifier.requests
    assert "ambiguous_product_match" in req.presented["reason_codes"]
    assert req.presented["item"]["sku"] == "SKU-MOUSE-WL-A"  # concrete, not vague
    assert req.presented["alternatives"]


def test_unresolvable_escalation_goes_back_to_requester(h: Harness):
    res = h.buy(
        "buy a garden gnome", product_description="garden gnome statue", category="household"
    )
    assert res.state is TxnState.NEEDS_CLARIFICATION
    assert "no_product_match" in res.decision.reason_codes
    assert h.notifier.requests == []


def test_missing_shipping_address_never_buys(tmp_path):
    h = Harness(tmp_path, ship_to=None)
    res = h.buy()
    assert res.state is TxnState.NEEDS_CLARIFICATION
    assert "missing_shipping_address" in res.decision.reason_codes
    assert h.merchant.orders_created == 0


def test_user_price_limit_escalates(h: Harness):
    res = h.buy("buy a clear phone case for iphone 15 under $15", max_price_usd=15)
    assert res.state is TxnState.PENDING_APPROVAL
    assert res.decision.reason_codes == ("user_max_price_exceeded",)


def test_velocity_anomaly_escalates_sixth_purchase(h: Harness):
    for _ in range(5):
        assert (
            h.buy(
                "buy a standard stapler",
                product_description="standard office stapler",
                category="office_supplies",
            ).state
            is TxnState.COMPLETED
        )
    res = h.buy(
        "buy a standard stapler",
        product_description="standard office stapler",
        category="office_supplies",
    )
    assert res.state is TxnState.PENDING_APPROVAL
    assert "velocity_exceeded" in res.decision.reason_codes


def test_price_anomaly_uses_purchase_history(h: Harness):
    for _ in range(3):
        h.buy(
            "buy a standard stapler",
            product_description="standard office stapler",
            category="office_supplies",
        )
        h.clock.advance(hours=2)
    h.merchant.catalog["SKU-STAPLER-STD"]["price_minor"] = 1899  # doubled
    res = h.buy(
        "buy a standard stapler",
        product_description="standard office stapler",
        category="office_supplies",
    )
    assert res.decision.reason_codes == ("price_anomaly",)


def test_cumulative_daily_cap_escalates(h: Harness):
    for _ in range(3):  # 3 x $44.98 lamps = $134.94
        assert (
            h.buy(
                "buy led desk lamp", product_description="led desk lamp", category="office_supplies"
            ).state
            is TxnState.COMPLETED
        )
        h.clock.advance(minutes=30)
    res = h.buy(
        "buy led desk lamp", product_description="led desk lamp", category="office_supplies"
    )
    assert "daily_cap_exceeded" in res.decision.reason_codes
    h.clock.advance(hours=25)
    assert (
        h.buy(
            "buy led desk lamp", product_description="led desk lamp", category="office_supplies"
        ).state
        is TxnState.COMPLETED
    )


# ============================================================================ DENY path


def test_blocklisted_vendor_is_denied_not_escalated(h: Harness):
    res = h.buy("buy a phone case from sketchy mart", vendor="sketchy_mart")
    assert res.state is TxnState.DENIED
    assert res.decision.reason_codes == ("vendor_blocklisted",)
    assert h.notifier.requests == []
    assert any("Denied" in m for _, m in h.notifier.updates)


def test_denylisted_category_is_denied(h: Harness):
    res = h.buy(
        "buy a $25 gift card",
        product_description="gift card",
        category="gift_cards",
        max_price_usd=25,
    )
    assert res.state is TxnState.DENIED
    assert "category_denied" in res.decision.reason_codes
    assert h.merchant.orders_created == 0 and h.notifier.requests == []


def test_unknown_vendor_without_integration_is_rejected(h: Harness):
    res = h.buy("buy from walmart", vendor="walmart")
    assert res.state is TxnState.REJECTED and "unsupported_vendor" in res.message


# ============================================================================ circuit breakers


def test_hard_ceiling_cannot_be_approved_past(tmp_path):
    h = Harness(
        tmp_path,
        limits=HardLimits(
            max_transaction_minor=9000, max_daily_minor=20000, max_weekly_minor=50000
        ),
    )
    res = h.buy("buy headphones", product_description="noise cancelling wireless headphones")
    assert res.state is TxnState.PENDING_APPROVAL
    out = h.orch.decide_approval(res.approval_id, ApprovalAction.APPROVE, "ceo")
    assert out.state is TxnState.BLOCKED_BY_CIRCUIT_BREAKER
    assert "hard_transaction_ceiling" in out.message
    assert h.merchant.orders_created == 0 and h.payments.issued == {}


def test_hard_daily_ceiling_blocks_even_allowed_purchases(tmp_path):
    h = Harness(tmp_path, limits=HardLimits(10000, 4999, 50000))
    assert h.buy().state is TxnState.COMPLETED  # $24.98
    assert h.buy().state is TxnState.COMPLETED  # $49.96 <= $49.99
    third = h.buy()  # $74.94 > $49.99 -> blocked
    assert third.state is TxnState.BLOCKED_BY_CIRCUIT_BREAKER
    assert h.merchant.orders_created == 2


# ============================================================================ the gate


def test_permit_cannot_be_forged():
    with pytest.raises(PermissionError):
        ExecutionPermit("txn_x", "policy_allow", 100, object())


def test_gate_refuses_unapproved_escalation(h: Harness):
    res = h.buy("buy headphones", product_description="noise cancelling wireless headphones")
    with pytest.raises(GateViolation, match="without recorded human approval"):
        h.orch._authorize(res.txn_id)


def test_gate_refuses_denied_transaction(h: Harness):
    res = h.buy(vendor="sketchy_mart")
    with pytest.raises(GateViolation):
        h.orch._authorize(res.txn_id)


def test_execute_refuses_completed_transaction_replay(h: Harness):
    from spenddat.models import ResolvedItem

    res = h.buy()
    snap = h.orch._snapshot(res.txn_id)
    permit = ExecutionPermit(res.txn_id, "policy_allow", 10**6, _SEAL)
    with pytest.raises(GateViolation, match="not in an executable state"):
        h.orch._execute(permit, h.acp, ResolvedItem.model_validate(snap["item"]), None)
    assert h.merchant.orders_created == 1


# ============================================================================ failure paths


def test_payment_credential_failure_means_no_purchase(h: Harness):
    h.payments.fail_issue = True
    res = h.buy()
    assert res.state is TxnState.FAILED and "payment_credential" in res.message
    assert h.merchant.orders_created == 0
    ev = h.event_types(res.txn_id)
    assert "execution_started" in ev and ev[-1] == "execution_failed"
    assert h.store.spend_since(RuntimeMode.SANDBOX, h.clock.t.replace(year=2000)) == 0


def test_lost_response_is_reconciled_not_repaid(h: Harness):
    h.merchant.drop_next_complete_response = True  # order created, response lost
    res = h.buy()
    assert res.state is TxnState.COMPLETED
    assert h.merchant.orders_created == 1  # exactly one order
    assert len(h.payments.issued) == 1  # exactly one credential
    ev = h.event_types(res.txn_id)
    assert "execution_outcome_unknown" in ev and "reconciliation_checked" in ev
    assert h.payments.active_tokens() == set()


def test_unknown_outcome_holds_spend_and_is_resolved_later(h: Harness):
    h.stub.fail_next_purchase_with = TimeoutError
    h.stub.complete_before_failing = True
    orig = h.stub.order_status
    h.stub.order_status = lambda ref: (OrderStatus.UNKNOWN, None)
    res = h.buy(
        "buy gel pen 10 pack",
        vendor="stub_vendor",
        product_description="gel pen pack",
        category="office_supplies",
    )
    assert res.state is TxnState.PENDING_APPROVAL  # stub vendor is not pre-approved
    out = h.orch.decide_approval(res.approval_id, ApprovalAction.APPROVE, "alice")
    assert out.state is TxnState.NEEDS_RECONCILIATION
    assert h.store.spend_since(RuntimeMode.SANDBOX, h.clock.t.replace(year=2000)) == 899
    h.stub.order_status = orig
    (rec,) = h.orch.reconcile()
    assert rec.state is TxnState.COMPLETED
    assert h.stub.orders_created == 1


def test_definitive_vendor_failure_releases_spend(h: Harness):
    h.stub.fail_next_purchase_with = VendorError
    res = h.buy(
        "buy gel pen",
        vendor="stub_vendor",
        product_description="gel pen pack",
        category="office_supplies",
    )
    out = h.orch.decide_approval(res.approval_id, ApprovalAction.APPROVE, "alice")
    assert out.state is TxnState.FAILED
    assert h.stub.orders_created == 0
    assert h.store.spend_since(RuntimeMode.SANDBOX, h.clock.t.replace(year=2000)) == 0
    assert h.payments.active_tokens() == set()


# ============================================================================ idempotency


def test_client_request_id_prevents_duplicate_purchase(h: Harness):
    h.llm.push(intent())
    first = h.orch.submit("buy a clear phone case", client_request_id="req-123")
    again = h.orch.submit("buy a clear phone case", client_request_id="req-123")
    assert first.state is TxnState.COMPLETED
    assert again.txn_id == first.txn_id and "duplicate" in again.message
    assert h.merchant.orders_created == 1


# ============================================================================ second vendor


def test_second_vendor_flows_through_unchanged_pipeline(h: Harness):
    """Stub vendor was added by registering an adapter only (no policy/approval/orch change).
    It is not on the allow-list, so the unchanged policy escalates it; after approval it buys.
    """
    res = h.buy(
        "buy felt desk mat",
        vendor="stub_vendor",
        product_description="felt desk mat",
        category="office_supplies",
    )
    assert res.decision.reason_codes == ("vendor_not_preapproved",)
    out = h.orch.decide_approval(res.approval_id, ApprovalAction.APPROVE, "alice")
    assert out.state is TxnState.COMPLETED and out.receipt.vendor == "stub_vendor"


# ============================================================================ modes


class _LabelOnlyLiveProvider:
    """In-test stand-in so the *orchestrator* can be run with mode=LIVE. Makes no calls."""

    name = "stripe"

    def __init__(self) -> None:
        from spenddat.payments import FakePaymentProvider

        self._inner = FakePaymentProvider(RuntimeMode.SANDBOX)

    def issue(self, **kw):
        return self._inner.issue(**kw)

    def revoke(self, token):
        self._inner.revoke(token)


@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"product_description": "noise cancelling wireless headphones"},
        {"vendor": "sketchy_mart"},
        {"category": "gift_cards", "product_description": "gift card"},
    ],
)
def test_mode_never_changes_the_decision(tmp_path, kwargs):
    sandbox = Harness(tmp_path / "s")
    live = Harness(tmp_path / "l", mode=RuntimeMode.LIVE, payments=_LabelOnlyLiveProvider())
    a, b = sandbox.buy(**kwargs), live.buy(**kwargs)
    assert a.decision.outcome == b.decision.outcome
    assert a.decision.reason_codes == b.decision.reason_codes
    assert {e.mode for e in live.store.events(b.txn_id)} == {"live"}


def test_payment_error_type_is_exported():
    assert issubclass(PaymentError, RuntimeError)
