"""Command-line interface: submit requests, review approvals, inspect the audit trail."""

from __future__ import annotations

import argparse
import getpass
import json
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation

from spenddat.approval import ApprovalError, ApprovalRequest, render_request
from spenddat.audit import AuditStore, ChainError
from spenddat.config import Settings, StartupError, build_orchestrator, startup_checks
from spenddat.models import ApprovalAction, Modification, RuntimeMode, TxnState
from spenddat.observability import configure_logging
from spenddat.policy import load_policy
from spenddat.secrets import build_secrets


def _dollars_to_minor(s: str) -> int:
    try:
        d = Decimal(s.lstrip("$"))
    except InvalidOperation as e:
        raise argparse.ArgumentTypeError(f"not an amount: {s}") from e
    if d <= 0:
        raise argparse.ArgumentTypeError("amount must be positive")
    return int((d * 100).to_integral_value())


def _fmt(minor: int | None) -> str:
    return "-" if minor is None else f"${minor / 100:,.2f}"


def _store(settings: Settings) -> AuditStore:
    return AuditStore(settings.db_path)


def _mode_filter(value: str) -> RuntimeMode | None:
    return None if value == "all" else RuntimeMode(value)


def cmd_buy(args: argparse.Namespace) -> int:
    orch = build_orchestrator()
    res = orch.submit(args.request, client_request_id=args.request_id)
    print(json.dumps(res.model_dump(mode="json"), indent=2))
    return 0 if res.state not in (TxnState.FAILED,) else 1


def _decide(args: argparse.Namespace, action: ApprovalAction) -> int:
    orch = build_orchestrator()
    mod = None
    if action is ApprovalAction.APPROVE and (args.max_total or args.quantity):
        action = ApprovalAction.APPROVE_WITH_MODIFICATION
        mod = Modification(max_total_minor=args.max_total, quantity=args.quantity)
    who = args.as_ or getpass.getuser()
    try:
        res = orch.decide_approval(args.approval_id, action, who, mod)
    except ApprovalError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    print(json.dumps(res.model_dump(mode="json"), indent=2))
    return 0


def cmd_approve(args: argparse.Namespace) -> int:
    return _decide(args, ApprovalAction.APPROVE)


def cmd_deny(args: argparse.Namespace) -> int:
    return _decide(args, ApprovalAction.DENY)


def cmd_review(args: argparse.Namespace) -> int:
    """Interactive CLI approval channel."""
    orch = build_orchestrator()
    orch.sweep_expired()
    pending = orch.store.pending_approvals(orch.mode)
    if not pending:
        print("No pending approvals.")
        return 0
    who = args.as_ or getpass.getuser()
    for row in pending:
        req = ApprovalRequest(
            row["approval_id"],
            row["txn_id"],
            RuntimeMode(row["mode"]),
            datetime.fromisoformat(row["expires_at"]),
            json.loads(row["presented"]),
        )
        print("\n" + render_request(req))
        choice = input("[a]pprove / approve with [l]ower ceiling / [d]eny / [s]kip? ").strip()
        try:
            if choice == "a":
                res = orch.decide_approval(row["approval_id"], ApprovalAction.APPROVE, who)
            elif choice == "l":
                ceiling = _dollars_to_minor(input("max total in dollars: ").strip())
                res = orch.decide_approval(
                    row["approval_id"],
                    ApprovalAction.APPROVE_WITH_MODIFICATION,
                    who,
                    Modification(max_total_minor=ceiling),
                )
            elif choice == "d":
                res = orch.decide_approval(row["approval_id"], ApprovalAction.DENY, who)
            else:
                continue
            print(f"-> {res.state.value}: {res.message}")
        except (ApprovalError, argparse.ArgumentTypeError) as e:
            print(f"error: {e}")
    return 0


def cmd_approvals(args: argparse.Namespace) -> int:
    settings = Settings.from_env()
    store = _store(settings)
    rows = store.pending_approvals(_mode_filter(args.mode))
    if not rows:
        print("No pending approvals.")
    for r in rows:
        p = json.loads(r["presented"])
        print(
            f"{r['approval_id']}  [{r['mode']}]  {p['item']['title']}  "
            f"{_fmt(p['item']['total_minor'])}  expires {r['expires_at']}  "
            f"reasons: {', '.join(p['reason_codes'])}"
        )
    return 0


def cmd_history(args: argparse.Namespace) -> int:
    settings = Settings.from_env()
    rows = _store(settings).list_txns(_mode_filter(args.mode), args.limit)
    print(f"{'txn_id':26} {'mode':8} {'state':26} {'vendor':14} {'amount':>10}  created")
    for r in rows:
        print(
            f"{r['txn_id']:26} {r['mode']:8} {r['state']:26} {(r['vendor'] or '-'):14} "
            f"{_fmt(r['amount_minor']):>10}  {r['created_at'][:19]}"
        )
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    store = _store(Settings.from_env())
    events = store.events(args.txn_id)
    if not events:
        print(f"no audit events for {args.txn_id}", file=sys.stderr)
        return 1
    for e in events:
        print(f"#{e.seq:<5} {e.ts[:23]}  [{e.mode}]  {e.event_type}")
        print("        " + json.dumps(e.payload, default=str)[:2000])
    return 0


def cmd_spend(args: argparse.Namespace) -> int:
    settings = Settings.from_env()
    store = _store(settings)
    policy = load_policy(settings.policy_file)
    now = datetime.now(UTC)
    modes = [RuntimeMode(args.mode)] if args.mode != "all" else list(RuntimeMode)
    for m in modes:
        day = store.spend_since(m, now - timedelta(days=1))
        week = store.spend_since(m, now - timedelta(days=7))
        hl = settings.hard_limits
        print(
            f"[{m.value}] last 24h: {_fmt(day)} / policy {_fmt(policy.caps.daily_minor)} "
            f"/ hard {_fmt(hl.max_daily_minor)}"
        )
        print(
            f"[{m.value}] last 7d:  {_fmt(week)} / policy {_fmt(policy.caps.weekly_minor)} "
            f"/ hard {_fmt(hl.max_weekly_minor)}"
        )
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    try:
        n = _store(Settings.from_env()).verify_chain()
    except ChainError as e:
        print(f"AUDIT LOG TAMPERED: {e}", file=sys.stderr)
        return 3
    print(f"audit chain OK ({n} events)")
    return 0


def cmd_sweep(args: argparse.Namespace) -> int:
    for r in build_orchestrator().sweep_expired():
        print(f"{r.txn_id}: {r.message}")
    return 0


def cmd_reconcile(args: argparse.Namespace) -> int:
    for r in build_orchestrator().reconcile():
        print(f"{r.txn_id}: {r.state.value} - {r.message}")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    import os

    settings = Settings.from_env()
    startup_checks(settings, build_secrets(settings.mode), dict(os.environ))
    print(f"configuration OK for {settings.mode.value.upper()} mode")
    return 0


def cmd_sandbox_merchant(args: argparse.Namespace) -> int:
    import os

    from spenddat.sandbox.reference_merchant import ReferenceMerchant, serve, stripe_test_charger

    key = os.environ.get("SPENDDAT_SANDBOX_ACP_MERCHANT_API_KEY", "merchant_test_key")
    seller_key = os.environ.get("REFERENCE_MERCHANT_STRIPE_TEST_KEY")
    charger = stripe_test_charger(seller_key) if seller_key else None
    print(
        f"reference ACP merchant on http://{args.host}:{args.port} "
        f"(charging via Stripe test mode: {'yes' if charger else 'no'})"
    )
    serve(ReferenceMerchant(api_key=key, charger=charger), args.host, args.port)
    return 0


def cmd_ui(args: argparse.Namespace) -> int:
    import logging
    import os

    from spenddat.stores import StoreConfig
    from spenddat.ui.server import serve
    from spenddat.ui.service import UiService

    logging.getLogger("spenddat").setLevel(logging.WARNING)
    settings = Settings.from_env()
    if args.demo:
        from spenddat.ui.demo import build_demo

        app = build_demo(
            args.data_dir, settings.policy_file, seed=not args.no_seed, reset=args.reset
        )
        service = UiService(
            app.orch, llm_label=app.label, config=app.config, rebuild=app.rebuild, demo=True
        )
    else:
        from spenddat.llm import choose_llm

        orch = build_orchestrator()
        service = UiService(
            orch,
            llm_label=choose_llm(dict(os.environ), build_secrets(orch.mode)).label,
            config=StoreConfig(settings.vendors_file, settings.policy_file, orch.mode),
            rebuild=build_orchestrator,
        )
    serve(
        service,
        args.host,
        args.port,
        open_browser=not args.no_browser,
        allow_remote=args.allow_remote,
    )
    return 0


SHOP_FILE = "config/shop.yaml"
SHOP_STATE = "data/shop"


def cmd_shop(args: argparse.Namespace) -> int:
    from pathlib import Path

    from spenddat.shop import ShopError, init_shop, load_shop, refund_orders, run_shop

    settings = Settings.from_env()
    try:
        if args.shop_cmd == "init":
            for line in init_shop(
                shop_file=Path(SHOP_FILE),
                env_file=Path(".env"),
                vendors_file=Path(settings.vendors_file),
                policy_file=Path(settings.policy_file),
                test_profile=args.test_profile,
                live_profile=args.live_profile,
            ):
                print(f"  - {line}")
            print(
                "done. Next: put the shop's Stripe key in .env as "
                "SPENDDAT_SHOP_STRIPE_SECRET_KEY, then run `spenddat stripe-check`."
            )
            return 0
        shop = load_shop(SHOP_FILE)
        if args.shop_cmd == "run":
            run_shop(shop, SHOP_STATE)
            return 0
        if args.shop_cmd == "orders":
            from spenddat.shop import build_merchant

            orders = build_merchant(shop, SHOP_STATE).orders()
            print(f"{shop.name} [{'LIVE' if shop.live else 'TEST'}] - {len(orders)} order(s)")
            for o in orders:
                items = ", ".join(f"{i['quantity']} x {i['id']}" for i in o["items"])
                print(
                    f"  {o['id']}  ${o['total'] / 100:.2f}  {o.get('created_at', '')[:19]}  "
                    f"{items}  {o.get('payment_reference')}"
                )
            return 0
        if args.shop_cmd == "refund":
            if not args.order_id and not args.all:
                print("give an order id or --all", file=sys.stderr)
                return 2
            for oid, result in refund_orders(shop, SHOP_STATE, None if args.all else args.order_id):
                print(f"  {oid}: {result}")
            return 0
    except ShopError as e:
        print(f"error: {e}", file=sys.stderr)
        return 78
    return 2


def cmd_stripe_check(args: argparse.Namespace) -> int:
    import os
    from pathlib import Path

    from spenddat.stripe_check import run_check

    settings = Settings.from_env()
    rep = run_check(
        settings.mode, build_secrets(settings.mode), dict(os.environ), Path(settings.vendors_file)
    )
    print(rep.render())
    print("")
    print("All good." if rep.ok else "Fix the FAIL items above and run this again.")
    return 0 if rep.ok else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="spenddat", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("buy", help="submit a natural-language purchase request")
    b.add_argument("request")
    b.add_argument("--request-id", help="client idempotency key (safe to retry)")
    b.set_defaults(func=cmd_buy)

    for name, fn in (("approve", cmd_approve), ("deny", cmd_deny)):
        a = sub.add_parser(name, help=f"{name} an escalated transaction")
        a.add_argument("approval_id")
        a.add_argument("--as", dest="as_", help="approver identity (default: OS user)")
        if name == "approve":
            a.add_argument(
                "--max-total",
                type=_dollars_to_minor,
                help="approve only up to this total, in dollars",
            )
            a.add_argument("--quantity", type=int, help="approve a smaller quantity")
        a.set_defaults(func=fn)

    r = sub.add_parser("review", help="interactively review pending approvals")
    r.add_argument("--as", dest="as_")
    r.set_defaults(func=cmd_review)

    for name, fn, h in (
        ("approvals", cmd_approvals, "list pending approvals"),
        ("history", cmd_history, "transaction history"),
        ("spend", cmd_spend, "cumulative spend vs caps"),
    ):
        s = sub.add_parser(name, help=h)
        s.add_argument("--mode", choices=["sandbox", "live", "all"], default="all")
        if name == "history":
            s.add_argument("--limit", type=int, default=50)
        s.set_defaults(func=fn)

    s = sub.add_parser("show", help="full audit trail for one transaction")
    s.add_argument("txn_id")
    s.set_defaults(func=cmd_show)

    sub.add_parser("verify-audit", help="verify the audit hash chain").set_defaults(func=cmd_verify)
    sub.add_parser("sweep", help="expire timed-out approvals").set_defaults(func=cmd_sweep)
    sub.add_parser("reconcile", help="resolve in-flight/unknown purchases").set_defaults(
        func=cmd_reconcile
    )
    sub.add_parser("check", help="run startup configuration checks").set_defaults(func=cmd_check)

    u = sub.add_parser("ui", help="open the web dashboard and chat")
    u.add_argument(
        "--demo",
        action="store_true",
        help="sandbox demo: built-in store, fake payments, no credentials needed",
    )
    u.add_argument("--reset", action="store_true", help="(demo) start from a fresh database")
    u.add_argument("--no-seed", action="store_true", help="(demo) don't add sample history")
    u.add_argument("--data-dir", default="data/demo", help="(demo) where demo files live")
    u.add_argument("--host", default="127.0.0.1")
    u.add_argument("--port", type=int, default=8420)
    u.add_argument("--no-browser", action="store_true")
    u.add_argument(
        "--allow-remote",
        action="store_true",
        help="allow binding a non-loopback address (not recommended)",
    )
    u.set_defaults(func=cmd_ui)

    sh = sub.add_parser("shop", help="your own agent-checkout shop on your Stripe account")
    shs = sh.add_subparsers(dest="shop_cmd", required=True)
    si = shs.add_parser("init", help="create the shop config, agent key and store entry")
    si.add_argument("--test-profile", help="the shop account's Stripe profile id (test mode)")
    si.add_argument("--live-profile", help="the shop account's Stripe profile id (live mode)")
    shs.add_parser("run", help="open the shop on 127.0.0.1")
    shs.add_parser("orders", help="list the shop's orders")
    sr = shs.add_parser("refund", help="refund an order (or --all)")
    sr.add_argument("order_id", nargs="?")
    sr.add_argument("--all", action="store_true")
    sh.set_defaults(func=cmd_shop)

    sub.add_parser(
        "stripe-check", help="verify Stripe keys, card and shop profile (no money moves)"
    ).set_defaults(func=cmd_stripe_check)

    m = sub.add_parser("sandbox-merchant", help="run the reference ACP merchant locally")
    m.add_argument("--host", default="127.0.0.1")
    m.add_argument("--port", type=int, default=8787)
    m.set_defaults(func=cmd_sandbox_merchant)
    return p


def main(argv: list[str] | None = None) -> int:
    from spenddat.dotenv import load_dotenv

    load_dotenv()
    configure_logging()
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except StartupError as e:
        print(str(e), file=sys.stderr)
        return 78  # EX_CONFIG


if __name__ == "__main__":
    sys.exit(main())
