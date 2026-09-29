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
        build_demo(str(tmp_path / "d.db"), "policies/default.yaml")


def test_demo_seeds_history(monkeypatch, tmp_path):
    from spenddat.ui.demo import build_demo
    from tests.conftest import POLICY_FILE

    monkeypatch.delenv("SPENDDAT_SANDBOX_ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("SPENDDAT_MODE", "sandbox")
    orch, label = build_demo(str(tmp_path / "d.db"), str(POLICY_FILE))
    svc = UiService(orch, llm_label=label)
    states = {t["state"] for t in svc.transactions(None)}
    assert {"completed", "denied", "pending_approval", "approval_denied"} <= states
    assert svc.summary()["audit"]["ok"]
    assert json.dumps(svc.summary())  # serialisable
