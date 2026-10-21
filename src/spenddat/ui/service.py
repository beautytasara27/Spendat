"""Application service behind the web UI.

Thin, read-mostly layer over the orchestrator and audit store. It never makes policy or
execution decisions itself: purchases go through ``Orchestrator.submit`` and approvals
through ``Orchestrator.decide_approval``, exactly like the CLI.
"""

from __future__ import annotations

import contextlib
import getpass
import json
import re
import threading
import uuid
from collections.abc import Callable
from datetime import timedelta
from typing import Any

from spenddat.approval import ApprovalError
from spenddat.audit import ChainError
from spenddat.models import (
    ApprovalAction,
    Modification,
    RuntimeMode,
    TxnResult,
    TxnState,
)
from spenddat.orchestrator import Orchestrator
from spenddat.stores import StoreConfig, StoreConfigError

Block = dict[str, Any]


def _money(minor: int | None) -> str:
    return "-" if minor is None else f"${minor / 100:,.2f}"


class UiService:
    def __init__(
        self,
        orch: Orchestrator,
        *,
        llm_label: str,
        config: StoreConfig | None = None,
        rebuild: Callable[[], Orchestrator] | None = None,
        demo: bool = False,
    ) -> None:
        self.orch = orch
        self.llm_label = llm_label
        self.config = config
        self._rebuild = rebuild
        self.demo = demo
        self._lock = threading.RLock()
        self._conversations: dict[str, dict[str, Any]] = {}
        self._approver = f"{getpass.getuser()} (ui)"

    # ================================================================== stores

    def stores(self, check: bool | str = False) -> dict[str, Any]:
        """``check=True`` probes API stores; ``check="<id>"`` probes just that store
        (browser stores are only probed on request: it opens Chrome)."""
        with self._lock:
            if self.config is None:
                return {"editable": False, "stores": [], "catalogs": []}
            rows = self.config.list()
            for r in rows:
                adapter = self.orch.registry.get(r["id"])
                r["loaded"] = adapter is not None
                r["secret_env"] = (
                    self.config.secret_env_name(r["id"]) if r["type"] == "acp" else None
                )
                r["health"] = None
                r["integration"] = getattr(adapter, "integration", None)
                wanted = check == r["id"] or (
                    check is True and getattr(adapter, "integration", "api") != "browser"
                )
                if wanted and adapter is not None:
                    try:
                        r["health"] = adapter.health_check()
                    except Exception as e:
                        r["health"] = {"ok": False, "error": str(e)[:200]}
            from spenddat.sandbox.catalogs import CATALOGS

            return {
                "editable": True,
                "mode": self.orch.mode.value,
                "demo": self.demo,
                "stores": rows,
                "catalogs": sorted(CATALOGS) if self.demo else [],
                "vendors_file": str(self.config.vendors_file),
                "policy_file": str(self.config.policy_file),
            }

    def change_store(self, action: str, vid: str | None, body: dict[str, Any]) -> dict[str, Any]:
        """Apply one store change, reload, and audit it. Rolls back if the reload fails."""
        if self.config is None or self._rebuild is None:
            raise StoreConfigError("store editing is not available in this mode")
        with self._lock:
            snap = self.config.snapshot()
            before_version = self.orch.policy.full_version
            before = self._store_row(vid) if vid else None
            try:
                if action == "add":
                    vid = self.config.add(body)
                elif action == "update":
                    assert vid is not None
                    if "enabled" in body:
                        self.config.set_enabled(vid, bool(body["enabled"]))
                    if "trust" in body:
                        self.config.set_trust(vid, str(body["trust"]))
                elif action == "remove":
                    assert vid is not None
                    self.config.remove(vid)
                else:
                    raise StoreConfigError(f"unknown action {action!r}")
                new_orch = self._rebuild()
            except Exception as e:
                self.config.restore(snap)
                if isinstance(e, StoreConfigError):
                    raise
                raise StoreConfigError(f"change not applied: {e}") from e
            self.orch = new_orch
            after = self._store_row(vid) if action != "remove" else None
            self.orch.store.append(
                "config",
                "stores_changed",
                self.orch.mode,
                self.orch.now(),
                {
                    "action": action,
                    "store": vid,
                    "before": before,
                    "after": after,
                    "changed_by": self._approver,
                    "policy_version_before": before_version,
                    "policy_version_after": self.orch.policy.full_version,
                },
            )
            return {"store": after, "policy_version": self.orch.policy.full_version}

    def _store_row(self, vid: str | None) -> dict[str, Any] | None:
        if not vid or self.config is None:
            return None
        try:
            r = self.config.get(vid)
        except StoreConfigError:
            return None
        return {k: r[k] for k in ("id", "name", "type", "enabled", "trust")}

    # ================================================================== dashboard data

    def _receipts(self) -> dict[str, dict[str, Any]]:
        rows = self.orch.store._db.execute(
            "SELECT txn_id, payload FROM audit_events WHERE event_type='execution_succeeded'"
        ).fetchall()
        return {r["txn_id"]: json.loads(r["payload"]).get("receipt", {}) for r in rows}

    def _txn_view(self, row: Any, receipts: dict[str, Any]) -> dict[str, Any]:
        snap = json.loads(row["snapshot"]) if row["snapshot"] else {}
        item = snap.get("item") or {}
        intent = snap.get("intent") or {}
        decision = snap.get("decision") or {}
        apr = self.orch.store.approval_for_txn(row["txn_id"])
        receipt = receipts.get(row["txn_id"])
        return {
            "txn_id": row["txn_id"],
            "mode": row["mode"],
            "state": row["state"],
            "request_text": row["request_text"],
            "vendor": row["vendor"],
            "category": row["category"],
            "sku": row["sku"],
            "title": item.get("title") or intent.get("product_description"),
            "quantity": item.get("quantity") or intent.get("quantity"),
            "amount_minor": (receipt or {}).get("amount_minor")
            or row["amount_minor"]
            or item.get("total_minor"),
            "created_at": row["created_at"],
            "committed_at": row["committed_at"],
            "completed_at": row["completed_at"],
            "outcome": decision.get("outcome"),
            "reasons": [r["detail"] for r in decision.get("reasons", [])],
            "reason_codes": [r["code"] for r in decision.get("reasons", [])],
            "approval": None
            if apr is None
            else {
                "approval_id": apr["approval_id"],
                "status": apr["status"],
                "decided_by": apr["decided_by"],
                "action": apr["action"],
                "expires_at": apr["expires_at"],
            },
            "order_id": (receipt or {}).get("order_id"),
            "autonomous": receipt is not None and apr is None,
        }

    def transactions(self, mode: RuntimeMode | None, limit: int = 300) -> list[dict[str, Any]]:
        with self._lock:
            receipts = self._receipts()
            return [self._txn_view(r, receipts) for r in self.orch.store.list_txns(mode, limit)]

    def transaction_detail(self, txn_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self.orch.store.get_txn(txn_id)
            if row is None:
                return None
            view = self._txn_view(row, self._receipts())
            view["events"] = [
                {
                    "seq": e.seq,
                    "type": e.event_type,
                    "ts": e.ts,
                    "mode": e.mode,
                    "hash": e.hash[:12],
                    "payload": e.payload,
                }
                for e in self.orch.store.events(txn_id)
            ]
            return view

    def approvals(self) -> list[dict[str, Any]]:
        with self._lock:
            self.orch.sweep_expired()
            return [
                {
                    "approval_id": r["approval_id"],
                    "txn_id": r["txn_id"],
                    "mode": r["mode"],
                    "created_at": r["created_at"],
                    "expires_at": r["expires_at"],
                    "presented": json.loads(r["presented"]),
                }
                for r in self.orch.store.pending_approvals(self.orch.mode)
            ]

    def summary(self) -> dict[str, Any]:
        with self._lock:
            o, store, now = self.orch, self.orch.store, self.orch.now()
            p, hl = o.policy, o.hard_limits
            try:
                chain = {"ok": True, "events": store.verify_chain()}
            except ChainError as e:
                chain = {"ok": False, "error": str(e)}
            ship = o.ship_to
            return {
                "mode": o.mode.value,
                "llm": self.llm_label,
                "now": now.isoformat(),
                "vendors": o.vendor_ids(),
                "vendor_names": o.registry.display_names(),
                "allowed_vendors": sorted(p.vendors.allow),
                "ship_to": f"{ship.name}, {ship.city} {ship.state}" if ship else None,
                "policy": {
                    "version": p.full_version,
                    "per_transaction_minor": p.caps.per_transaction_minor,
                    "per_category_minor": p.caps.per_category_minor,
                    "daily_minor": p.caps.daily_minor,
                    "weekly_minor": p.caps.weekly_minor,
                    "price_deviation_pct": p.anomaly.price_deviation_pct,
                    "velocity": p.anomaly.velocity.model_dump(),
                    "approval_timeout_minutes": p.approval.timeout_minutes,
                    "allowed_categories": sorted(p.categories.allow),
                    "denied_categories": sorted(p.categories.deny),
                },
                "hard_limits": {
                    "transaction_minor": hl.max_transaction_minor,
                    "daily_minor": hl.max_daily_minor,
                    "weekly_minor": hl.max_weekly_minor,
                },
                "spend": {
                    "day_minor": store.spend_since(o.mode, now - timedelta(days=1)),
                    "week_minor": store.spend_since(o.mode, now - timedelta(days=7)),
                },
                "pending_approvals": len(store.pending_approvals(o.mode)),
                "audit": chain,
            }

    # ================================================================== approvals

    def decide(
        self,
        approval_id: str,
        action: str,
        max_total_minor: int | None = None,
        quantity: int | None = None,
    ) -> list[Block]:
        with self._lock:
            mod = None
            act = ApprovalAction.DENY if action == "deny" else ApprovalAction.APPROVE
            if act is ApprovalAction.APPROVE and (max_total_minor or quantity):
                act = ApprovalAction.APPROVE_WITH_MODIFICATION
                mod = Modification(max_total_minor=max_total_minor, quantity=quantity)
            try:
                res = self.orch.decide_approval(approval_id, act, self._approver, mod)
            except ApprovalError as e:
                return [{"type": "text", "tone": "warn", "text": str(e)}]
            return self._result_blocks(res)

    # ================================================================== chat

    HELP = (
        "I'm your purchasing agent. Tell me what to buy in plain English, for example "
        '"buy a clear phone case for my iPhone 15 under $25". In-policy purchases I '
        "complete on my own; anything over a limit, from a new store, or unusual, I'll "
        "bring to you for approval; restricted items I decline. You can also ask me "
        '"how much have I spent?", "what\'s pending?", "show recent orders" or '
        '"what are my limits?".'
    )

    def chat(self, conversation_id: str | None, message: str) -> dict[str, Any]:
        message = message.strip()
        cid = conversation_id or f"conv_{uuid.uuid4().hex[:12]}"
        with self._lock:
            conv = self._conversations.setdefault(cid, {"pending": None})
            if not message:
                return {"conversation_id": cid, "blocks": []}
            blocks = self._quick_answer(message)
            if blocks is None:
                text = message
                if conv["pending"]:
                    text = f"{conv['pending']}. {message}"
                    conv["pending"] = None
                res = self.orch.submit(text)
                if res.state is TxnState.NEEDS_CLARIFICATION:
                    conv["pending"] = text
                blocks = self._result_blocks(res)
            return {"conversation_id": cid, "blocks": blocks}

    def _quick_answer(self, message: str) -> list[Block] | None:
        low = message.lower().strip(" ?!.")
        if re.search(r"\b(buy|order|purchase|get me|pick up)\b", low):
            return None
        if low in ("help", "hi", "hello", "hey") or "what can you do" in low:
            return [{"type": "text", "text": self.HELP}]
        if re.search(r"\b(spend|spent|spending|budget)\b", low):
            s = self.summary()
            return [
                {"type": "text", "text": "Here's where your spending stands."},
                {
                    "type": "spend",
                    "spend": s["spend"],
                    "policy": s["policy"],
                    "hard_limits": s["hard_limits"],
                },
            ]
        if re.search(r"\b(pending|approval|approvals|waiting)\b", low):
            aps = self.approvals()
            if not aps:
                return [{"type": "text", "text": "Nothing is waiting for your approval."}]
            return [
                {
                    "type": "text",
                    "text": f"{len(aps)} purchase{'s' if len(aps) != 1 else ''} waiting on you:",
                },
                *[
                    self._approval_block(a["approval_id"], a["presented"], a["expires_at"])
                    for a in aps
                ],
            ]
        if re.search(r"\b(history|recent|orders|purchases|bought)\b", low):
            txns = self.transactions(self.orch.mode, 6)
            return [
                {"type": "text", "text": "Your most recent activity:"},
                {"type": "list", "transactions": txns},
            ]
        if re.search(r"\b(limit|limits|caps|policy|rules)\b", low):
            s = self.summary()
            return [
                {"type": "text", "text": "These are the rules I follow."},
                {"type": "policy", "policy": s["policy"], "hard_limits": s["hard_limits"]},
            ]
        return None

    @staticmethod
    def _approval_block(approval_id: str, presented: dict[str, Any], expires_at: str) -> Block:
        return {
            "type": "approval",
            "approval_id": approval_id,
            "item": presented["item"],
            "reasons": presented["reasons"],
            "ship_to": presented.get("ship_to"),
            "alternatives": presented.get("alternatives", []),
            "expires_at": expires_at,
        }

    def _result_blocks(self, res: TxnResult) -> list[Block]:
        snap: dict[str, Any] = {}
        with contextlib.suppress(KeyError):
            snap = self.orch.snapshot(res.txn_id)
        item = snap.get("item") or {}
        d = res.decision
        if res.state is TxnState.COMPLETED and res.receipt:
            human = res.approval_id is not None
            return [
                {
                    "type": "text",
                    "text": "Approved and purchased."
                    if human
                    else "Done. That was within your policy, so I bought it on my own.",
                },
                {
                    "type": "receipt",
                    "txn_id": res.txn_id,
                    "mode": res.mode.value,
                    "title": item.get("title"),
                    "sku": item.get("sku"),
                    "quantity": item.get("quantity"),
                    "vendor": res.receipt.vendor,
                    "order_id": res.receipt.order_id,
                    "amount_minor": res.receipt.amount_minor,
                    "unit_minor": item.get("unit_price_minor"),
                    "shipping_minor": item.get("shipping_minor"),
                    "tax_minor": item.get("tax_minor"),
                    "basis": "human_approval" if human else "policy_allow",
                },
            ]
        if res.state is TxnState.PENDING_APPROVAL and res.approval_id:
            apr = self.orch.store.get_approval(res.approval_id)
            presented = json.loads(apr["presented"])
            return [
                {"type": "text", "text": "I found it, but this one needs your OK before I buy."},
                self._approval_block(res.approval_id, presented, apr["expires_at"]),
            ]
        if res.state is TxnState.DENIED and d:
            return [
                {
                    "type": "denied",
                    "txn_id": res.txn_id,
                    "title": item.get("title")
                    or (snap.get("intent") or {}).get("product_description"),
                    "reasons": [r.detail for r in d.reasons],
                }
            ]
        if res.state is TxnState.NEEDS_CLARIFICATION:
            return [{"type": "text", "tone": "ask", "text": res.message}]
        if res.state is TxnState.REJECTED:
            return [
                {
                    "type": "text",
                    "tone": "warn",
                    "text": f"I can't do that one. {_humanize(res.message)}",
                }
            ]
        tone = {
            TxnState.APPROVAL_DENIED: "muted",
            TxnState.APPROVAL_EXPIRED: "muted",
        }.get(res.state, "warn")
        return [
            {
                "type": "status",
                "tone": tone,
                "state": res.state.value,
                "txn_id": res.txn_id,
                "text": res.message,
            }
        ]


def _humanize(msg: str) -> str:
    code, _, rest = msg.partition(": ")
    return rest or code
