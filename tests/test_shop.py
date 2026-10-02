"""Your own shop: init, live-mode purchases against it, declines, refunds, stripe-check.

Stripe is simulated with httpx.MockTransport (no network, no money). The flow is wired
exactly as in real use: Spenddat (LIVE mode) issues a shared payment token with the agent
key, the shop charges it with the shop key, and refunds go through the shop key.
"""

from __future__ import annotations

import shutil
from datetime import timedelta
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest

from spenddat.approval import ApprovalService, RecordingNotifier
from spenddat.audit import AuditStore
from spenddat.config import Settings, startup_checks
from spenddat.guards import HardLimits
from spenddat.intent import IntentParser, ScriptedLlm
from spenddat.models import ApprovalAction, RuntimeMode, TxnState
from spenddat.orchestrator import Orchestrator
from spenddat.payments import StripeSharedPaymentTokens
from spenddat.policy import PolicyEngine, load_policy
from spenddat.secrets import EnvSecrets
from spenddat.shop import ShopError, build_merchant, init_shop, load_shop, refund_orders
from spenddat.stores import StoreConfig
from spenddat.stripe_check import run_check
from spenddat.vendors.acp import AcpAdapter
from spenddat.vendors.base import AdapterRegistry
from tests.conftest import POLICY_FILE, ROOT, SHIP_TO

LIVE_PROFILE = "profile_61LiveShop"
TEST_PROFILE = "profile_test_61TestShop"


class FakeStripe:
    """Just enough of Stripe: accounts, payment methods, SPTs, payment intents, refunds."""

    def __init__(self, livemode: bool = True) -> None:
        self.livemode = livemode
        self.tokens: dict[str, dict] = {}
        self.intents: dict[str, dict] = {}
        self.refunds: list[str] = []
        self.next_charge = "succeeded"  # or "declined" / "requires_action"
        self.token_status = "active"
        self.charged_total = 0

    def handler(self, req: httpx.Request) -> httpx.Response:
        path = req.url.path
        form = {k: v[0] for k, v in parse_qs(req.content.decode()).items()} if req.content else {}
        key = req.headers.get("authorization", "")
        if path == "/v1/account":
            acct = "acct_agent" if "agent" in _basic_user(key) else "acct_shop"
            return httpx.Response(200, json={"id": acct, "charges_enabled": True})
        if path == "/v1/customers":
            return httpx.Response(200, json={"data": [{"id": "cus_1", "name": "Me"}]})
        if path == "/v1/customers/cus_1/payment_methods":
            return httpx.Response(
                200,
                json={"data": [{"id": "pm_found1", "card": {"brand": "visa", "last4": "1111"}}]},
            )
        if path.startswith("/v1/payment_methods/"):
            return httpx.Response(
                200, json={"id": path.rsplit("/", 1)[1], "card": {"brand": "visa", "last4": "4242"}}
            )
        if path == "/v1/shared_payment/issued_tokens":
            tid = f"spt_{len(self.tokens) + 1:06d}abc"
            self.tokens[tid] = {
                "seller": form["seller_details[network_business_profile]"],
                "max": int(form["usage_limits[max_amount]"]),
                "status": self.token_status,
            }
            return httpx.Response(
                200,
                json={
                    "id": tid,
                    "livemode": self.livemode,
                    "status": self.token_status,
                    "usage_limits": {
                        "currency": form["usage_limits[currency]"],
                        "max_amount": int(form["usage_limits[max_amount]"]),
                    },
                },
            )
        if path.endswith("/revoke"):
            self.tokens[path.split("/")[-2]]["status"] = "deactivated"
            return httpx.Response(200, json={"status": "deactivated"})
        if path == "/v1/payment_intents":
            tok = self.tokens[form["payment_method_data[shared_payment_granted_token]"]]
            amount = int(form["amount"])
            if tok["status"] != "active" or amount > tok["max"]:
                return httpx.Response(
                    402,
                    json={
                        "error": {"type": "invalid_request_error", "message": "token not usable"}
                    },
                )
            if self.next_charge == "declined":
                return httpx.Response(
                    402,
                    json={"error": {"type": "card_error", "message": "Your card was declined."}},
                )
            pid = f"pi_{len(self.intents) + 1:04d}"
            status = "requires_action" if self.next_charge == "requires_action" else "succeeded"
            self.intents[pid] = {"amount": amount, "status": status}
            if status == "succeeded":
                self.charged_total += amount
                tok["status"] = "deactivated"
            return httpx.Response(200, json={"id": pid, "status": status})
        if path.endswith("/cancel") and "/payment_intents/" in path:
            self.intents[path.split("/")[-2]]["status"] = "canceled"
            return httpx.Response(200, json={"status": "canceled"})
        if path == "/v1/refunds":
            pi = form["payment_intent"]
            self.refunds.append(pi)
            self.charged_total -= self.intents[pi]["amount"]
            return httpx.Response(
                200, json={"amount": self.intents[pi]["amount"], "status": "succeeded"}
            )
        return httpx.Response(404, json={"error": {"message": path}})


def _basic_user(header: str) -> str:
    import base64

    try:
        return base64.b64decode(header.split()[1]).decode()
    except Exception:
        return ""


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A copy of the repo config in a temp dir, with `spenddat shop init` already run."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config").mkdir()
    (tmp_path / "policies").mkdir()
    shutil.copy(ROOT / "config" / "vendors.yaml", tmp_path / "config" / "vendors.yaml")
    shutil.copy(POLICY_FILE, tmp_path / "policies" / "default.yaml")
    shutil.copy(ROOT / "config" / "profile.example.yaml", tmp_path / "config" / "profile.yaml")
    (tmp_path / ".env").write_text("SPENDDAT_MODE=sandbox\n")
    done = init_shop(
        shop_file=tmp_path / "config" / "shop.yaml",
        env_file=tmp_path / ".env",
        vendors_file=tmp_path / "config" / "vendors.yaml",
        policy_file=tmp_path / "policies" / "default.yaml",
        test_profile=TEST_PROFILE,
        live_profile=LIVE_PROFILE,
    )
    return tmp_path, done


def env_from(path: Path) -> dict[str, str]:
    out = {}
    for line in path.read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            k, _, v = line.partition("=")
            out[k] = v
    return out


def test_init_writes_everything_once(project):
    root, done = project
    assert any("created" in d for d in done) and any("Auto-buy" in d for d in done)
    env = env_from(root / ".env")
    key = env["SPENDDAT_SHOP_AGENT_KEY"]
    assert len(key) > 20
    assert env["SPENDDAT_LIVE_MY_SHOP_API_KEY"] == key == env["SPENDDAT_SANDBOX_MY_SHOP_API_KEY"]
    cfg = StoreConfig(
        root / "config/vendors.yaml", root / "policies/default.yaml", RuntimeMode.LIVE
    )
    shop = cfg.get("my_shop")
    assert shop["trust"] == "auto" and shop["base_url"] == "http://127.0.0.1:8788"
    assert cfg.get("acp_merchant")["enabled"] is False  # placeholder example store off
    pol = load_policy(root / "policies/default.yaml")
    assert "digital_goods" in pol.categories.allow
    assert pol.caps.per_category_minor["digital_goods"] == 100
    # running again changes nothing and keeps the same key
    again = init_shop(
        shop_file=root / "config/shop.yaml",
        env_file=root / ".env",
        vendors_file=root / "config/vendors.yaml",
        policy_file=root / "policies/default.yaml",
        test_profile=None,
        live_profile=None,
    )
    assert all("profile" not in d and "key" not in d for d in again)
    assert env_from(root / ".env")["SPENDDAT_SHOP_AGENT_KEY"] == key
    assert "# Vendor integrations" in (root / "config/vendors.yaml").read_text()


def live_env(root: Path) -> dict[str, str]:
    env = env_from(root / ".env")
    env.update(
        {
            "SPENDDAT_MODE": "live",
            "SPENDDAT_LIVE_CONFIRM": "I_ACCEPT_REAL_PURCHASES",
            "SPENDDAT_LIVE_STRIPE_SECRET_KEY": "sk_live_agent_abc123",
            "SPENDDAT_LIVE_STRIPE_PAYMENT_METHOD_ID": "pm_realcard123",
            "SPENDDAT_LIVE_OPENROUTER_API_KEY": "sk-or-x",
            "SPENDDAT_SHOP_STRIPE_SECRET_KEY": "sk_live_shop_abc123",
        }
    )
    return env


def test_live_startup_checks_pass_with_local_shop(project):
    root, _ = project
    env = live_env(root)
    s = Settings.from_env(env)
    startup_checks(s, EnvSecrets(RuntimeMode.LIVE, env), env)


def test_live_checks_still_reject_remote_http(project):
    from spenddat.config import StartupError

    root, _ = project
    v = root / "config/vendors.yaml"
    v.write_text(v.read_text().replace("http://127.0.0.1:8788", "http://shop.example.com"))
    env = live_env(root)
    with pytest.raises(StartupError, match="must be https"):
        startup_checks(Settings.from_env(env), EnvSecrets(RuntimeMode.LIVE, env), env)


class LiveRig:
    def __init__(self, root: Path) -> None:
        self.env = live_env(root)
        self.stripe = FakeStripe(livemode=True)
        self.http = httpx.Client(transport=httpx.MockTransport(self.stripe.handler))
        self.shop = load_shop(root / "config/shop.yaml", self.env)
        assert self.shop.live
        self.root = root
        self.merchant = self._merchant()
        self.llm = ScriptedLlm()
        policy = load_policy(root / "policies/default.yaml")
        reg = AdapterRegistry()
        reg.register(
            AcpAdapter(
                vendor_id="my_shop",
                base_url=self.shop.base_url,
                feed_url=f"{self.shop.base_url}/feed.json",
                api_key=self.env["SPENDDAT_LIVE_MY_SHOP_API_KEY"],
                seller_profile=LIVE_PROFILE,
                client=httpx.Client(transport=self.merchant.mock_transport(self.shop.base_url)),
            )
        )
        store = AuditStore(root / "audit.db")
        self.notifier = RecordingNotifier()
        self.orch = Orchestrator(
            mode=RuntimeMode.LIVE,
            store=store,
            parser=IntentParser(
                self.llm,
                supported_vendors={"my_shop"},
                autonomous_vendors=policy.vendors.allow,
                supported_categories=policy.categories.allow | policy.categories.deny,
            ),
            engine=PolicyEngine(policy),
            approvals=ApprovalService(store, [self.notifier], timedelta(minutes=60)),
            registry=reg,
            payments=StripeSharedPaymentTokens(
                RuntimeMode.LIVE,
                self.env["SPENDDAT_LIVE_STRIPE_SECRET_KEY"],
                self.env["SPENDDAT_LIVE_STRIPE_PAYMENT_METHOD_ID"],
                client=self.http,
            ),
            hard_limits=HardLimits(20000, 30000, 75000),
            ship_to=SHIP_TO,
        )

    def _merchant(self):
        m = build_merchant(self.shop, self.root / "data/shop")
        from spenddat.sandbox.reference_merchant import stripe_charger

        m.charger = stripe_charger(self.shop.stripe_key, self.http, allow_live=True)
        return m

    def buy(self, desc: str):
        self.llm.push(
            {
                "status": "ok",
                "vendor": "any_allowed_vendor",
                "product_description": desc,
                "category": "digital_goods",
                "quantity": 1,
                "max_price_usd": None,
            }
        )
        return self.orch.submit(f"buy a {desc}")


@pytest.fixture
def rig(project):
    return LiveRig(project[0])


def test_live_demo_1_autonomous_one_dollar_purchase(rig):
    res = rig.buy("spenddat thank-you note")
    assert res.state is TxnState.COMPLETED, res.message
    assert res.approval_id is None and rig.notifier.requests == []
    assert res.receipt.amount_minor == 100
    assert rig.stripe.charged_total == 100
    (tok,) = rig.stripe.tokens.values()
    assert tok["seller"] == LIVE_PROFILE and tok["max"] == 100
    assert tok["status"] == "deactivated"
    assert {e.mode for e in rig.orch.store.events(res.txn_id)} == {"live"}


def test_live_demo_2_two_dollars_needs_approval(rig):
    res = rig.buy("spenddat supporter pass")
    assert res.state is TxnState.PENDING_APPROVAL
    assert res.decision.reason_codes == ("category_cap_exceeded",)
    assert rig.stripe.charged_total == 0
    done = rig.orch.decide_approval(res.approval_id, ApprovalAction.APPROVE, "me")
    assert done.state is TxnState.COMPLETED and rig.stripe.charged_total == 200


def test_orders_survive_restart_and_refund_all(rig):
    rig.buy("spenddat thank-you note")
    res = rig.buy("spenddat supporter pass")
    rig.orch.decide_approval(res.approval_id, ApprovalAction.APPROVE, "me")
    reopened = rig._merchant()  # a fresh shop process reading the saved orders
    assert len(reopened.orders()) == 2
    out = refund_orders(rig.shop, rig.root / "data/shop", None, client=rig.http)
    assert [r for _, r in out] == ["refunded $1.00 (succeeded)", "refunded $2.00 (succeeded)"]
    assert rig.stripe.charged_total == 0


def test_declined_card_means_failed_and_nothing_charged(rig):
    rig.stripe.next_charge = "declined"
    res = rig.buy("spenddat thank-you note")
    assert res.state is TxnState.FAILED and "declined" in res.message
    assert rig.stripe.charged_total == 0
    assert all(t["status"] == "deactivated" for t in rig.stripe.tokens.values())  # revoked


def test_three_d_secure_at_charge_is_cancelled_cleanly(rig):
    rig.stripe.next_charge = "requires_action"
    res = rig.buy("spenddat thank-you note")
    assert res.state is TxnState.FAILED and "3-D Secure" in res.message
    assert rig.stripe.charged_total == 0
    assert all(i["status"] == "canceled" for i in rig.stripe.intents.values())


def test_three_d_secure_at_token_issue_is_refused(rig):
    rig.stripe.token_status = "requires_action"
    res = rig.buy("spenddat thank-you note")
    assert res.state is TxnState.FAILED and "3-D Secure" in res.message
    assert rig.stripe.intents == {}


def test_shop_refuses_wrong_agent_key(rig):
    r = rig.merchant.handle("POST", "/checkout_sessions", {"authorization": "Bearer nope"}, {})
    assert r[0] == 401


def test_load_shop_validation(project):
    root, _ = project
    with pytest.raises(ShopError, match="SPENDDAT_SHOP_STRIPE_SECRET_KEY"):
        load_shop(root / "config/shop.yaml", {"SPENDDAT_SHOP_AGENT_KEY": "x" * 30})
    y = root / "config/shop.yaml"
    y.write_text(y.read_text().replace("price_minor: 100", "price_minor: 20"))
    with pytest.raises(ShopError, match="minimum charge"):
        load_shop(y, live_env(root))


def test_stripe_check_passes_and_moves_no_money(project):
    root, _ = project
    env = live_env(root)
    fake = FakeStripe(livemode=True)
    rep = run_check(
        RuntimeMode.LIVE,
        EnvSecrets(RuntimeMode.LIVE, env),
        env,
        root / "config/vendors.yaml",
        client=httpx.Client(transport=httpx.MockTransport(fake.handler)),
    )
    assert rep.ok, rep.render()
    assert fake.charged_total == 0 and fake.intents == {}
    (tok,) = fake.tokens.values()
    assert tok["max"] == 50 and tok["status"] == "deactivated"


def test_stripe_check_reports_missing_pieces(project):
    root, _ = project
    env = {"SPENDDAT_MODE": "live"}
    rep = run_check(
        RuntimeMode.LIVE,
        EnvSecrets(RuntimeMode.LIVE, env),
        env,
        root / "config/vendors.yaml",
        client=httpx.Client(transport=httpx.MockTransport(FakeStripe().handler)),
    )
    text = rep.render()
    assert not rep.ok
    for needle in ("Agent Stripe key", "Card on file", "Shop Stripe key"):
        assert needle in text


def test_stripe_check_flags_wrong_mode_profile(project):
    root, _ = project
    init_shop(
        shop_file=root / "config/shop.yaml",
        env_file=root / ".env",
        vendors_file=root / "config/vendors.yaml",
        policy_file=root / "policies/default.yaml",
        test_profile=None,
        live_profile="profile_test_oops",
    )
    env = live_env(root)
    rep = run_check(
        RuntimeMode.LIVE,
        EnvSecrets(RuntimeMode.LIVE, env),
        env,
        root / "config/vendors.yaml",
        client=httpx.Client(transport=httpx.MockTransport(FakeStripe().handler)),
    )
    assert "not a live-mode profile" in rep.render()


def test_stripe_check_lists_saved_cards_when_pm_missing(project):
    root, _ = project
    env = live_env(root)
    del env["SPENDDAT_LIVE_STRIPE_PAYMENT_METHOD_ID"]
    rep = run_check(
        RuntimeMode.LIVE,
        EnvSecrets(RuntimeMode.LIVE, env),
        env,
        root / "config/vendors.yaml",
        client=httpx.Client(transport=httpx.MockTransport(FakeStripe().handler)),
    )
    assert "pm_found1  visa ending 1111  (Me)" in rep.render()


def _check(root, env):
    return run_check(
        RuntimeMode.LIVE,
        EnvSecrets(RuntimeMode.LIVE, env),
        env,
        root / "config/vendors.yaml",
        client=httpx.Client(transport=httpx.MockTransport(FakeStripe().handler)),
    )


def test_stripe_check_confirms_two_separate_accounts(project):
    root, _ = project
    rep = _check(root, live_env(root))
    assert "Agent (acct_agent) and shop (acct_shop) are separate Stripe accounts" in rep.render()


def test_stripe_check_fails_when_shop_uses_agent_key(project):
    root, _ = project
    env = live_env(root)
    env["SPENDDAT_SHOP_STRIPE_SECRET_KEY"] = env["SPENDDAT_LIVE_STRIPE_SECRET_KEY"]
    rep = _check(root, env)
    assert not rep.ok and "same key" in rep.render()


def test_stripe_check_fails_when_both_keys_are_one_account(project):
    root, _ = project
    env = live_env(root)
    env["SPENDDAT_SHOP_STRIPE_SECRET_KEY"] = "sk_live_agent_second_key"  # same account
    rep = _check(root, env)
    assert not rep.ok and "same Stripe account (acct_agent)" in rep.render()
    assert "Issued a 50-cent" not in rep.render()  # no token issued when setup is wrong


def test_shop_refuses_to_run_with_the_agent_key(project):
    root, _ = project
    env = live_env(root)
    env["SPENDDAT_SHOP_STRIPE_SECRET_KEY"] = env["SPENDDAT_LIVE_STRIPE_SECRET_KEY"]
    with pytest.raises(ShopError, match="agent's key"):
        load_shop(root / "config/shop.yaml", env)
