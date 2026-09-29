# ADR-0003: Stripe Shared Payment Tokens for scoped credentials

**Status:** accepted · 2026-09-29

## Options
* **Stripe Issuing virtual cards** — scoped, but the card number must be revealed to pay a
  merchant's checkout, which puts a PAN inside our process. Rejected (PRD §2).
* **Network agentic tokens (Visa Intelligent Commerce / Mastercard Agent Pay)** — right idea,
  but access is partner-gated.
* **Stripe Shared Payment Tokens (SPT)** — chosen. The agent grants a seller scoped use of the
  buyer's saved PaymentMethod: `usage_limits.max_amount`, `usage_limits.expires_at`,
  `seller_details.network_business_profile`; revocable; the underlying credential is never
  exposed. SPT is the native payment primitive of the Agentic Commerce Protocol (ADR-0004).

## Implementation
`POST /v1/shared_payment/issued_tokens` per transaction (cap = resolved total, seller = the
merchant's Stripe profile, 15-minute expiry, idempotency key `txn_id:credential`), then
`POST /v1/shared_payment/issued_tokens/{id}/revoke` in a `finally`. The system only ever holds
a `pm_…` id and `spt_…` ids.

Mode safety: the key prefix must match the mode (`sk_test_/rk_test_` vs `sk_live_/rk_live_`)
and Stripe's `livemode` flag on each issued token must match too, else the token is revoked
and the transaction fails.

## Risks
SPTs are in **public preview** (US only) and require accepting Stripe's agentic-commerce
terms; the `Stripe-Version` header is configurable (default `2026-04-22.preview`). If the API
shape changes, only `payments/__init__.py` changes.
