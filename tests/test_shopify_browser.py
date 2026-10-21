"""Shopify-through-the-browser, end to end, in a real headless Chromium.

The store is simulated (tests/fake_shopify.py) but everything else is real: Playwright,
the WebMCP tool calls with JSON-string arguments, page navigation from storefront to
checkout, the Spenddat pipeline, policy, approval and audit.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

pytest.importorskip("playwright.sync_api")

from spenddat.approval import ApprovalService, RecordingNotifier
from spenddat.audit import AuditStore
from spenddat.browser.session import BrowserConfig, BrowserSession
from spenddat.guards import HardLimits
from spenddat.intent import IntentParser, ScriptedLlm
from spenddat.models import ApprovalAction, RuntimeMode, TxnState
from spenddat.orchestrator import Orchestrator
from spenddat.payments import FakePaymentProvider
from spenddat.policy import PolicyEngine, load_policy
from spenddat.vendors.base import AdapterRegistry
from spenddat.vendors.shopify_web import (
    ShopifyWebAdapter,
    args_for_add,
    args_for_search,
    find_products,
    money_minor,
    products_to_candidates,
    tool_errors,
    unwrap,
)
from tests.conftest import POLICY_FILE, SHIP_TO
from tests.fake_shopify import FakeShopify

pytestmark = pytest.mark.browser


@pytest.fixture(scope="module")
def store():
    s = FakeShopify()
    yield s
    s.close()


@pytest.fixture
def session(tmp_path):
    s = BrowserSession(
        BrowserConfig(profile_dir=str(tmp_path / "profile"), channel=None, headless=True)
    )
    try:
        s.run(lambda b: b.context, timeout=60)
    except Exception as e:
        pytest.skip(f"no headless Chromium available: {e}")
    yield s
    s.close()


def set_flags(session, url, **flags):
    q = "&".join(f"{k}={1 if v else 0}" for k, v in flags.items())
    session.run(lambda b: b.page("store:sneaker_co", f"{url}/?{q}"))


class Rig:
    def __init__(self, tmp_path, store, session):
        self.session = session
        self.adapter = ShopifyWebAdapter(
            "sneaker_co",
            store.url,
            session,
            buyer={"email": "me@example.com"},
            escalation_timeout_s=8,
            poll_s=0.3,
        )
        self.adapter.display_name = "Sneaker Co"
        policy = load_policy(POLICY_FILE)
        reg = AdapterRegistry()
        reg.register(self.adapter)
        self.store = AuditStore(tmp_path / "audit.db")
        self.notifier = RecordingNotifier()
        self.llm = ScriptedLlm()
        self.payments = FakePaymentProvider(RuntimeMode.SANDBOX)
        self.orch = Orchestrator(
            mode=RuntimeMode.SANDBOX,
            store=self.store,
            parser=IntentParser(
                self.llm,
                supported_vendors={"sneaker_co"},
                autonomous_vendors={"sneaker_co"},  # even "trusted"...
                supported_categories=policy.categories.allow,
            ),
            engine=PolicyEngine(policy),
            approvals=ApprovalService(self.store, [self.notifier], timedelta(minutes=60)),
            registry=reg,
            payments=self.payments,
            hard_limits=HardLimits(50000, 60000, 90000),
            ship_to=SHIP_TO,
        )

    def ask(self, **kw):
        base = {
            "status": "ok",
            "vendor": "sneaker_co",
            "product_description": "adidas sambas",
            "category": "footwear",
            "quantity": 1,
            "max_price_usd": None,
        }
        base.update(kw)
        self.llm.push(base)
        return self.orch.submit("buy adidas sambas")

    def orders(self):
        url = self.adapter.store_url

        def job(b):
            p = b.page("probe", url)
            return p.evaluate("() => JSON.parse(localStorage.getItem('orders') || '0')")

        return self.session.run(job)


@pytest.fixture
def rig(tmp_path, store, session):
    return Rig(tmp_path, store, session)


# ---------------------------------------------------------------------------- full flows


def test_samba_size_and_colour_found_priced_and_escalated(rig):
    res = rig.ask(size="8.5", color="white")
    assert res.state is TxnState.PENDING_APPROVAL, res.message
    # Shopify purchases always need your OK, whatever the store's trust level:
    assert "merchant_requires_buyer_approval" in res.decision.reason_codes
    (req,) = rig.notifier.requests
    item = req.presented["item"]
    assert item["title"] == "adidas Samba OG (White, size 8.5)"
    assert item["unit_price_minor"] == 10000
    assert item["shipping_minor"] == 499  # cheapest option chosen
    assert item["total_minor"] == 10499


def test_approve_buys_with_saved_shop_pay_card_no_stripe_token(rig):
    res = rig.ask(size="8.5", color="white")
    done = rig.orch.decide_approval(res.approval_id, ApprovalAction.APPROVE, "me")
    assert done.state is TxnState.COMPLETED, done.message
    assert done.receipt.order_id == "#1001" and done.receipt.amount_minor == 10499
    assert rig.payments.issued == {}  # wallet payment: no Stripe credential issued
    ev = {e.event_type: e.payload for e in rig.store.events(res.txn_id)}
    assert ev["merchant_wallet_payment"]["instrument"] == "visa ending 4242"
    assert ev["merchant_wallet_payment"]["max_amount_minor"] == 10499
    assert rig.orders() == 1


def test_buyer_review_on_the_page_then_complete(rig, store):
    """complete_checkout asks for buyer review; the buyer approves on the page; the
    adapter sees the checkout ready again, re-checks the total and completes once."""
    set_flags(rig.session, store.url, review=True)
    res = rig.ask(size="9", color="black")
    done = rig.orch.decide_approval(res.approval_id, ApprovalAction.APPROVE, "me")
    assert done.state is TxnState.COMPLETED, done.message
    assert rig.orders() == 1


def test_price_rise_during_page_review_is_refused(rig, store):
    set_flags(rig.session, store.url, review=True, jump=True)
    res = rig.ask(size="8", color="white")
    done = rig.orch.decide_approval(res.approval_id, ApprovalAction.APPROVE, "me")
    assert done.state is TxnState.FAILED and "total changed" in done.message
    assert rig.orders() == 0


def test_no_saved_card_fails_cleanly(rig, store):
    set_flags(rig.session, store.url, nocard=True)
    res = rig.ask(size="8.5", color="white")
    done = rig.orch.decide_approval(res.approval_id, ApprovalAction.APPROVE, "me")
    assert done.state is TxnState.FAILED
    assert "Shop Pay" in done.message


def test_sold_out_variant_is_explained(rig):
    res = rig.ask(size="8.5", color="black")
    assert res.state is TxnState.NEEDS_CLARIFICATION
    assert "isn't available in black in size 8.5" in res.message


def test_health_check_counts_tools(rig):
    h = rig.adapter.health_check()
    assert h["ok"] and h["tools"] >= 6


# ---------------------------------------------------------------------------- unit helpers


@pytest.mark.parametrize(
    "v,want",
    [
        ("100.00", 10000),
        ({"amount": "14.5", "currencyCode": "USD"}, 1450),
        (1999, 1999),
        (12.5, 1250),
        ("$1,234.56", 123456),
        (None, None),
    ],
)
def test_money_minor(v, want):
    assert money_minor(v) == want


def test_args_mapping_variants():
    assert args_for_search({"properties": {"q": {"type": "string"}}}, "x") == {"q": "x"}
    assert args_for_add(
        {
            "properties": {
                "lines": {
                    "type": "array",
                    "items": {"properties": {"merchandiseId": {}, "quantity": {}}},
                }
            }
        },
        "v1",
        2,
    ) == {"lines": [{"merchandiseId": "v1", "quantity": 2}]}
    assert args_for_add(
        {"properties": {"variant_id": {}, "quantity": {}, "action": {"enum": ["add", "remove"]}}},
        "v1",
        1,
    ) == {"variant_id": "v1", "quantity": 1, "action": "add"}
    from spenddat.vendors.shopify_web import SchemaMismatch

    with pytest.raises(SchemaMismatch):
        args_for_add({"properties": {"note": {}}}, "v1", 1)


def test_product_parsing_from_storefront_api_shapes():
    res = {
        "content": [
            {
                "type": "text",
                "text": '{"products":[{"id":"p1","title":"Tee",'
                '"variants":{"edges":[{"node":{"id":"v1","price":{"amount":"28.0"},'
                '"availableForSale":true,"selectedOptions":[{"name":"Size","value":"M"},'
                '{"name":"Color","value":"Black"}]}}]}}]}',
            }
        ]
    }
    prods = find_products(unwrap(res))
    (c,) = products_to_candidates("s", "black tee", prods)
    assert (c.sku, c.unit_price_minor, c.size, c.color, c.group_id) == (
        "v1",
        2800,
        "M",
        "Black",
        "p1",
    )


def test_tool_errors_parsing():
    assert tool_errors(
        {
            "ucp": {"status": "error"},
            "messages": [{"type": "error", "code": "completion_failed", "content": "x"}],
        }
    ) == [("completion_failed", "x")]
    assert tool_errors({"status": "completed"}) == []


# ---------------------------------------------------------------------------- safety & config


def test_sandbox_dry_run_stops_before_placing_the_order(rig, store):
    rig.adapter.dry_run = True
    res = rig.ask(size="8.5", color="white")
    done = rig.orch.decide_approval(res.approval_id, ApprovalAction.APPROVE, "me")
    assert done.state is TxnState.FAILED
    assert "dry run" in done.message and "NOT placed" in done.message
    assert "$104.99" in done.message and "visa ending 4242" in done.message
    assert rig.orders() == 0


def test_build_adapter_dry_run_rules(monkeypatch):
    from spenddat.secrets import EnvSecrets
    from spenddat.vendors import build_adapter

    spec = {
        "id": "s",
        "type": "shopify_web",
        "name": "S",
        "sandbox": {"store_url": "https://s.example"},
        "live": {"store_url": "https://s.example"},
    }
    sb = build_adapter(spec, RuntimeMode.SANDBOX, EnvSecrets(RuntimeMode.SANDBOX, {}), None)
    live = build_adapter(spec, RuntimeMode.LIVE, EnvSecrets(RuntimeMode.LIVE, {}), None)
    spec["sandbox"]["test_store"] = True
    dev = build_adapter(spec, RuntimeMode.SANDBOX, EnvSecrets(RuntimeMode.SANDBOX, {}), None)
    assert (sb.dry_run, live.dry_run, dev.dry_run) == (True, False, False)
    assert sb.requires_buyer_confirmation and sb.payment_kind == "merchant_wallet"


def test_store_config_rules_for_shopify(tmp_path):
    from spenddat.stores import StoreConfig, StoreConfigError
    from tests.conftest import ROOT

    v = tmp_path / "v.yaml"
    v.write_text((ROOT / "config" / "vendors.yaml").read_text())
    pol = tmp_path / "p.yaml"
    pol.write_text(POLICY_FILE.read_text())
    cfg = StoreConfig(v, pol, RuntimeMode.LIVE)
    with pytest.raises(StoreConfigError, match="https"):
        cfg.add({"name": "Kicks", "type": "shopify_web", "store_url": "http://kicks.example"})
    with pytest.raises(StoreConfigError, match="Auto-buy"):
        cfg.add(
            {
                "name": "Kicks",
                "type": "shopify_web",
                "store_url": "https://kicks.example",
                "trust": "auto",
            }
        )
    cfg.remove("kicks")  # the failed add above was written before the trust check
    vid = cfg.add({"name": "Kicks", "type": "shopify_web", "store_url": "https://kicks.example/"})
    st = cfg.get(vid)
    assert st["store_url"] == "https://kicks.example" and st["trust"] == "ask"
    with pytest.raises(StoreConfigError, match="Auto-buy"):
        cfg.set_trust(vid, "auto")


def test_inspect_command_saves_tool_schemas(store, tmp_path, monkeypatch, capsys):
    import json

    from spenddat.cli import main

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SPENDDAT_BROWSER_CHANNEL", "chromium")
    monkeypatch.setenv("SPENDDAT_BROWSER_HEADLESS", "1")
    monkeypatch.setenv("SPENDDAT_BROWSER_PROFILE", str(tmp_path / "prof"))
    assert main(["browser", "inspect", store.url, "--query", "samba"]) == 0
    out = capsys.readouterr().out
    assert "search_catalog" in out and "update_cart" in out
    saved = json.loads((tmp_path / "data/browser/inspect-127.0.0.1.json").read_text())
    assert saved["sample_search"]["args"] == {"query": "samba", "limit": 10}
    assert saved["sample_search"]["result"]["products"][0]["title"] == "adidas Samba OG"


def test_web_bot_auth_signature_verifies(tmp_path):
    import base64
    import json

    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    from spenddat.browser.webbotauth import WebBotAuthSigner, init_keys, jwk_thumbprint

    key_file, dir_file, kid = init_keys(tmp_path / "wba")
    jwk = json.loads(dir_file.read_text())["keys"][0]
    assert jwk["kty"] == "OKP" and jwk["crv"] == "Ed25519" and jwk_thumbprint(jwk) == kid
    assert init_keys(tmp_path / "wba")[2] == kid  # re-running keeps the same key
    signer = WebBotAuthSigner.from_files(key_file, "https://me.example/.well-known/x")
    h = signer.headers_for("https://kicks.example/products/samba", now=1_800_000_000)
    params = h["Signature-Input"].removeprefix("sig1=")
    assert 'tag="web-bot-auth"' in params and f'keyid="{kid}"' in params
    assert "created=1800000000;expires=1800000060" in params
    base = (
        f'"@authority": kicks.example\n"signature-agent": {h["Signature-Agent"]}\n'
        f'"@signature-params": {params}'
    )
    sig = base64.b64decode(h["Signature"].removeprefix("sig1=:").removesuffix(":"))
    pub = base64.urlsafe_b64decode(jwk["x"] + "==")
    Ed25519PublicKey.from_public_bytes(pub).verify(sig, base.encode())  # raises if wrong
