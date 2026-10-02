"""`spenddat stripe-check`: verify the Stripe setup for the current mode. No money moves.

It checks the agent's key and card, the shop's key and Stripe profile, then issues a
50-cent shared payment token to the shop and immediately revokes it. A token is only
permission to charge; revoking it before any shop uses it means nothing is charged.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from spenddat.models import RuntimeMode
from spenddat.payments import PaymentError, StripeSharedPaymentTokens
from spenddat.secrets import SecretsProvider
from spenddat.shop import SHOP_ID, check_profiles

API = "https://api.stripe.com"


@dataclass
class Report:
    lines: list[tuple[bool | None, str, str]] = field(default_factory=list)

    def add(self, ok: bool | None, what: str, hint: str = "") -> None:
        self.lines.append((ok, what, hint))

    @property
    def ok(self) -> bool:
        return all(ok is not False for ok, _, _ in self.lines)

    def render(self) -> str:
        out = []
        for ok, what, hint in self.lines:
            mark = {True: "OK  ", False: "FAIL", None: "note"}[ok]
            out.append(f"[{mark}] {what}")
            if hint:
                out.append(f"       -> {hint}")
        return "\n".join(out)


def _account(http: httpx.Client, key: str) -> tuple[dict | None, str | None]:
    r = http.get(f"{API}/v1/account", auth=(key, ""))
    if r.status_code == 200:
        return r.json(), None
    try:
        msg = r.json().get("error", {}).get("message", "")
    except ValueError:
        msg = r.text[:120]
    return None, f"HTTP {r.status_code}: {msg}"


def _find_cards(http: httpx.Client, key: str) -> list[str]:
    """List card payment methods saved on the account's customers (ids, brand, last 4)."""
    out: list[str] = []
    r = http.get(f"{API}/v1/customers", params={"limit": 10}, auth=(key, ""))
    if r.status_code != 200:
        return out
    for c in r.json().get("data", []):
        pr = http.get(
            f"{API}/v1/customers/{c['id']}/payment_methods",
            params={"type": "card", "limit": 5},
            auth=(key, ""),
        )
        if pr.status_code != 200:
            continue
        for p in pr.json().get("data", []):
            card = p.get("card") or {}
            who = c.get("name") or c.get("email") or c["id"]
            out.append(
                f"{p['id']}  {card.get('brand', 'card')} ending {card.get('last4')}  ({who})"
            )
    return out


def run_check(
    mode: RuntimeMode,
    secrets: SecretsProvider,
    env: dict[str, str],
    vendors_file: Path,
    client: httpx.Client | None = None,
) -> Report:
    http = client or httpx.Client(timeout=30)
    rep = Report()
    want = "live" if mode is RuntimeMode.LIVE else "test"
    rep.add(None, f"Checking {mode.value.upper()} mode (Stripe {want} keys). No money moves.")

    # ---- agent (Spenddat) side
    akey = secrets.get("stripe_secret_key") or ""
    pm = secrets.get("stripe_payment_method_id") or ""
    agent_acct = None
    if not akey.startswith((f"sk_{want}_", f"rk_{want}_")):
        rep.add(
            False,
            "Agent Stripe key",
            f"set SPENDDAT_{mode.value.upper()}_STRIPE_SECRET_KEY to a {want} key "
            f"(sk_{want}_... or rk_{want}_...)",
        )
    else:
        agent_acct, err = _account(http, akey)
        if agent_acct:
            rep.add(True, f"Agent Stripe key works (account {agent_acct.get('id')})")
            if mode is RuntimeMode.LIVE and not agent_acct.get("charges_enabled"):
                rep.add(
                    None,
                    "Agent account isn't fully activated",
                    "if issuing a token fails below, finish activation in the Dashboard",
                )
        elif err and "403" in err:
            rep.add(None, "Agent key can't read account details (restricted key) - continuing")
        else:
            rep.add(False, "Agent Stripe key rejected", err or "")

    if not pm.startswith("pm_") and akey.startswith((f"sk_{want}_", f"rk_{want}_")):
        found = _find_cards(http, akey)
        if found:
            rep.add(None, "Cards saved on the agent account (copy one pm_ id into .env):")
            for line in found:
                rep.add(None, f"    {line}")
    if not pm.startswith("pm_"):
        rep.add(
            False,
            "Card on file",
            f"set SPENDDAT_{mode.value.upper()}_STRIPE_PAYMENT_METHOD_ID to your card's pm_... id"
            + (" (in test mode you can use pm_card_visa)" if want == "test" else ""),
        )
    elif pm.startswith("pm_card_"):
        if want == "live":
            rep.add(
                False,
                "Card on file",
                "pm_card_* ids are Stripe test cards; use your real card's pm_ id",
            )
        else:
            rep.add(True, f"Card on file: Stripe test card ({pm})")
    elif akey:
        r = http.get(f"{API}/v1/payment_methods/{pm}", auth=(akey, ""))
        if r.status_code == 200:
            card = r.json().get("card") or {}
            rep.add(
                True,
                f"Card on file: {card.get('brand', 'card')} ending {card.get('last4', '????')}",
            )
        else:
            rep.add(False, "Card on file", f"Stripe can't find {pm} on the agent account")

    # ---- shop (seller) side
    skey = env.get("SPENDDAT_SHOP_STRIPE_SECRET_KEY", "")
    shop_acct = None
    if not skey.startswith((f"sk_{want}_", f"rk_{want}_")):
        rep.add(
            False,
            "Shop Stripe key",
            f"set SPENDDAT_SHOP_STRIPE_SECRET_KEY to the shop account's {want} key",
        )
    else:
        shop_acct, err = _account(http, skey)
        if shop_acct:
            rep.add(True, f"Shop Stripe key works (account {shop_acct.get('id')})")
            if mode is RuntimeMode.LIVE and not shop_acct.get("charges_enabled"):
                rep.add(
                    False,
                    "Shop account can't take live payments yet",
                    "finish account activation in the Stripe Dashboard",
                )
        elif err and "403" in err:
            rep.add(None, "Shop key can't read account details (restricted key) - continuing")
        else:
            rep.add(False, "Shop Stripe key rejected", err or "")
    # Separation of roles: the agent's account holds your card, the shop's account takes
    # the money. They must be two different Stripe accounts.
    if akey and skey and akey == skey:
        rep.add(
            False,
            "Agent and shop are using the same key",
            "the shop needs its own Stripe account - put that account's key in "
            "SPENDDAT_SHOP_STRIPE_SECRET_KEY",
        )
    elif agent_acct and shop_acct:
        if agent_acct.get("id") == shop_acct.get("id"):
            rep.add(
                False,
                f"Agent and shop are the same Stripe account ({agent_acct.get('id')})",
                "create a separate account for the shop (Dashboard account menu > "
                "New account) and use its key for SPENDDAT_SHOP_STRIPE_SECRET_KEY",
            )
        else:
            rep.add(
                True,
                f"Agent ({agent_acct.get('id')}) and shop ({shop_acct.get('id')}) "
                "are separate Stripe accounts",
            )
    elif akey and skey:
        rep.add(
            None,
            "Couldn't confirm the agent and shop are different accounts "
            "(restricted keys can't read account details) - make sure they are",
        )

    profiles = check_profiles(vendors_file)
    profile = profiles.get(mode.value) or ""
    if not profile.startswith("profile_") or "REPLACE" in profile:
        rep.add(
            False,
            "Shop's Stripe profile",
            f"run: spenddat shop init --{'live' if want == 'live' else 'test'}-profile "
            "profile_...  (Dashboard > Settings > Profile)",
        )
    elif (want == "test") != profile.startswith("profile_test_"):
        rep.add(False, "Shop's Stripe profile", f"{profile} is not a {want}-mode profile id")
    else:
        rep.add(True, f"Shop's Stripe profile: {profile}")

    # ---- dry run: issue and revoke a 50-cent token (only if everything above passed)
    if rep.ok and akey and pm:
        try:
            provider = StripeSharedPaymentTokens(mode, akey, pm, client=http)
            cred = provider.issue(
                amount_minor=50,
                currency="usd",
                seller=profile,
                ttl=timedelta(minutes=5),
                idempotency_key=f"stripe-check-{uuid.uuid4().hex}",
                now=datetime.now(UTC),
                metadata={"purpose": "spenddat stripe-check"},
            )
            time.sleep(0.2)
            provider.revoke(cred.token)
            rep.add(True, "Issued a 50-cent agent payment token to your shop and revoked it")
        except PaymentError as e:
            hint = str(e)
            if (
                "preview" in hint.lower()
                or "not enabled" in hint.lower()
                or "access" in hint.lower()
            ):
                hint += " -- agent payment tokens may need to be enabled for your account"
            rep.add(False, "Issuing an agent payment token", hint)
    elif rep.ok:
        rep.add(False, "Token dry run skipped", "fix the items above first")
    rep.add(None, f"Shop store id in Spenddat: {SHOP_ID}")
    return rep
