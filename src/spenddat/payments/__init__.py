"""Scoped payment credentials.

The system never handles card numbers. For each approved transaction it asks the payment
provider to mint a *scoped* credential that references the buyer's saved payment method
by id only, and hands that credential to the vendor adapter.

``StripeSharedPaymentTokens`` uses Stripe Shared Payment Tokens (SPTs), the credential
type the Agentic Commerce Protocol is built on:

* capped at the approved amount (``usage_limits[max_amount]``),
* restricted to one seller (``seller_details[network_business_profile]``),
* time-limited (``usage_limits[expires_at]``) and consumed on use,
* revoked explicitly when the transaction finishes, success or failure.

Sandbox and live differ only in the API key (``sk_test_`` vs ``sk_live_``); the provider
additionally verifies that Stripe's ``livemode`` flag on every token matches the
configured runtime mode and refuses to continue on mismatch.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

import httpx

from spenddat.models import RuntimeMode
from spenddat.observability import get_logger, log

_log = get_logger("payments")


class PaymentError(RuntimeError):
    pass


@dataclass(frozen=True)
class ScopedCredential:
    token: str
    provider: str
    max_amount_minor: int
    currency: str
    seller: str
    expires_at: datetime
    livemode: bool


class PaymentProvider(Protocol):
    name: str

    def issue(
        self,
        *,
        amount_minor: int,
        currency: str,
        seller: str,
        ttl: timedelta,
        idempotency_key: str,
        now: datetime,
        metadata: dict[str, str],
    ) -> ScopedCredential: ...

    def revoke(self, token: str) -> None: ...


class StripeSharedPaymentTokens:
    name = "stripe"

    def __init__(
        self,
        mode: RuntimeMode,
        secret_key: str,
        payment_method_id: str,
        api_version: str = "2026-04-22.preview",
        base_url: str = "https://api.stripe.com",
        client: httpx.Client | None = None,
    ) -> None:
        expected = "live" if mode is RuntimeMode.LIVE else "test"
        if not secret_key.startswith((f"sk_{expected}_", f"rk_{expected}_")):
            raise PaymentError(f"Stripe key is not a {expected}-mode key for {mode.value} mode")
        if not payment_method_id.startswith("pm_"):
            raise PaymentError("STRIPE_PAYMENT_METHOD_ID must be a PaymentMethod id (pm_...)")
        self._mode = mode
        self._key = secret_key
        self._pm = payment_method_id
        self._version = api_version
        self._base = base_url.rstrip("/")
        self._http = client or httpx.Client(timeout=httpx.Timeout(20.0, connect=5.0))

    def _post(self, path: str, data: dict[str, str], idem: str | None = None) -> dict:
        headers = {"Stripe-Version": self._version}
        if idem:
            headers["Idempotency-Key"] = idem
        try:
            r = self._http.post(
                f"{self._base}{path}", data=data, headers=headers, auth=(self._key, "")
            )
        except httpx.TransportError as e:
            raise PaymentError(f"stripe unreachable: {type(e).__name__}") from e
        if r.status_code >= 400:
            try:
                err = r.json().get("error", {})
                msg = f"{err.get('type')}: {err.get('message')}"
            except ValueError:
                msg = r.text[:200]
            raise PaymentError(f"stripe {path} HTTP {r.status_code}: {msg}")
        return r.json()

    def issue(
        self,
        *,
        amount_minor: int,
        currency: str,
        seller: str,
        ttl: timedelta,
        idempotency_key: str,
        now: datetime,
        metadata: dict[str, str],
    ) -> ScopedCredential:
        if amount_minor <= 0:
            raise PaymentError("amount must be positive")
        expires = now + ttl
        data = {
            "payment_method": self._pm,
            "seller_details[network_business_profile]": seller,
            "usage_limits[currency]": currency,
            "usage_limits[max_amount]": str(amount_minor),
            "usage_limits[expires_at]": str(int(expires.timestamp())),
        }
        for k, v in metadata.items():
            data[f"shared_metadata[{k}]"] = v
        body = self._post("/v1/shared_payment/issued_tokens", data, idem=idempotency_key)
        cred = ScopedCredential(
            token=body["id"],
            provider=self.name,
            max_amount_minor=int(body["usage_limits"]["max_amount"]),
            currency=body["usage_limits"]["currency"],
            seller=seller,
            expires_at=expires,
            livemode=bool(body.get("livemode")),
        )
        if cred.livemode != (self._mode is RuntimeMode.LIVE):
            self.revoke(cred.token)
            raise PaymentError("Stripe livemode flag does not match configured runtime mode")
        if cred.max_amount_minor > amount_minor:
            self.revoke(cred.token)
            raise PaymentError("issued token cap exceeds the approved amount")
        if body.get("status") not in (None, "active"):
            self.revoke(cred.token)
            if body.get("status") == "requires_action":
                raise PaymentError(
                    "your card issuer wants extra verification (3-D Secure) before this card "
                    "can be used by an agent; nothing was charged"
                )
            raise PaymentError(f"payment token not usable (status {body.get('status')})")
        log(
            _log,
            20,
            "credential_issued",
            max_amount_minor=cred.max_amount_minor,
            livemode=cred.livemode,
        )
        return cred

    def revoke(self, token: str) -> None:
        body = self._post(f"/v1/shared_payment/issued_tokens/{token}/revoke", {})
        log(_log, 20, "credential_revoked", status=body.get("status"))


class FakePaymentProvider:
    """Offline stand-in for unit tests and local dev. Refuses to exist in live mode."""

    name = "stripe"

    def __init__(self, mode: RuntimeMode) -> None:
        if mode is not RuntimeMode.SANDBOX:
            raise PaymentError("FakePaymentProvider is only permitted in sandbox mode")
        self.issued: dict[str, ScopedCredential] = {}
        self.revoked: set[str] = set()
        self._idem: dict[str, str] = {}
        self.fail_issue = False

    def issue(
        self,
        *,
        amount_minor: int,
        currency: str,
        seller: str,
        ttl: timedelta,
        idempotency_key: str,
        now: datetime,
        metadata: dict[str, str],
    ) -> ScopedCredential:
        if self.fail_issue:
            raise PaymentError("injected issue failure")
        if idempotency_key in self._idem:
            return self.issued[self._idem[idempotency_key]]
        cred = ScopedCredential(
            token=f"spt_fake{uuid.uuid4().hex[:16]}",
            provider=self.name,
            max_amount_minor=amount_minor,
            currency=currency,
            seller=seller,
            expires_at=now + ttl,
            livemode=False,
        )
        self.issued[cred.token] = cred
        self._idem[idempotency_key] = cred.token
        return cred

    def revoke(self, token: str) -> None:
        self.revoked.add(token)

    def active_tokens(self) -> set[str]:
        return set(self.issued) - self.revoked
