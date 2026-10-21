# Shopping on Shopify stores (browser agent)

Shopify lets AI agents check out on its stores through **WebMCP**: the store publishes
"tools" (search, cart, checkout) inside the web page, and an agent running in the browser
calls them. Spenddat opens a real Chrome window, calls those tools, and pays with **your
Shop Pay saved card**. It never clicks around, scrapes pages, or sees your card number.

Every Shopify order needs your approval in Spenddat. Shopify may *also* ask you to
review the order on the checkout page in the Chrome window; Spenddat waits up to 10
minutes for that, then finishes.

## 1. Install (once)

```bash
pip install -e ".[browser]"      # Playwright + cryptography
```

You also need **Google Chrome** (recent; WebMCP is in Chrome 153+). Spenddat starts it
with the WebMCP flags itself. No Chrome? Use Playwright's Chromium instead:

```bash
playwright install chromium
# in .env
SPENDDAT_BROWSER_CHANNEL=chromium
```

## 2. Sign in to Shop Pay (once)

```bash
spenddat browser login
```

A Chrome window opens on shop.app. Sign in and make sure a card is saved in Shop Pay,
then close the window. The sign-in is kept in `data/browser-profile` (git-ignored) so the
agent can use your saved card. Spenddat never reads your passwords or cookies.

## 3. Add a store

In the web UI (`spenddat ui`): **Stores → Add store → Shopify store**, paste the store
address (e.g. `https://kicks.example.com`). Shopify stores are always **Ask me first**;
Auto-buy is turned off for them on purpose.

Click **Test connection** to check the store publishes agent tools.

## 4. What happens in each mode

| Mode | What the agent does |
|---|---|
| **Sandbox**, normal store | Everything up to the final step: search, cart, checkout, price. Then it stops. **No order is placed** (you'll see "dry run"). |
| **Sandbox**, *development store* box ticked | Places the order. Use this only with a Shopify development store with test payments, where nothing is charged. |
| **Live** | Places real orders and charges your Shop Pay card, after your approval. |

The policy still applies everywhere: budgets, category caps, blocked categories, and the
hard circuit breakers. If the checkout total goes up after you approved it, the order is
refused.

## 5. If a store doesn't work: inspect it

Shopify documents its checkout tools but **not the exact input formats of its storefront
tools** (search, cart). Spenddat reads each tool's schema at run time and adapts, but a
real store may differ. To see what a store offers:

```bash
spenddat browser inspect https://kicks.example.com --query "samba"
```

This prints the tools and saves their schemas plus a sample search to
`data/browser/inspect-<host>.json`. If a purchase fails with a "schema mismatch" message,
send that file and the adapter mapping can be fixed.

## 6. Web Bot Auth (recommended for live)

Stores use bot protection. Shopify recognises agents that sign their requests with
**Web Bot Auth** and whose key is registered with Shopify.

```bash
spenddat browser keys            # creates data/wba/key.pem + the public key directory
```

1. Host `data/wba/http-message-signatures-directory` at a public https URL you control,
   e.g. `https://<you>.github.io/.well-known/http-message-signatures-directory`.
   (Never publish `key.pem`.)
2. Register that URL with Shopify (shopify.dev → agents → Web Bot Auth).
3. Add to `.env`: `SPENDDAT_WBA_DIRECTORY_URL=<that URL>`.

From then on, Spenddat signs every request its browser makes to stores.

## Limitations (honest list)

* WebMCP is new: in Chrome it is still behind flags / an origin trial. Spenddat sets the
  flags, but a Chrome update may change behaviour.
* Storefront tool formats are unpublished; the mapping is tested against a simulated
  store (`tests/fake_shopify.py`), not yet against a real one. Use `browser inspect`.
* The Web Bot Auth signature follows the public draft but hasn't been checked against
  Shopify's verifier.
* Only stores that have agent checkout turned on work. Stores can opt out.
* One item per order (same as the rest of Spenddat).

Settings (`.env`): `SPENDDAT_BROWSER_CHANNEL` (chrome | msedge | chromium),
`SPENDDAT_BROWSER_PROFILE`, `SPENDDAT_BROWSER_HEADLESS=1` (tests only; Shopify reviews
need a visible window), `SPENDDAT_BROWSER_EXECUTABLE`, `SPENDDAT_WBA_DIRECTORY_URL`.
