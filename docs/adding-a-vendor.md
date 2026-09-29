# Adding a vendor

You will touch **only** the vendor layer, config, and the contract test. Policy, approval and
orchestration code do not change.

## 1. Another ACP merchant (no code)

Add an entry to `config/vendors.yaml`:

```yaml
  - id: second_store
    type: acp
    sandbox: {base_url: ..., feed_url: ..., seller_profile: profile_test_..., api_key_secret: second_store_api_key}
    live:    {base_url: https://..., feed_url: https://..., seller_profile: profile_..., api_key_secret: second_store_api_key}
```

Set the secret (`SPENDDAT_<MODE>_SECOND_STORE_API_KEY`), then add `second_store` to
`vendors.allow` in the policy when you want it to be purchasable autonomously (until then its
purchases escalate as `vendor_not_preapproved`).

## 2. A new integration type

1. **Check the vendor's terms** for automated/agentic purchasing and record the result in a
   new ADR. Do not build browser automation against a vendor that prohibits it.
2. **Implement** `spenddat.vendors.base.VendorAdapter` in `src/spenddat/vendors/<name>.py`.
   `vendors/stub.py` is a complete, minimal template.

   | Method | Must |
   |---|---|
   | `search(query, limit)` | be read-only; return in-stock `Candidate`s best-first with a 0–1 `score` |
   | `resolve(candidate, qty, ship_to)` | return an authoritative `ResolvedItem` (unit, shipping, tax, total, `vendor_ref`, `merchant_payment_id`); charge nothing |
   | `purchase(item, payment, ship_to, idempotency_key)` | pay with `payment.token` only; refuse if `payment.max_amount_minor < total`; be idempotent per key; raise `VendorError` when definitely not purchased and `VendorOutcomeUnknown` when unsure |
   | `order_status(vendor_ref)` | report `COMPLETED` (+ receipt), `NOT_COMPLETED`, `CANCELED` or `UNKNOWN` without side effects |
   | `cancel(vendor_ref)` | optional, best effort |

   Browser-automation adapters additionally: persist the authenticated session (e.g. a
   Playwright `storage_state` file, encrypted at rest, path from secrets) instead of logging in
   per transaction; after submitting payment, treat any missing confirmation as
   `VendorOutcomeUnknown` and implement `order_status` by reading the order history page, so
   the orchestrator can reconcile instead of retrying.
3. **Register a builder** in `src/spenddat/vendors/__init__.py`:
   `ADAPTER_BUILDERS["mytype"] = _build_mytype`.
4. **Certify** it: add a factory to `ADAPTERS` in `tests/contract/test_adapter_contract.py`
   and run `make test`. The contract suite checks search, pricing without charging,
   idempotent purchase, token-cap enforcement, cancel, and status reporting.
5. Add the `vendors.yaml` entry and secrets as in section 1.
