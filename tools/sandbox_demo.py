"""Offline end-to-end sandbox demo: both PRD flows over real HTTP, no credentials needed.

Starts the reference ACP merchant on a local port, then runs:
  1. an in-policy request  -> ALLOW    -> autonomous purchase -> audit
  2. an over-cap request   -> ESCALATE -> human approval      -> purchase -> audit

Uses the fake payment provider and a scripted intent parser unless you export
SPENDDAT_SANDBOX_ANTHROPIC_API_KEY (real LLM parsing) and Stripe test credentials
(SPENDDAT_PAYMENT_BACKEND=stripe + SPENDDAT_SANDBOX_STRIPE_*). Never touches live mode.

    python tools/sandbox_demo.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from spenddat.config import build_orchestrator  # noqa: E402
from spenddat.intent import ScriptedLlm  # noqa: E402
from spenddat.models import ApprovalAction  # noqa: E402
from spenddat.observability import configure_logging  # noqa: E402
from spenddat.sandbox.reference_merchant import ReferenceMerchant, serve  # noqa: E402

PORT = 8799


def main() -> int:
    if os.environ.get("SPENDDAT_MODE", "sandbox") != "sandbox":
        print("sandbox_demo only runs in sandbox mode", file=sys.stderr)
        return 2
    configure_logging("WARNING")
    threading.Thread(
        target=serve, args=(ReferenceMerchant(api_key="demo_key"), "127.0.0.1", PORT), daemon=True
    ).start()
    for _ in range(50):
        try:
            httpx.get(f"http://127.0.0.1:{PORT}/feed.json", timeout=0.2)
            break
        except httpx.HTTPError:
            time.sleep(0.05)

    tmp = Path(tempfile.mkdtemp())
    (tmp / "vendors.yaml").write_text(f"""
vendors:
  - id: acp_merchant
    type: acp
    sandbox: {{base_url: "http://127.0.0.1:{PORT}", feed_url: "http://127.0.0.1:{PORT}/feed.json",
              seller_profile: profile_test_demo, api_key_secret: acp_merchant_api_key}}
""")
    env = {
        **os.environ,
        "SPENDDAT_MODE": "sandbox",
        "SPENDDAT_DB": str(tmp / "demo.db"),
        "SPENDDAT_POLICY_FILE": str(ROOT / "policies" / "default.yaml"),
        "SPENDDAT_VENDORS_FILE": str(tmp / "vendors.yaml"),
        "SPENDDAT_PROFILE_FILE": str(ROOT / "config" / "profile.example.yaml"),
        "SPENDDAT_SANDBOX_ACP_MERCHANT_API_KEY": "demo_key",
        "SPENDDAT_PAYMENT_BACKEND": os.environ.get("SPENDDAT_PAYMENT_BACKEND", "fake"),
    }
    llm = None
    if not env.get("SPENDDAT_SANDBOX_ANTHROPIC_API_KEY"):
        env["SPENDDAT_SANDBOX_ANTHROPIC_API_KEY"] = "unused-scripted"
        llm = ScriptedLlm(
            {
                "status": "ok",
                "vendor": "acp_merchant",
                "category": "electronics_accessories",
                "product_description": "clear phone case iphone 15",
                "quantity": 1,
                "max_price_usd": 25,
            },
            {
                "status": "ok",
                "vendor": "acp_merchant",
                "category": "electronics_accessories",
                "product_description": "noise cancelling wireless headphones",
                "quantity": 1,
                "max_price_usd": None,
            },
        )
    orch = build_orchestrator(env, llm=llm) if llm else build_orchestrator(env)

    print("\n=== 1. In-policy request (expect autonomous purchase) ===")
    r1 = orch.submit("buy a clear phone case for my iPhone 15 from acp merchant, under $25")
    print(f"{r1.state.value}: {r1.message}")

    print("\n=== 2. Over-cap request (expect escalation) ===")
    r2 = orch.submit("buy noise cancelling wireless headphones from acp merchant")
    print(f"{r2.state.value}: {r2.message}")
    print("\n--- simulated reviewer approves ---")
    r3 = orch.decide_approval(r2.approval_id, ApprovalAction.APPROVE, "demo-reviewer")
    print(f"{r3.state.value}: {r3.message}")

    for r in (r1, r2):
        print(f"\n=== audit trail {r.txn_id} ===")
        for e in orch.store.events(r.txn_id):
            print(f"  #{e.seq:<3} [{e.mode}] {e.event_type}")
    print(f"\naudit chain verified: {orch.store.verify_chain()} events")
    ok = r1.state.value == "completed" and r3.state.value == "completed"
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
