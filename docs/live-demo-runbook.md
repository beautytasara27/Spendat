# Live-mode demonstration runbook

This performs **real purchases with real money**. Run it deliberately, by hand. The test suite
and CI can never do this: they have no live credentials and the fake provider refuses live mode.

## Prerequisites (one time)

1. **Merchant (Vendor A).** Onboard with a merchant that exposes Agentic Commerce Protocol
   checkout endpoints to agents. From them obtain: ACP base URL, product feed URL, an API
   bearer key for your agent, and their Stripe `network_business_profile` id. Record their
   agent terms in `docs/adr/0004-vendor-a-acp.md`. Fill in `vendors.yaml → live:`.
2. **Stripe (payments).** In your own Stripe account (US): accept the agentic-commerce
   (Shared Payment Tokens) terms; save the buyer's card as a PaymentMethod on a Customer via
   Stripe Elements/Checkout (the card is entered into Stripe's UI, never into this system) and
   note its `pm_…` id; create a **restricted** live key with Shared Payment Tokens write
   access. Rehearse the whole flow first with test keys.
3. **Profile.** `cp config/profile.example.yaml config/profile.yaml` and fill in the real
   shipping address and buyer contact.
4. **Secrets** in your secrets manager (or `SPENDDAT_LIVE_*` env vars for a one-off):
   `stripe_secret_key`, `stripe_payment_method_id`, `anthropic_api_key`,
   `acp_merchant_api_key`, optionally `approval_webhook_url`.
5. Keep the default hard ceilings or lower them for the demo, e.g.
   `SPENDDAT_HARD_MAX_TRANSACTION_MINOR=10000`.

## Rehearse in sandbox

```bash
export SPENDDAT_MODE=sandbox        # with SPENDDAT_SANDBOX_* test credentials
spenddat check
spenddat buy "<same requests as below>"
```

## Go live

```bash
export SPENDDAT_MODE=live
export SPENDDAT_LIVE_CONFIRM=I_ACCEPT_REAL_PURCHASES
export SPENDDAT_DB=data/live.db
spenddat check                      # must print: configuration OK for LIVE mode
```

### Demo 1 — autonomous purchase (ALLOW, no human)

Pick an in-stock item whose **total incl. shipping** is within the policy caps for its category
(e.g. ≤ $30 for electronics accessories), and state a limit above its item price:

```bash
spenddat buy "buy <item> from acp merchant, under \$<limit>" --request-id demo-allow-1
```

Expected: `"state": "completed"` with an order id; no approval request is printed.

### Demo 2 — escalation then human approval (ESCALATE)

Pick an item above the per-transaction or category cap but below the hard ceiling:

```bash
spenddat buy "buy <pricier item> from acp merchant" --request-id demo-escalate-1
# -> pending_approval, with approval id and the specific reasons
spenddat approvals --mode live
spenddat approve <approval_id> --as "<your name>"
```

Expected: `"state": "completed"` with an order id.

## Evidence

```bash
spenddat history --mode live
spenddat show <txn_id_1>     # request → intent → policy ALLOW → execution_started → credential → succeeded → revoked
spenddat show <txn_id_2>     # ... policy ESCALATE → approval_requested → approval_decided → ... → succeeded
spenddat verify-audit
spenddat spend --mode live
```

Cross-check the order ids with the merchant's order confirmation emails and the charges in
the Stripe dashboard (live mode). Every audit event shows `[live]`.

If a command reports `needs_reconciliation`, do not re-run the purchase; run
`spenddat reconcile` and check the merchant order page.
