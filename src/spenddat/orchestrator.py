"""Orchestrator: intent -> policy -> (approval only if ESCALATE) -> execution -> audit.

Safety properties enforced here (and tested in tests/test_orchestrator.py):

1. **Single execution gate.** ``_execute`` is the only code that obtains a payment
   credential or calls ``adapter.purchase``. It requires an ``ExecutionPermit``, which
   only ``_authorize`` can mint, and ``_authorize`` re-derives authority from the audit
   store: a recorded policy ALLOW for this transaction, or a recorded human approval of
   its ESCALATE. Error and retry paths never mint permits; reconciliation only *reads*
   vendor state and never re-submits a purchase.
2. **Audit before and after.** ``execution_started`` is committed before the credential
   is issued; a terminal event is appended afterwards on every path, including failure.
3. **Hard ceilings last.** Circuit breakers run inside the same DB transaction that
   reserves the spend, after policy and after approval.
4. **Idempotency.** Vendor and payment calls carry keys derived from the transaction id;
   a lost response is reconciled by reading vendor state, never by paying again.
5. **Mode-blind.** Nothing here branches on sandbox vs live; the mode is only recorded.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from spenddat.approval import ApprovalError, ApprovalService
from spenddat.audit import AuditStore
from spenddat.guards import CircuitBreakerTripped, HardLimits, check_hard_limits
from spenddat.intent import IntentParser
from spenddat.models import (
    Address,
    ApprovalAction,
    ApprovalStatus,
    Candidate,
    Clarification,
    DataQuality,
    Decision,
    Modification,
    OrderStatus,
    Outcome,
    PurchaseIntent,
    Receipt,
    ResolvedItem,
    RuntimeMode,
    SpendContext,
    TransactionFacts,
    TxnResult,
    TxnState,
    UnsupportedRequest,
)
from spenddat.observability import current_mode, current_txn, get_logger, log
from spenddat.payments import PaymentError, PaymentProvider, ScopedCredential
from spenddat.policy import PolicyEngine
from spenddat.vendors.base import (
    AdapterRegistry,
    PaymentHandle,
    VendorAdapter,
    VendorError,
    VendorOutcomeUnknown,
)

_log = get_logger("orchestrator")
AMBIGUITY_MARGIN = 0.1
CREDENTIAL_TTL = timedelta(minutes=15)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _one_line(a: Address | None) -> str:
    if a is None:
        return "(none)"
    parts = [a.name, a.line_one, a.line_two, f"{a.city}, {a.state} {a.postal_code}", a.country]
    return ", ".join(p for p in parts if p)


class _PermitSeal:
    """Module-private capability; only ``_authorize`` holds an instance."""


_SEAL = _PermitSeal()


@dataclass(frozen=True)
class ExecutionPermit:
    txn_id: str
    basis: str  # "policy_allow" | "human_approval"
    ceiling_minor: int
    seal: object

    def __post_init__(self) -> None:
        if self.seal is not _SEAL:
            raise PermissionError("ExecutionPermit can only be created by the execution gate")


class GateViolation(PermissionError):
    pass


class Orchestrator:
    def __init__(
        self,
        *,
        mode: RuntimeMode,
        store: AuditStore,
        parser: IntentParser,
        engine: PolicyEngine,
        approvals: ApprovalService,
        registry: AdapterRegistry,
        payments: PaymentProvider,
        hard_limits: HardLimits,
        ship_to: Address | None,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self.mode = mode
        self.store = store
        self._parser = parser
        self._engine = engine
        self._approvals = approvals
        self._registry = registry
        self._payments = payments
        self._limits = hard_limits
        self._ship_to = ship_to
        self._now = clock

    # ================================================================== helpers

    def _event(self, txn_id: str, event: str, payload: dict[str, Any]) -> None:
        self.store.append(txn_id, event, self.mode, self._now(), payload)
        log(_log, 20, event, **{k: v for k, v in payload.items() if k in ("state", "outcome")})

    def _set(
        self, txn_id: str, state: TxnState, event: str, payload: dict[str, Any], **fields: Any
    ) -> None:
        with self.store.atomic():
            self.store.update_txn(txn_id, self._now(), state=state, **fields)
            self._event(txn_id, event, {"state": state.value, **payload})

    def _result(self, txn_id: str, state: TxnState, message: str, **kw: Any) -> TxnResult:
        return TxnResult(txn_id=txn_id, state=state, message=message, mode=self.mode, **kw)

    def _snapshot(self, txn_id: str) -> dict[str, Any]:
        row = self.store.get_txn(txn_id)
        if row is None:
            raise KeyError(txn_id)
        return json.loads(row["snapshot"]) if row["snapshot"] else {}

    def _save_snapshot(self, txn_id: str, **updates: Any) -> None:
        snap = self._snapshot(txn_id)
        snap.update(updates)
        self.store.update_txn(txn_id, self._now(), snapshot=json.dumps(snap, default=str))

    def _bind(self, txn_id: str) -> None:
        current_txn.set(txn_id)
        current_mode.set(self.mode.value)

    # ================================================================== entry point

    def submit(self, request_text: str, client_request_id: str | None = None) -> TxnResult:
        """Handle one natural-language purchase request end to end."""
        if client_request_id:
            digest = hashlib.sha256(f"{self.mode.value}:{client_request_id}".encode()).hexdigest()
            txn_id = f"txn_{digest[:20]}"
            existing = self.store.get_txn(txn_id)
            if existing is not None:  # idempotent resubmission: report, never redo
                return self._result(
                    txn_id,
                    TxnState(existing["state"]),
                    "duplicate request; returning existing transaction",
                )
        else:
            txn_id = f"txn_{uuid.uuid4().hex[:20]}"
        self._bind(txn_id)
        self._approvals.sweep_expired(self._now())
        with self.store.atomic():
            self.store.create_txn(txn_id, self.mode, request_text, self._now())
            self._event(
                txn_id, "request_received", {"request_text": request_text, "mode": self.mode.value}
            )

        parsed = self._parser.parse(request_text)
        self._event(txn_id, "intent_parsed", {"result": parsed.model_dump(mode="json")})
        if isinstance(parsed, Clarification):
            self._set(
                txn_id,
                TxnState.NEEDS_CLARIFICATION,
                "clarification_requested",
                {"question": parsed.question},
            )
            return self._result(txn_id, TxnState.NEEDS_CLARIFICATION, parsed.question)
        if isinstance(parsed, UnsupportedRequest):
            self._set(
                txn_id,
                TxnState.REJECTED,
                "request_rejected",
                {"reason_code": parsed.reason_code, "explanation": parsed.explanation},
            )
            return self._result(
                txn_id, TxnState.REJECTED, f"{parsed.reason_code}: {parsed.explanation}"
            )
        return self._plan(txn_id, parsed)

    # ================================================================== planning

    def _plan(self, txn_id: str, intent: PurchaseIntent) -> TxnResult:
        with self.store.atomic():
            self.store.update_txn(
                txn_id, self._now(), vendor=intent.vendor, category=intent.category
            )
            self._save_snapshot(txn_id, intent=intent.model_dump(mode="json"))

        adapter = self._registry.get(intent.vendor)
        if adapter is None:
            if intent.vendor in self._engine.policy.vendors.deny:
                facts = self._facts(intent, None, 0, ambiguous=False)
                return self._decide(txn_id, intent, facts, None, [])
            msg = (
                f"unsupported_vendor: no integration for '{intent.vendor}'; "
                f"supported: {', '.join(self._registry.vendor_ids())}"
            )
            self._set(
                txn_id,
                TxnState.REJECTED,
                "request_rejected",
                {"reason_code": "unsupported_vendor", "explanation": msg},
            )
            return self._result(txn_id, TxnState.REJECTED, msg)

        try:
            candidates = [
                c
                for c in adapter.search(intent.product_description)
                if c.currency == intent.currency
            ]
        except VendorError as e:
            self._set(txn_id, TxnState.FAILED, "vendor_unavailable", {"error": str(e)})
            return self._result(txn_id, TxnState.FAILED, f"vendor search failed: {e}")
        self._event(
            txn_id,
            "candidates_found",
            {"candidates": [c.model_dump(mode="json") for c in candidates]},
        )

        ambiguous = len(candidates) > 1 and (
            candidates[0].score - candidates[1].score < AMBIGUITY_MARGIN
        )
        item: ResolvedItem | None = None
        if candidates and self._ship_to is not None:
            try:
                item = adapter.resolve(candidates[0], intent.quantity, self._ship_to)
            except VendorError as e:
                self._event(txn_id, "resolution_failed", {"error": str(e)})
        if item is not None:
            self._event(txn_id, "item_resolved", {"item": item.model_dump(mode="json")})

        facts = self._facts(intent, item, len(candidates), ambiguous=ambiguous)
        return self._decide(txn_id, intent, facts, item, candidates[1:] if ambiguous else [])

    def _facts(
        self, intent: PurchaseIntent, item: ResolvedItem | None, n: int, *, ambiguous: bool
    ) -> TransactionFacts:
        return TransactionFacts(
            vendor=intent.vendor,
            category=intent.category,
            quantity=item.quantity if item else intent.quantity,
            unit_price_minor=item.unit_price_minor if item else None,
            total_minor=item.total_minor if item else None,
            currency=item.currency if item else intent.currency,
            user_max_price_minor=intent.max_price_minor,
            sku=item.sku if item else None,
            data=DataQuality(
                candidate_count=n,
                has_shipping_address=self._ship_to is not None,
                has_price=item is not None,
                ambiguous=ambiguous,
            ),
        )

    def _spend_context(self, facts: TransactionFacts) -> SpendContext:
        now = self._now()
        p = self._engine.policy
        return SpendContext(
            now=now,
            daily_spend_minor=self.store.spend_since(self.mode, now - timedelta(days=1)),
            weekly_spend_minor=self.store.spend_since(self.mode, now - timedelta(days=7)),
            recent_purchase_count=self.store.purchases_since(
                self.mode, now - timedelta(minutes=p.anomaly.velocity.window_minutes)
            ),
            reference_price_minor=self.store.reference_price(
                self.mode,
                facts.vendor,
                facts.sku,
                facts.category,
                now - timedelta(days=p.anomaly.history_days),
                p.anomaly.min_history,
            ),
        )

    def _decide(
        self,
        txn_id: str,
        intent: PurchaseIntent,
        facts: TransactionFacts,
        item: ResolvedItem | None,
        alternatives: list[Candidate],
    ) -> TxnResult:
        context = self._spend_context(facts)
        decision = self._engine.evaluate(facts, context)
        with self.store.atomic():
            self.store.update_txn(
                txn_id,
                self._now(),
                sku=facts.sku,
                unit_price_minor=facts.unit_price_minor,
                vendor_ref=item.vendor_ref if item else None,
            )
            self._save_snapshot(
                txn_id,
                item=item.model_dump(mode="json") if item else None,
                decision=decision.model_dump(mode="json"),
            )
            self._event(
                txn_id,
                "policy_decided",
                {
                    "outcome": decision.outcome.value,
                    "reason_codes": list(decision.reason_codes),
                    "explanation": decision.explanation(),
                    "policy_version": decision.policy_version,
                    "facts": facts.model_dump(mode="json"),
                    "context": context.model_dump(mode="json"),
                },
            )

        adapter = self._registry.get(facts.vendor)
        if decision.outcome is Outcome.DENY:
            if item and adapter:
                adapter.cancel(item.vendor_ref)
            msg = f"Denied: {decision.explanation()}"
            self._set(
                txn_id,
                TxnState.DENIED,
                "transaction_denied",
                {"reasons": list(decision.reason_codes)},
            )
            self._approvals.notify_user(txn_id, msg)
            return self._result(txn_id, TxnState.DENIED, msg, decision=decision)

        if decision.outcome is Outcome.ESCALATE:
            if item is None:
                # Nothing concrete to put in front of an approver; the missing data has to
                # come from the requester. The ESCALATE decision is already on record.
                msg = (
                    f"Needs more information before this can be reviewed: {decision.explanation()}"
                )
                self._set(
                    txn_id,
                    TxnState.NEEDS_CLARIFICATION,
                    "clarification_requested",
                    {"question": msg, "reasons": list(decision.reason_codes)},
                )
                return self._result(txn_id, TxnState.NEEDS_CLARIFICATION, msg, decision=decision)
            presented = {
                "item": item.model_dump(mode="json"),
                "ship_to": _one_line(self._ship_to),
                "reasons": [r.detail for r in decision.reasons],
                "reason_codes": list(decision.reason_codes),
                "request_text": self.store.get_txn(txn_id)["request_text"],
                "policy_version": decision.policy_version,
                "alternatives": [
                    f"{c.title} ({c.sku}) ${c.unit_price_minor / 100:,.2f}" for c in alternatives
                ],
            }
            req = self._approvals.open(txn_id, self.mode, presented, self._now())
            with self.store.atomic():
                self.store.update_txn(txn_id, self._now(), state=TxnState.PENDING_APPROVAL)
            return self._result(
                txn_id,
                TxnState.PENDING_APPROVAL,
                f"Escalated for approval ({req.approval_id}): {decision.explanation()}",
                decision=decision,
                approval_id=req.approval_id,
            )

        # ALLOW: execute immediately, no human involved.
        assert item is not None and adapter is not None  # ALLOW implies clean resolution
        permit = self._authorize(txn_id)
        return self._execute(permit, adapter, item, decision)

    # ================================================================== approvals

    def decide_approval(
        self,
        approval_id: str,
        action: ApprovalAction,
        decided_by: str,
        modification: Modification | None = None,
    ) -> TxnResult:
        row = self.store.get_approval(approval_id)
        if row is None:
            raise ApprovalError(f"unknown approval {approval_id}")
        txn_id = row["txn_id"]
        self._bind(txn_id)
        if RuntimeMode(row["mode"]) is not self.mode:
            raise ApprovalError(f"approval belongs to {row['mode']} mode; running {self.mode}")
        presented_total = int(json.loads(row["presented"])["item"]["total_minor"])
        presented_qty = int(json.loads(row["presented"])["item"]["quantity"])
        if modification:
            if modification.max_total_minor and modification.max_total_minor > presented_total:
                raise ApprovalError("a modification may only lower the approved total")
            if modification.quantity and modification.quantity > presented_qty:
                raise ApprovalError("a modification may only lower the quantity")

        try:
            dec = self._approvals.decide(approval_id, action, decided_by, self._now(), modification)
        except ApprovalError:
            self._finalize_expired()
            raise
        if dec.status is ApprovalStatus.DENIED:
            snap = self._snapshot(txn_id)
            self._cancel_cart(snap)
            msg = f"Denied by {decided_by}"
            self._set(
                txn_id,
                TxnState.APPROVAL_DENIED,
                "transaction_denied_by_human",
                {"approval_id": approval_id, "decided_by": decided_by},
            )
            self._approvals.notify_user(txn_id, msg)
            return self._result(txn_id, TxnState.APPROVAL_DENIED, msg, approval_id=approval_id)

        # Approved: re-resolve a fresh cart so price/stock are current, and hold the
        # vendor to the approved ceiling.
        snap = self._snapshot(txn_id)
        old = ResolvedItem.model_validate(snap["item"])
        adapter = self._registry.get(old.vendor)
        assert adapter is not None
        quantity = modification.quantity if modification and modification.quantity else old.quantity
        ceiling = (
            modification.max_total_minor
            if modification and modification.max_total_minor
            else presented_total
        )
        adapter.cancel(old.vendor_ref)
        candidate = Candidate(
            vendor=old.vendor,
            sku=old.sku,
            title=old.title,
            unit_price_minor=old.unit_price_minor,
            currency=old.currency,
        )
        try:
            if self._ship_to is None:
                raise VendorError("no shipping address")
            item = adapter.resolve(candidate, quantity, self._ship_to)
        except VendorError as e:
            self._set(
                txn_id,
                TxnState.FAILED,
                "execution_failed",
                {"stage": "re_resolve", "error": str(e)},
            )
            return self._result(txn_id, TxnState.FAILED, f"could not re-confirm item: {e}")
        if item.total_minor > ceiling:
            adapter.cancel(item.vendor_ref)
            msg = (
                f"current total ${item.total_minor / 100:,.2f} exceeds approved ceiling "
                f"${ceiling / 100:,.2f}; not purchased"
            )
            self._set(
                txn_id,
                TxnState.FAILED,
                "execution_failed",
                {"stage": "approved_ceiling", "error": msg},
            )
            self._approvals.notify_user(txn_id, msg)
            return self._result(txn_id, TxnState.FAILED, msg, approval_id=approval_id)
        with self.store.atomic():
            self.store.update_txn(
                txn_id,
                self._now(),
                vendor_ref=item.vendor_ref,
                unit_price_minor=item.unit_price_minor,
            )
            self._save_snapshot(
                txn_id, item=item.model_dump(mode="json"), approved_ceiling_minor=ceiling
            )
            self._event(
                txn_id,
                "item_reconfirmed",
                {"item": item.model_dump(mode="json"), "approved_ceiling_minor": ceiling},
            )
        permit = self._authorize(txn_id)
        return self._execute(permit, adapter, item, None, approval_id=approval_id)

    def sweep_expired(self) -> list[TxnResult]:
        self._approvals.sweep_expired(self._now())
        return self._finalize_expired()

    def _finalize_expired(self) -> list[TxnResult]:
        out = []
        for row in self.store.txns_in_state(self.mode, TxnState.PENDING_APPROVAL):
            apr = self.store.approval_for_txn(row["txn_id"])
            if apr is None or apr["status"] != ApprovalStatus.EXPIRED.value:
                continue
            txn_id = row["txn_id"]
            self._bind(txn_id)
            self._cancel_cart(self._snapshot(txn_id))
            msg = f"Approval {apr['approval_id']} timed out; transaction automatically denied"
            self._set(
                txn_id,
                TxnState.APPROVAL_EXPIRED,
                "transaction_expired",
                {"approval_id": apr["approval_id"]},
            )
            self._approvals.notify_user(txn_id, msg)
            out.append(self._result(txn_id, TxnState.APPROVAL_EXPIRED, msg))
        return out

    def _cancel_cart(self, snap: dict[str, Any]) -> None:
        if snap.get("item"):
            adapter = self._registry.get(snap["item"]["vendor"])
            if adapter:
                adapter.cancel(snap["item"]["vendor_ref"])

    # ================================================================== the gate

    def _authorize(self, txn_id: str) -> ExecutionPermit:
        """Mint a permit only from recorded authority. The only permit constructor."""
        snap = self._snapshot(txn_id)
        decision = Decision.model_validate(snap["decision"]) if snap.get("decision") else None
        item = snap.get("item")
        if decision is None or item is None:
            raise GateViolation(f"{txn_id}: no recorded decision/item")
        if decision.outcome is Outcome.ALLOW:
            return ExecutionPermit(txn_id, "policy_allow", int(item["total_minor"]), _SEAL)
        if decision.outcome is Outcome.ESCALATE:
            apr = self.store.approval_for_txn(txn_id)
            if apr is None or apr["status"] != ApprovalStatus.APPROVED.value:
                raise GateViolation(f"{txn_id}: ESCALATE without recorded human approval")
            ceiling = int(snap.get("approved_ceiling_minor") or item["total_minor"])
            return ExecutionPermit(txn_id, "human_approval", ceiling, _SEAL)
        raise GateViolation(f"{txn_id}: decision {decision.outcome} is not executable")

    def _execute(
        self,
        permit: ExecutionPermit,
        adapter: VendorAdapter,
        item: ResolvedItem,
        decision: Decision | None,
        approval_id: str | None = None,
    ) -> TxnResult:
        if not isinstance(permit, ExecutionPermit) or permit.seal is not _SEAL:
            raise GateViolation("execution requires a permit from the gate")
        txn_id = permit.txn_id
        row = self.store.get_txn(txn_id)
        if row is None or row["state"] not in (
            TxnState.RECEIVED.value,
            TxnState.PENDING_APPROVAL.value,
        ):
            raise GateViolation(f"{txn_id}: not in an executable state ({row and row['state']})")
        if item.total_minor > permit.ceiling_minor:
            raise GateViolation(f"{txn_id}: total exceeds authorised ceiling")

        # Hard circuit breakers + spend reservation + audit-before-execution, atomically.
        now = self._now()
        try:
            with self.store.atomic():
                check_hard_limits(self._limits, self.store, self.mode, item.total_minor, now)
                self.store.update_txn(
                    txn_id,
                    now,
                    state=TxnState.EXECUTING,
                    amount_minor=item.total_minor,
                    committed_at=now.isoformat(),
                    vendor_ref=item.vendor_ref,
                    unit_price_minor=item.unit_price_minor,
                    sku=item.sku,
                )
                self._event(
                    txn_id,
                    "execution_started",
                    {
                        "state": TxnState.EXECUTING.value,
                        "basis": permit.basis,
                        "ceiling_minor": permit.ceiling_minor,
                        "item": item.model_dump(mode="json"),
                        "idempotency_key": txn_id,
                        "mode": self.mode.value,
                    },
                )
        except CircuitBreakerTripped as cb:
            adapter.cancel(item.vendor_ref)
            self._set(
                txn_id,
                TxnState.BLOCKED_BY_CIRCUIT_BREAKER,
                "circuit_breaker_tripped",
                {"code": cb.code, "detail": cb.detail},
            )
            self._approvals.notify_user(txn_id, f"Blocked by hard limit: {cb}")
            return self._result(
                txn_id,
                TxnState.BLOCKED_BY_CIRCUIT_BREAKER,
                str(cb),
                decision=decision,
                approval_id=approval_id,
            )

        cred: ScopedCredential | None = None
        try:
            try:
                cred = self._payments.issue(
                    amount_minor=item.total_minor,
                    currency=item.currency,
                    seller=item.merchant_payment_id or item.vendor,
                    ttl=CREDENTIAL_TTL,
                    idempotency_key=f"{txn_id}:credential",
                    now=self._now(),
                    metadata={"txn_id": txn_id, "mode": self.mode.value},
                )
            except PaymentError as e:
                adapter.cancel(item.vendor_ref)
                return self._fail(txn_id, "payment_credential", str(e), decision, approval_id)
            self._event(
                txn_id,
                "payment_credential_issued",
                {
                    "provider": cred.provider,
                    "max_amount_minor": cred.max_amount_minor,
                    "currency": cred.currency,
                    "seller": cred.seller,
                    "expires_at": cred.expires_at.isoformat(),
                    "livemode": cred.livemode,
                },
            )
            handle = PaymentHandle(cred.token, cred.provider, cred.max_amount_minor, cred.currency)
            try:
                receipt = adapter.purchase(item, handle, self._ship_to, idempotency_key=txn_id)
            except VendorError as e:
                adapter.cancel(item.vendor_ref)
                return self._fail(txn_id, "purchase", str(e), decision, approval_id)
            except (VendorOutcomeUnknown, Exception) as e:
                self._event(
                    txn_id, "execution_outcome_unknown", {"error": f"{type(e).__name__}: {e}"}
                )
                return self._reconcile_one(txn_id, adapter, decision, approval_id)
            return self._complete(txn_id, receipt, decision, approval_id)
        finally:
            if cred is not None:
                self._revoke(txn_id, cred)

    def _revoke(self, txn_id: str, cred: ScopedCredential) -> None:
        try:
            self._payments.revoke(cred.token)
            self._event(txn_id, "payment_credential_revoked", {"provider": cred.provider})
        except PaymentError as e:
            self._event(txn_id, "payment_credential_revoke_failed", {"error": str(e)})

    def _complete(
        self, txn_id: str, receipt: Receipt, decision: Decision | None, approval_id: str | None
    ) -> TxnResult:
        now = self._now()
        self._set(
            txn_id,
            TxnState.COMPLETED,
            "execution_succeeded",
            {"receipt": receipt.model_dump(mode="json")},
            amount_minor=receipt.amount_minor,
            completed_at=now.isoformat(),
        )
        msg = (
            f"Purchased: order {receipt.order_id} from {receipt.vendor}, "
            f"${receipt.amount_minor / 100:,.2f} [{self.mode.value}]"
        )
        self._approvals.notify_user(txn_id, msg)
        return self._result(
            txn_id,
            TxnState.COMPLETED,
            msg,
            decision=decision,
            receipt=receipt,
            approval_id=approval_id,
        )

    def _fail(
        self,
        txn_id: str,
        stage: str,
        error: str,
        decision: Decision | None,
        approval_id: str | None,
    ) -> TxnResult:
        # amount released: nothing was charged.
        self._set(
            txn_id,
            TxnState.FAILED,
            "execution_failed",
            {"stage": stage, "error": error},
            amount_minor=0,
        )
        self._approvals.notify_user(txn_id, f"Purchase failed at {stage}: {error}")
        return self._result(
            txn_id,
            TxnState.FAILED,
            f"failed at {stage}: {error}",
            decision=decision,
            approval_id=approval_id,
        )

    # ================================================================== reconciliation

    def _reconcile_one(
        self,
        txn_id: str,
        adapter: VendorAdapter,
        decision: Decision | None,
        approval_id: str | None,
    ) -> TxnResult:
        row = self.store.get_txn(txn_id)
        status, receipt = adapter.order_status(row["vendor_ref"])
        self._event(txn_id, "reconciliation_checked", {"vendor_status": status.value})
        if status is OrderStatus.COMPLETED and receipt is not None:
            return self._complete(txn_id, receipt, decision, approval_id)
        if status in (OrderStatus.NOT_COMPLETED, OrderStatus.CANCELED):
            adapter.cancel(row["vendor_ref"])
            return self._fail(
                txn_id, "purchase", "vendor confirms no order was created", decision, approval_id
            )
        msg = "outcome unknown; spend held and flagged for reconciliation (will not retry payment)"
        self._set(txn_id, TxnState.NEEDS_RECONCILIATION, "needs_reconciliation", {"note": msg})
        self._approvals.notify_user(txn_id, msg)
        return self._result(
            txn_id, TxnState.NEEDS_RECONCILIATION, msg, decision=decision, approval_id=approval_id
        )

    def reconcile(self) -> list[TxnResult]:
        """Resolve in-flight transactions after a crash or unknown outcome. Never re-pays."""
        out = []
        for row in self.store.txns_in_state(
            self.mode, TxnState.EXECUTING, TxnState.NEEDS_RECONCILIATION
        ):
            self._bind(row["txn_id"])
            adapter = self._registry.get(row["vendor"])
            if adapter is None or not row["vendor_ref"]:
                continue
            out.append(self._reconcile_one(row["txn_id"], adapter, None, None))
        return out
