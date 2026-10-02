"""Your own agent-checkout shop, charging real cards through your own Stripe account.

There's no public shop yet that lets an independent agent buy, so this runs one: the
same Agentic Commerce Protocol server the tests use, plus a product list from
``config/shop.yaml``, saved orders, and seller-side charging with *your* Stripe key.
Test keys move test money; live keys move real money (to your own Stripe balance), and
``spenddat shop refund`` sends it back.

Secrets (in ``.env`` or your secrets manager, never in files in the repo):

* ``SPENDDAT_SHOP_STRIPE_SECRET_KEY`` - the shop's Stripe secret key (``sk_test_``/``sk_live_``,
  or a restricted ``rk_`` key). Its prefix decides whether the shop is in test or live mode.
* ``SPENDDAT_SHOP_AGENT_KEY`` - the key an agent must present to use the shop's checkout.
  Spenddat reads the same value as ``SPENDDAT_<MODE>_MY_SHOP_API_KEY``.

The shop only listens on 127.0.0.1: it is a shop for your own agent on your own machine.
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import yaml

from spenddat.models import RuntimeMode
from spenddat.sandbox.reference_merchant import ReferenceMerchant, serve, stripe_charger
from spenddat.stores import StoreConfig, StoreConfigError

SHOP_ID = "my_shop"
DEFAULT_PORT = 8788

DEFAULT_SHOP_YAML = """\
# Your own agent-checkout shop (`spenddat shop run`). Prices are integer cents.
# Stripe's minimum charge is $0.50. Buying from this shop charges YOUR card and pays YOUR
# Stripe account; `spenddat shop refund --all` refunds every order.
name: My Test Shop
port: {port}
currency: usd

fulfillment:                 # one option = chosen automatically
  - id: digital
    title: Digital delivery
    subtitle: Instant
    total: 0

products:
  - id: NOTE-1
    title: Spenddat Thank-You Note
    description: A digital thank-you note. A real $1.00 payment to your own shop.
    price_minor: 100
    product_category: digital_goods
  - id: PASS-2
    title: Spenddat Supporter Pass
    description: A digital supporter pass. A real $2.00 payment to your own shop.
    price_minor: 200
    product_category: digital_goods
"""


class ShopError(RuntimeError):
    pass


@dataclass(frozen=True)
class ShopSettings:
    name: str
    port: int
    products: list[dict[str, Any]]
    fulfillment: list[dict[str, Any]]
    stripe_key: str
    agent_key: str

    @property
    def live(self) -> bool:
        return self.stripe_key.startswith(("sk_live_", "rk_live_"))

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"


def load_shop(path: str | Path, env: dict[str, str] | None = None) -> ShopSettings:
    e = env if env is not None else os.environ
    p = Path(path)
    if not p.exists():
        raise ShopError(f"{p} not found - run `spenddat shop init` first")
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    products = list(data.get("products") or [])
    if not products:
        raise ShopError("shop.yaml has no products")
    for prod in products:
        missing = {"id", "title", "price_minor", "product_category"} - set(prod)
        if missing:
            raise ShopError(f"product {prod.get('id', '?')} is missing {sorted(missing)}")
        prod.setdefault("description", prod["title"])
        if int(prod["price_minor"]) < 50:
            raise ShopError(f"product {prod['id']}: Stripe's minimum charge is 50 cents")
    key = e.get("SPENDDAT_SHOP_STRIPE_SECRET_KEY", "").strip()
    if not key.startswith(("sk_test_", "sk_live_", "rk_test_", "rk_live_")):
        raise ShopError("set SPENDDAT_SHOP_STRIPE_SECRET_KEY to the shop's Stripe secret key")
    agent_key = e.get("SPENDDAT_SHOP_AGENT_KEY", "").strip()
    if len(agent_key) < 20:
        raise ShopError("SPENDDAT_SHOP_AGENT_KEY is missing - run `spenddat shop init`")
    return ShopSettings(
        name=str(data.get("name") or "My Test Shop"),
        port=int(data.get("port") or DEFAULT_PORT),
        products=products,
        fulfillment=list(data.get("fulfillment") or [])
        or [{"id": "digital", "title": "Digital delivery", "total": 0}],
        stripe_key=key,
        agent_key=agent_key,
    )


def build_merchant(s: ShopSettings, state_dir: str | Path) -> ReferenceMerchant:
    mode = "live" if s.live else "test"
    return ReferenceMerchant(
        api_key=s.agent_key,
        catalog=s.products,
        fulfillment=s.fulfillment,
        charger=stripe_charger(s.stripe_key, allow_live=True),
        state_file=Path(state_dir) / f"orders-{mode}.json",
        link_base=s.base_url,
    )


def run_shop(s: ShopSettings, state_dir: str | Path) -> None:
    merchant = build_merchant(s, state_dir)
    mode = "LIVE - real cards, real money" if s.live else "TEST - Stripe test mode"
    print(f"{s.name} is open at {s.base_url}  [{mode}]  (Ctrl+C to close)")
    for p in s.products:
        print(f"  {p['id']:<10} ${int(p['price_minor']) / 100:>7.2f}  {p['title']}")
    try:
        serve(merchant, "127.0.0.1", s.port)
    except KeyboardInterrupt:
        print("\nshop closed")


def refund_orders(
    s: ShopSettings,
    state_dir: str | Path,
    order_id: str | None,
    client: httpx.Client | None = None,
) -> list[tuple[str, str]]:
    """Refund one order or all orders. Returns [(order_id, result)]."""
    merchant = build_merchant(s, state_dir)
    http = client or httpx.Client(timeout=30)
    out = []
    for o in merchant.orders():
        if order_id and o["id"] != order_id:
            continue
        pi = o.get("payment_reference") or ""
        if not pi.startswith("pi_"):
            out.append((o["id"], "no Stripe payment to refund"))
            continue
        r = http.post(
            "https://api.stripe.com/v1/refunds",
            auth=(s.stripe_key, ""),
            data={"payment_intent": pi},
            headers={"Idempotency-Key": f"refund_{pi}"},
        )
        body = r.json() if r.content else {}
        if r.status_code < 400:
            out.append(
                (o["id"], f"refunded ${body.get('amount', 0) / 100:.2f} ({body.get('status')})")
            )
        else:
            msg = body.get("error", {}).get("message", f"HTTP {r.status_code}")
            out.append((o["id"], f"not refunded: {msg}"))
    if order_id and not out:
        out.append((order_id, "no such order"))
    return out


# ---------------------------------------------------------------------------- init


def _env_lines(env_file: Path) -> dict[str, str]:
    if not env_file.exists():
        return {}
    out = {}
    for line in env_file.read_text(encoding="utf-8-sig").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, _, v = line.partition("=")
            out[k.strip().removeprefix("export ").strip()] = v.strip()
    return out


def init_shop(
    *,
    shop_file: Path,
    env_file: Path,
    vendors_file: Path,
    policy_file: Path,
    test_profile: str | None,
    live_profile: str | None,
    port: int = DEFAULT_PORT,
) -> list[str]:
    """Create/refresh the shop config, the agent key, the store entry and the policy bits.

    Safe to run again (e.g. to add profile ids later). Returns a list of what it did.
    """
    done: list[str] = []
    if not shop_file.exists():
        shop_file.parent.mkdir(parents=True, exist_ok=True)
        shop_file.write_text(DEFAULT_SHOP_YAML.format(port=port), encoding="utf-8")
        done.append(f"created {shop_file} with a $1.00 and a $2.00 product")
    else:
        port = int(
            (yaml.safe_load(shop_file.read_text(encoding="utf-8")) or {}).get("port") or port
        )

    # agent key: one secret, readable by the shop and by Spenddat in both modes
    have = _env_lines(env_file)
    key = have.get("SPENDDAT_SHOP_AGENT_KEY") or f"shopkey_{secrets.token_urlsafe(24)}"
    wanted = {
        "SPENDDAT_SHOP_AGENT_KEY": key,
        "SPENDDAT_SANDBOX_MY_SHOP_API_KEY": key,
        "SPENDDAT_LIVE_MY_SHOP_API_KEY": key,
    }
    missing = {k: v for k, v in wanted.items() if k not in have}
    if missing:
        with env_file.open("a", encoding="utf-8") as f:
            if env_file.stat().st_size and not env_file.read_text(encoding="utf-8").endswith("\n"):
                f.write("\n")
            f.write("\n# ---- your own test shop (spenddat shop) - generated, keep private\n")
            for k, v in missing.items():
                f.write(f"{k}={v}\n")
            if "SPENDDAT_SHOP_STRIPE_SECRET_KEY" not in have:
                f.write(
                    "# SPENDDAT_SHOP_STRIPE_SECRET_KEY=sk_test_...   # the SHOP account's key\n"
                )
        done.append(f"added the shop's agent key to {env_file}")

    # store entry (both modes) via the comment-preserving editor
    base = f"http://127.0.0.1:{port}"
    for mode_name, profile in (("sandbox", test_profile), ("live", live_profile)):
        if profile and not profile.startswith("profile_"):
            raise ShopError(f"{mode_name} profile must start with profile_")
    from ruamel.yaml.comments import CommentedMap

    cfg = StoreConfig(vendors_file, policy_file, RuntimeMode.SANDBOX)
    doc, vendors = cfg._vendors()
    entry = next((v for v in vendors if v.get("id") == SHOP_ID), None)
    if entry is None:
        entry = CommentedMap()
        entry["id"] = SHOP_ID
        entry["name"] = "My Test Shop"
        entry["type"] = "acp"
        entry["enabled"] = True
        vendors.append(entry)
        done.append(f"added store '{SHOP_ID}' to {vendors_file}")
    for mode_name, profile in (("sandbox", test_profile), ("live", live_profile)):
        section = entry.get(mode_name) or CommentedMap()
        section["base_url"] = base
        section["feed_url"] = f"{base}/feed.json"
        if profile:
            section["seller_profile"] = profile
            done.append(f"set the shop's {mode_name} Stripe profile")
        section.setdefault(
            "seller_profile",
            "profile_test_REPLACE_ME" if mode_name == "sandbox" else "profile_REPLACE_ME",
        )
        section["api_key_secret"] = "my_shop_api_key"  # noqa: S105 - a secret *name*
        section["api_version"] = "2025-09-29"
        entry[mode_name] = section
    # the shipped example store only has placeholder live endpoints
    for v in vendors:
        placeholder = "REPLACE" in str((v.get("live") or {}).get("base_url", ""))
        if v.get("id") != SHOP_ID and placeholder and v.get("enabled", True):
            v["enabled"] = False
            done.append(f"turned off example store '{v.get('id')}' (placeholder endpoints)")
    cfg._write(vendors_file, cfg._dump(doc))

    # policy: a digital_goods category ($1.00 cap -> the $1 item auto-buys, the $2 asks)
    pdoc = cfg._load(policy_file)
    cats = pdoc["categories"]
    if "digital_goods" not in (cats.get("allow") or []):
        cats["allow"].append("digital_goods")
        done.append("added category digital_goods to the policy")
    caps = pdoc["caps"].setdefault("per_category_minor", CommentedMap())
    if "digital_goods" not in caps:
        caps["digital_goods"] = 100
        done.append("set a $1.00 per-purchase cap for digital_goods")
    allow = pdoc["vendors"]["allow"]
    if SHOP_ID not in allow:
        allow.append(SHOP_ID)
        done.append(f"set '{SHOP_ID}' to Auto-buy")
    cfg._write(policy_file, cfg._dump(pdoc))
    return done


def check_profiles(vendors_file: Path) -> dict[str, str | None]:
    try:
        cfg = StoreConfig(vendors_file, vendors_file, RuntimeMode.SANDBOX)
        _, vendors = cfg._vendors()
    except StoreConfigError:
        return {}
    entry = next((v for v in vendors if v.get("id") == SHOP_ID), None) or {}
    return {m: (entry.get(m) or {}).get("seller_profile") for m in ("sandbox", "live")}
