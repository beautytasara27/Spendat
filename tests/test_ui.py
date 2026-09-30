"""Web UI: API behaviour, chat routing, and localhost security checks."""

from __future__ import annotations

import json
import threading
from http.server import ThreadingHTTPServer

import httpx
import pytest

from spenddat.models import RuntimeMode
from spenddat.ui.offline_llm import OfflineIntentLlm
from spenddat.ui.server import make_handler
from spenddat.ui.service import UiService
from tests.conftest import Harness, intent


@pytest.fixture
def ui(tmp_path):
    h = Harness(tmp_path)
    svc = UiService(h.orch, llm_label="scripted")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), None)  # type: ignore[arg-type]
    port = httpd.server_address[1]
    allowed = {f"127.0.0.1:{port}", f"localhost:{port}"}
    httpd.RequestHandlerClass = make_handler(svc, allowed)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    client = httpx.Client(base_url=f"http://127.0.0.1:{port}", headers={"X-Spenddat": "1"})
    yield h, client
    httpd.shutdown()
    httpd.server_close()


def chat(client, msg, cid=None):
    r = client.post("/api/chat", json={"message": msg, "conversation_id": cid})
    assert r.status_code == 200, r.text
    return r.json()


def test_index_and_assets_served(ui):
    _, c = ui
    r = c.get("/")
    assert r.status_code == 200 and "Spenddat" in r.text
    assert "script-src 'self'" in r.headers["content-security-policy"]
    assert c.get("/app.js").status_code == 200
    assert c.get("/app.css").headers["content-type"].startswith("text/css")
    assert c.get("/../pyproject.toml").status_code == 404
    assert c.get("/%2e%2e/secrets.py").status_code == 404


def test_chat_purchase_allow_returns_receipt(ui):
    h, c = ui
    h.llm.push(intent(max_price_usd=25))
    out = chat(c, "buy a clear phone case for my iphone 15 under $25")
    kinds = [b["type"] for b in out["blocks"]]
    assert kinds == ["text", "receipt"]
    assert out["blocks"][1]["basis"] == "policy_allow"
    assert out["conversation_id"].startswith("conv_")


def test_chat_escalation_then_approve_via_api(ui):
    h, c = ui
    h.llm.push(intent(product_description="noise cancelling wireless headphones"))
    out = chat(c, "buy headphones")
    appr = next(b for b in out["blocks"] if b["type"] == "approval")
    assert appr["item"]["total_minor"] == 9498 and appr["reasons"]
    assert c.get("/api/summary").json()["pending_approvals"] == 1
    r = c.post(f"/api/approvals/{appr['approval_id']}", json={"action": "approve"})
    blocks = r.json()["blocks"]
    assert any(b["type"] == "receipt" and b["basis"] == "human_approval" for b in blocks)
    assert h.merchant.orders_created == 1
    again = c.post(f"/api/approvals/{appr['approval_id']}", json={"action": "approve"})
    assert again.json()["blocks"][0]["tone"] == "warn"
    assert h.merchant.orders_created == 1


def test_chat_denied_and_clarification_followup(ui):
    h, c = ui
    h.llm.push(intent(category="gift_cards", product_description="gift card"))
    assert chat(c, "buy a gift card")["blocks"][0]["type"] == "denied"
    h.llm.push({"status": "clarify", "clarifying_question": "Which item?"})
    first = chat(c, "buy something", "conv_x")
    assert first["blocks"][0]["tone"] == "ask"
    h.llm.push(intent())
    chat(c, "a clear phone case", "conv_x")
    assert "buy something. a clear phone case" in h.llm.prompts[-1]


def test_quick_answers_do_not_touch_the_pipeline(ui):
    h, c = ui
    for msg, kind in [
        ("how much have I spent?", "spend"),
        ("what are my limits", "policy"),
        ("show recent orders", "list"),
    ]:
        blocks = chat(c, msg)["blocks"]
        assert blocks[-1]["type"] == kind
    assert chat(c, "what's pending?")["blocks"][0]["text"].startswith("Nothing")
    assert h.llm.prompts == []  # no LLM / purchase calls were made


def test_transactions_and_detail(ui):
    h, c = ui
    h.buy()
    txns = c.get("/api/transactions?mode=current").json()
    assert txns[0]["state"] == "completed" and txns[0]["autonomous"] is True
    d = c.get(f"/api/transactions/{txns[0]['txn_id']}").json()
    assert d["events"][0]["type"] == "request_received"
    assert c.get("/api/transactions/txn_nope").status_code == 404
    s = c.get("/api/summary").json()
    assert s["mode"] == "sandbox" and s["audit"]["ok"] and s["spend"]["day_minor"] == 2498


def test_post_requires_custom_header(ui):
    _, c = ui
    r = httpx.post(f"{c.base_url}/api/chat", json={"message": "hi"})
    assert r.status_code == 403


def test_cross_origin_post_refused(ui):
    _, c = ui
    r = c.post("/api/chat", json={"message": "hi"}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403


def test_dns_rebinding_host_refused(ui):
    _, c = ui
    assert c.get("/api/summary", headers={"Host": "evil.example"}).status_code == 403


def test_bad_approval_action_rejected(ui):
    _, c = ui
    r = c.post("/api/approvals/apr_x", json={"action": "delete"})
    assert r.status_code == 400
    r = c.post("/api/approvals/apr_x", json={"action": "approve", "max_total_minor": -5})
    assert r.status_code == 400


def test_offline_parser_is_sandbox_only():
    with pytest.raises(RuntimeError):
        OfflineIntentLlm(RuntimeMode.LIVE, [])


def test_offline_parser_extracts_intent():
    o = OfflineIntentLlm(RuntimeMode.SANDBOX, ["acp_merchant", "stub_vendor"])
    out = o.extract(
        "", "<request>\nbuy 3 led desk lamps from acp merchant under $50\n</request>", {}
    )
    assert out["quantity"] == 3 and out["vendor"] == "acp_merchant"
    assert out["category"] == "office_supplies" and out["max_price_usd"] == 50
    assert (
        "iphone 15"
        in o.extract(
            "", "<request>\nbuy a clear phone case for my iPhone 15 under $25\n</request>", {}
        )["product_description"]
    )
    assert o.extract("", "<request>\nbuy something nice\n</request>", {})["status"] == "clarify"


def test_demo_refuses_live(monkeypatch, tmp_path):
    from spenddat.ui.demo import build_demo

    monkeypatch.setenv("SPENDDAT_MODE", "live")
    with pytest.raises(RuntimeError, match="sandbox"):
        build_demo(tmp_path / "demo", "policies/default.yaml")


@pytest.fixture
def demo(monkeypatch, tmp_path):
    from spenddat.ui.demo import build_demo
    from tests.conftest import POLICY_FILE

    for k in (
        "SPENDDAT_SANDBOX_ANTHROPIC_API_KEY",
        "SPENDDAT_SANDBOX_OPENROUTER_API_KEY",
        "SPENDDAT_LLM_PROVIDER",
    ):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("SPENDDAT_MODE", "sandbox")
    app = build_demo(tmp_path / "demo", str(POLICY_FILE))
    return app, UiService(
        app.orch, llm_label=app.label, config=app.config, rebuild=app.rebuild, demo=True
    )


def test_demo_seeds_history(demo):
    app, svc = demo
    states = {t["state"] for t in svc.transactions(None)}
    assert {"completed", "denied", "pending_approval", "approval_denied"} <= states
    assert svc.summary()["audit"]["ok"]
    assert json.dumps(svc.summary())  # serialisable
    assert app.label == "offline demo parser"


def test_demo_policy_is_a_copy(demo):
    from tests.conftest import POLICY_FILE

    app, _ = demo
    assert app.config.policy_file != POLICY_FILE
    assert app.config.policy_file.read_text() == POLICY_FILE.read_text()


def blocks_of(svc, text, cid="c"):
    return svc.chat(cid, text)["blocks"]


def test_samba_request_end_to_end(demo):
    """The exact request that used to fail: sizes, colours, multi-store search."""
    _, svc = demo
    b = blocks_of(svc, "buy adidas sambas if they are under $100 size 8.5 in white")
    appr = next(x for x in b if x["type"] == "approval")
    item = appr["item"]
    assert item["vendor"] == "sneaker_street"
    assert item["sku"] == "SAMBA-OG-WHT-8_5"
    assert "White" in item["title"] and "8.5" in item["title"]
    assert item["unit_price_minor"] == 10000
    # $104.99 with shipping is over the $50 per-purchase and footwear caps -> asks you
    assert any("cap" in r for r in appr["reasons"])


def test_unknown_store_gets_a_clear_answer(demo):
    _, svc = demo
    b = blocks_of(svc, "buy adidas sambas size 8.5 in white from adidas shop")
    assert "not connected to a store called 'adidas shop'" in b[0]["text"]
    assert "Sneaker Street" in b[0]["text"] and "Stores page" in b[0]["text"]


def test_variant_questions(demo):
    _, svc = demo
    q = blocks_of(svc, "buy adidas sambas in white", "v1")[0]
    assert q["tone"] == "ask" and "What size" in q["text"] and "8.5" in q["text"]
    # answering the follow-up completes the request (conversation memory)
    follow = blocks_of(svc, "8.5", "v1")
    assert any(x["type"] == "approval" for x in follow)
    sold_out = blocks_of(svc, "buy adidas sambas in green size 8.5", "v2")[0]
    assert "isn't available in green in size 8.5" in sold_out["text"]
    assert "In green it comes in sizes 6, 6.5, 7, 7.5, 8, 9" in sold_out["text"]
    assert "size 8.5 comes in Black, White" in sold_out["text"]
    colour = blocks_of(svc, "buy nike air force 1 size 9", "v3")[0]
    assert "Which colour" in colour["text"]


def test_unsupported_item_is_explained_not_questioned(demo):
    _, svc = demo
    b = blocks_of(svc, "buy a garden hose")
    assert b[0]["tone"] == "warn" and "category" in b[0]["text"]


def test_any_store_search_picks_across_stores(demo):
    _, svc = demo
    b = blocks_of(svc, "buy a felt desk mat")  # only Desk & Co sells it
    appr = next(x for x in b if x["type"] == "approval")
    assert appr["item"]["vendor"] == "stub_vendor"
    assert any("vendor_not_preapproved" in r for r in appr["reasons"])


def test_store_trust_changes_decisions_and_is_audited(demo):
    app, svc = demo
    svc.change_store("update", "stub_vendor", {"trust": "auto"})
    assert "stub_vendor" in app.config.policy_file.read_text()
    b = blocks_of(svc, "buy a gel pen pack from desk & co")
    assert any(x["type"] == "receipt" and x["basis"] == "policy_allow" for x in b)
    svc.change_store("update", "stub_vendor", {"trust": "blocked"})
    b = blocks_of(svc, "buy a gel pen pack from desk & co")
    assert b[0]["type"] == "denied"
    ev = [e for e in svc.orch.store.events("config") if e.event_type == "stores_changed"]
    assert len(ev) == 2 and ev[-1].payload["after"]["trust"] == "blocked"
    assert ev[-1].payload["policy_version_before"] != ev[-1].payload["policy_version_after"]
    assert svc.orch.store.verify_chain()


def test_disable_add_and_remove_store(demo):
    _, svc = demo
    svc.change_store("update", "sneaker_street", {"enabled": False})
    b = blocks_of(svc, "buy adidas sambas size 8.5 in white")
    assert "couldn't find" in b[0]["text"]
    svc.change_store("update", "sneaker_street", {"enabled": True})
    out = svc.change_store(
        "add",
        None,
        {"name": "Kicks Two", "type": "reference", "catalog": "sneakers", "trust": "auto"},
    )
    assert out["store"]["id"] == "kicks_two" and out["store"]["trust"] == "auto"
    listing = svc.stores(check=True)["stores"]
    kicks = next(s for s in listing if s["id"] == "kicks_two")
    assert kicks["loaded"] and kicks["health"]["ok"] and kicks["health"]["products"] > 50
    svc.change_store("remove", "kicks_two", {})
    assert all(s["id"] != "kicks_two" for s in svc.stores()["stores"])


def test_bad_store_change_is_rolled_back(demo):
    from spenddat.stores import StoreConfigError

    app, svc = demo
    before = app.config.snapshot()
    with pytest.raises(StoreConfigError):
        svc.change_store("add", None, {"name": "Real Shop", "type": "acp", "base_url": "not a url"})
    with pytest.raises(StoreConfigError, match="change not applied"):
        svc.change_store("add", None, {"name": "Broken", "type": "reference", "catalog": "nope"})
    assert app.config.snapshot() == before


def test_store_api_over_http(ui):
    _, c = ui
    r = c.get("/api/stores")
    assert r.status_code == 200 and r.json()["editable"] is False
    r = c.post("/api/stores/sneaker_street", json={"trust": "auto"})
    assert r.status_code == 400
