# Make a real purchase from your own shop

No public shop lets an independent agent buy yet, so you run one: a tiny agent-checkout
shop on your computer with **its own Stripe account**. Spenddat, using a **separate agent
Stripe account** that holds your card, buys from it with **your real card**; the money
lands in **the shop's Stripe balance**, and one command refunds it.

What you'll do: **Part A** rehearses everything with Stripe test money, then **Part B**
does it for real: one $1.00 purchase the agent makes on its own, one $2.00 purchase it
asks you to approve. Those two are the PRD's required live demo.

> **Cost.** The $3.00 is refunded, but Stripe keeps its normal card fee on each payment
> (in the US, about 30-35 cents per payment, charged to the shop account), so the live
> demo costs well under $1.

All commands run in Command Prompt, in your `spenddat` folder, with `.venv` active.

---

## Before you start

1. **Update and install:** `pip install -e ".[dev]"`
2. **Your address:** `copy config\profile.example.yaml config\profile.yaml`, then open it and
   put your real name and address. (Nothing ships, but live mode requires one.)
3. **Set up the shop files:** `spenddat shop init`

   This creates `config\shop.yaml` (two products: $1.00 and $2.00), adds a private key the
   agent uses to talk to the shop to your `.env`, adds the store "My Test Shop" to your
   store list set to **Auto-buy**, and adds a `digital_goods` category with a $1.00
   limit, so the $1 item buys on its own and the $2 item asks you.

## Two Stripe accounts, two jobs

| | **Agent account** ("Spenddat Agent") | **Shop account** ("My Test Shop") |
|---|---|---|
| Job | Holds your card; gives the shop a one-time, capped permission to charge it | Sells the items; takes the payment; issues refunds |
| Keys in `.env` | `SPENDDAT_SANDBOX_STRIPE_SECRET_KEY` / `SPENDDAT_LIVE_STRIPE_SECRET_KEY` | `SPENDDAT_SHOP_STRIPE_SECRET_KEY` |
| Your card | saved here (live) | never |
| Stripe profile | needs one | needs one; its id goes into `spenddat shop init` |
| Money | your card is charged | the payment lands here |

You create both under one Stripe login: in the Dashboard, open the account menu at the top
left and choose **New account**. Name them "Spenddat Agent" and "My Test Shop" so it's
always clear which one you're in. Spenddat enforces the split: `stripe-check` fails if both
keys belong to the same account, and the shop won't start with the agent's key.

---

## Part A: rehearse with Stripe test money (free)

1. **Create the two accounts** (see above): "Spenddat Agent" and "My Test Shop".
2. **Stripe profiles.** For **each** account: switch to it, go to test mode (or its
   sandbox), open <https://dashboard.stripe.com/profiles> and create the profile if asked.
   Copy the **shop** account's test profile id (it starts with `profile_test_`).
3. **Tell Spenddat the shop's profile:** `spenddat shop init --test-profile profile_test_...`
4. **Test keys.** In each account's test mode, open **Developers > API keys** and copy the
   **Secret key** (`sk_test_...`). Add to `.env`:
   ```
   SPENDDAT_MODE=sandbox
   SPENDDAT_SANDBOX_STRIPE_SECRET_KEY=sk_test_...        # AGENT account
   SPENDDAT_SANDBOX_STRIPE_PAYMENT_METHOD_ID=pm_card_visa # a Stripe test card
   SPENDDAT_SHOP_STRIPE_SECRET_KEY=sk_test_...           # SHOP account
   SPENDDAT_SANDBOX_OPENROUTER_API_KEY=sk-or-...         # your OpenRouter key
   ```
5. **Check the setup:** `spenddat stripe-check`

   It confirms the two keys are separate accounts, checks the card and the shop's profile,
   then has the agent give the shop a 50-cent payment permission and cancels it straight
   away. Nothing is charged. Fix anything marked FAIL.
6. **Open the shop** in one Command Prompt window: `spenddat shop run`
7. **Open Spenddat** in a second window (same folder, `.venv` active): `spenddat ui`

   Not `--demo`; this uses your real config. In Chat, ask:
   - *"Buy a Spenddat thank-you note"* — should complete on its own ($1.00).
   - *"Buy a Spenddat supporter pass"* — should ask you; press **Approve** ($2.00).
8. **Look in Stripe** (test mode): the two payments appear under **Payments** in the
   **shop** account. `spenddat shop orders` lists them; `spenddat shop refund --all`
   refunds them.

If both worked, everything is wired correctly. Part B is the same with live keys.

---

## Part B: the real purchases (real money)

1. **Activate payments on the shop account** (switch to "My Test Shop", Dashboard home,
   "Activate payments"): your details, identity and a bank account. Stripe needs this before
   an account can take real card payments. If `stripe-check` later says the agent account
   can't issue live payment tokens, activate that account too.
2. **Shop's live profile id.** In the shop account, outside test mode, open
   <https://dashboard.stripe.com/profiles>, copy the live profile id (`profile_...`, without
   `test`), then run `spenddat shop init --live-profile profile_...`
3. **Save your card on the agent account.** Switch to "Spenddat Agent", live mode:
   **Customers > Add customer** (yourself), open the customer, then **Add payment method**
   and enter your card. You type the card into Stripe's own page; Spenddat never sees the
   number.
4. **Live keys in `.env`.** Copy each account's live **Secret key** (`sk_live_...`).
   Restricted keys are safer: **Developers > API keys > Create restricted key**. Then edit
   `.env`:
   ```
   SPENDDAT_MODE=live
   SPENDDAT_LIVE_CONFIRM=I_ACCEPT_REAL_PURCHASES
   SPENDDAT_DB=data/live.db
   SPENDDAT_LIVE_STRIPE_SECRET_KEY=sk_live_...          # AGENT account
   SPENDDAT_SHOP_STRIPE_SECRET_KEY=sk_live_...          # SHOP account
   SPENDDAT_LIVE_OPENROUTER_API_KEY=sk-or-...
   ```
   Now run `spenddat stripe-check`. Because the card id is still missing, it lists the
   cards saved on the agent account. Copy yours into `.env` as
   `SPENDDAT_LIVE_STRIPE_PAYMENT_METHOD_ID=pm_...` and run `spenddat stripe-check` again
   until it says **All good**.
5. **Confirm the whole config:** `spenddat check` should print `configuration OK for LIVE mode`.
6. **Open the shop:** `spenddat shop run` (it says **LIVE - real cards, real money**).
7. **Open Spenddat:** `spenddat ui`. The top bar shows a red **LIVE** pill. Then:
   - **Demo 1, autonomous:** *"Buy a Spenddat thank-you note"*. It completes with no
     approval and shows a receipt for $1.00.
   - **Demo 2, approved by you:** *"Buy a Spenddat supporter pass"*. It shows an approval
     card ($2.00, over the $1.00 digital_goods limit). Press **Approve**.
8. **Evidence:** the Dashboard view lists both, with full audit trails (click a row).
   Also: `spenddat history --mode live`, `spenddat show <txn_id>`, `spenddat verify-audit`,
   the charges on your card in the agent account, and the two payments in the shop account.
9. **Get your money back:** `spenddat shop refund --all`

---

## If something goes wrong

| What you see | What it means / what to do |
|---|---|
| `stripe-check`: "same Stripe account" or "same key" | `SPENDDAT_SHOP_STRIPE_SECRET_KEY` must come from the shop account, the other keys from the agent account. Check which account is selected when you copy keys. |
| `stripe-check`: "Issuing an agent payment token" FAIL | Agent payment tokens may need enabling on your account; the message has Stripe's reason. Contact Stripe support quoting "Shared Payment Tokens (agentic commerce preview)". |
| Test mode: token FAIL mentioning the payment method | Stripe may not accept the `pm_card_visa` shortcut here. In test mode add a customer with card `4242 4242 4242 4242` (any future date, any CVC), then run `stripe-check` again to get its `pm_` id. |
| Purchase fails: "card declined" | Your bank refused it; nothing was charged. Try another card. |
| Purchase fails: "3-D Secure" | Your bank wanted an extra verification step that an agent can't complete yet. Nothing was charged. Another card may work. |
| Chat says it isn't connected to a store | Is `spenddat shop run` still open in the other window? |
| `spenddat check` mentions placeholders | Run `spenddat shop init --live-profile profile_...`. |
| Purchase shows **needs reconciliation** | Don't retry. Run `spenddat reconcile`; it asks the shop what happened. |

The shop only listens on your own computer (127.0.0.1). That's why live mode accepts
plain `http://127.0.0.1` for it while still requiring https for any other store.
