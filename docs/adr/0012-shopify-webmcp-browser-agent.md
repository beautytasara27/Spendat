# ADR 0012 — Shopify stores through WebMCP in a real browser

Status: accepted (2026-10-21)

## Context

Most retailers forbid automated purchasing. Shopify, since September 2026, lets agents
check out on its stores through WebMCP tools published in the page
(`document.modelContext`), paying with the buyer's Shop Pay saved instrument. The buyer
approves every order; Shopify may add an on-page review (`requires_escalation`).

## Decision

* New adapter `shopify_web` driving a persistent, visible Chrome profile via Playwright,
  calling **only** the store's WebMCP tools (no clicking or scraping).
* `payment_kind = "merchant_wallet"`: no Stripe token is issued; the cap the user approved
  is re-checked against the checkout total immediately before `complete_checkout`.
* `requires_buyer_confirmation = True` → a policy rule escalates every Shopify purchase;
  Auto-buy is refused for these stores.
* Storefront tool arguments are mapped from each tool's `inputSchema` at run time;
  `spenddat browser inspect` captures real schemas for adjustment.
* Sandbox mode is a dry run unless the store is marked as a development store.
* Optional Web Bot Auth (Ed25519, RFC 9421) request signing.
* A completion that fails or times out is "outcome unknown" → reconciliation, never retry.

## Consequences

+ Real, permitted purchases on a large set of stores without partner onboarding.
− Depends on a browser feature still behind flags; storefront formats unverified on real
  stores; a visible browser must be running on the user's machine.
