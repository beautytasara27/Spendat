# ADR-0004: Vendor A is an Agentic Commerce Protocol merchant (official API)

**Status:** accepted · 2026-09-29

## Terms-of-service review (PRD §2)
| Candidate | Automated purchasing stance | Outcome |
|---|---|---|
| Amazon (browser automation) | Conditions of Use prohibit robots / data-gathering tools; no public checkout API for third parties | **Rejected** |
| Walmart, Target, Best Buy (browser automation) | Site terms generally restrict automated access; no public buyer checkout API (re-check current terms before reconsidering) | **Rejected** |
| Shopify storefronts | Storefront API builds carts; card payment requires the hosted checkout | Not a full API path |
| **ACP merchants** (Stripe/OpenAI open standard) | Merchant publishes `POST /checkout_sessions`, `/{id}`, `/{id}/complete`, `/{id}/cancel` *specifically for AI agents*, paid by SPT | **Chosen** |

Because an ACP merchant opts in by exposing these endpoints, agentic purchasing is the
intended use, not a ToS grey area. Operators must still read the specific merchant's
agent/ACP terms during onboarding and record them here before going live.

## Design
`AcpAdapter` is generic over any ACP merchant: base URL, product-feed URL, bearer key, Stripe
seller profile and API version come from `config/vendors.yaml` per mode. Discovery uses the
merchant's product feed (OpenAI Product Feed Spec JSON), since ACP starts at checkout.
`resolve` creates a session with the shipping address, selects the cheapest fulfilment option,
and checks the returned cart matches the requested SKU/quantity; `purchase` sends
`payment_data {token, provider: "stripe"}` with an `Idempotency-Key`; `order_status` reads the
session for reconciliation.

Browser automation (and its session persistence/partial-failure handling) is therefore not
needed for Vendor A. The partial-failure contract still applies through `order_status` and is
exercised by tests with injected lost responses.

## Sandbox
A reference ACP merchant (`spenddat sandbox-merchant`) is bundled for tests and local runs;
given a Stripe test key it charges the received SPT with a test-mode PaymentIntent, as a real
ACP merchant would. A merchant's own ACP staging endpoint can be used instead.
