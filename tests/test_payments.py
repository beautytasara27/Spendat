"""Payment provider: scoping, mode safety and revocation. Offline via MockTransport.

``test_real_stripe_sandbox_*`` hits Stripe's real TEST-mode API and runs only when
``STRIPE_TEST_SECRET_KEY`` (sk_test_/rk_test_) and ``STRIPE_TEST_PAYMENT_METHOD_ID`` and
``STRIPE_TEST_SELLER_PROFILE`` are exported. Live keys are refused outright.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs

import httpx
import pytest

from spenddat.models import RuntimeMode
from spenddat.payments import FakePaymentProvider, PaymentError, StripeSharedPaymentTokens

NOW = datetime(2026, 9, 29, 12, tzinfo=UTC)


def _stripe(
    livemode: bool = False,
    mode: RuntimeMode = RuntimeMode.SANDBOX,
    key: str = "sk_test_abc123",
    cap_override: int | None = None,
):
    calls: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        if req.url.path.endswith("/revoke"):
            return httpx.Response(200, json={"id": "spt_1", "status": "revoked"})
        form = parse_qs(req.content.decode())
        return httpx.Response(
            200,
            json={
                "id": "spt_1",
                "object": "shared_payment.issued_token",
                "livemode": livemode,
                "status": "active",
                "usage_limits": {
                    "currency": form["usage_limits[currency]"][0],
                    "max_amount": cap_override or int(form["usage_limits[max_amount]"][0]),
                    "expires_at": int(form["usage_limits[expires_at]"][0]),
                },
            },
        )

    p = StripeSharedPaymentTokens(
        mode, key, "pm_123", client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    return p, calls


def _issue(p, amount=2498):
    return p.issue(
        amount_minor=amount,
        currency="usd",
        seller="profile_test_s",
        ttl=timedelta(minutes=15),
        idempotency_key="txn_1:credential",
        now=NOW,
        metadata={"txn_id": "txn_1"},
    )


def test_issues_scoped_single_seller_capped_token():
    p, calls = _stripe()
    cred = _issue(p)
    assert cred.token == "spt_1" and cred.max_amount_minor == 2498 and not cred.livemode
    req = calls[0]
    assert req.url.path == "/v1/shared_payment/issued_tokens"
    form = parse_qs(req.content.decode())
    assert form["payment_method"] == ["pm_123"]
    assert form["usage_limits[max_amount]"] == ["2498"]
    assert form["seller_details[network_business_profile]"] == ["profile_test_s"]
    assert int(form["usage_limits[expires_at]"][0]) == int(
        (NOW + timedelta(minutes=15)).timestamp()
    )
    assert req.headers["Idempotency-Key"] == "txn_1:credential"
    assert req.headers["Stripe-Version"].endswith(".preview")


def test_revocation_endpoint():
    p, calls = _stripe()
    p.revoke("spt_1")
    assert calls[-1].url.path == "/v1/shared_payment/issued_tokens/spt_1/revoke"


def test_livemode_mismatch_is_revoked_and_refused():
    p, calls = _stripe(livemode=True)
    with pytest.raises(PaymentError, match="livemode"):
        _issue(p)
    assert calls[-1].url.path.endswith("/revoke")


def test_overscoped_token_is_revoked_and_refused():
    p, calls = _stripe(cap_override=999999)
    with pytest.raises(PaymentError, match="exceeds"):
        _issue(p)
    assert calls[-1].url.path.endswith("/revoke")


@pytest.mark.parametrize(
    "mode,key",
    [
        (RuntimeMode.SANDBOX, "sk_live_abc"),
        (RuntimeMode.LIVE, "sk_test_abc"),
        (RuntimeMode.LIVE, "garbage"),
    ],
)
def test_key_must_match_mode(mode, key):
    with pytest.raises(PaymentError, match="not a"):
        StripeSharedPaymentTokens(mode, key, "pm_1")


def test_payment_method_must_be_a_reference_not_card_data():
    with pytest.raises(PaymentError, match="pm_"):
        StripeSharedPaymentTokens(RuntimeMode.SANDBOX, "sk_test_x", "4242424242424242")


def test_stripe_error_surfaces_cleanly():
    def handler(req):
        return httpx.Response(402, json={"error": {"type": "card_error", "message": "declined"}})

    p = StripeSharedPaymentTokens(
        RuntimeMode.SANDBOX,
        "sk_test_x",
        "pm_1",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(PaymentError, match="card_error: declined"):
        _issue(p)


def test_fake_provider_refuses_live_mode():
    with pytest.raises(PaymentError):
        FakePaymentProvider(RuntimeMode.LIVE)


def test_fake_provider_is_idempotent():
    f = FakePaymentProvider(RuntimeMode.SANDBOX)
    assert _issue(f).token == _issue(f).token


# ----------------------------------------------------------------------------- real sandbox

_KEY = os.environ.get("STRIPE_TEST_SECRET_KEY", "")
_PM = os.environ.get("STRIPE_TEST_PAYMENT_METHOD_ID", "")
_SELLER = os.environ.get("STRIPE_TEST_SELLER_PROFILE", "")


@pytest.mark.stripe_sandbox
@pytest.mark.skipif(not (_KEY and _PM and _SELLER), reason="Stripe test credentials not set")
def test_real_stripe_sandbox_issue_and_revoke():
    assert _KEY.startswith(("sk_test_", "rk_test_")), "only test keys may be used by the suite"
    p = StripeSharedPaymentTokens(RuntimeMode.SANDBOX, _KEY, _PM)
    cred = p.issue(
        amount_minor=100,
        currency="usd",
        seller=_SELLER,
        ttl=timedelta(minutes=5),
        idempotency_key=f"ci-{os.getpid()}-{datetime.now(UTC).timestamp()}",
        now=datetime.now(UTC),
        metadata={"source": "ci"},
    )
    assert cred.token.startswith("spt_") and cred.livemode is False
    p.revoke(cred.token)
