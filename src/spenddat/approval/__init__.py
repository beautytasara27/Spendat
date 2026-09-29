"""Human-in-the-loop approval (exception path only).

Invoked by the orchestrator exclusively for ESCALATE decisions. It owns the escalation
lifecycle - creation, notification, expiry, and recording the human's decision - but
never executes anything itself; the orchestrator acts on the recorded decision.
"""

from __future__ import annotations

import json
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol, TextIO

import httpx

from spenddat.audit import AuditStore
from spenddat.models import ApprovalAction, ApprovalStatus, Modification, RuntimeMode
from spenddat.observability import get_logger, log

_log = get_logger("approval")


class ApprovalError(RuntimeError):
    pass


@dataclass(frozen=True)
class ApprovalRequest:
    approval_id: str
    txn_id: str
    mode: RuntimeMode
    expires_at: datetime
    presented: dict[str, Any]


class Notifier(Protocol):
    def approval_requested(self, req: ApprovalRequest) -> None: ...

    def user_update(self, txn_id: str, message: str) -> None: ...


def render_request(req: ApprovalRequest) -> str:
    p = req.presented
    item = p["item"]
    lines = [
        f"[{req.mode.value.upper()}] Purchase needs approval  (approval {req.approval_id})",
        f"  Vendor:    {item['vendor']}",
        f"  Product:   {item['title']}  (SKU {item['sku']})",
        f"  Quantity:  {item['quantity']}",
        f"  Price:     ${item['unit_price_minor'] / 100:,.2f} each; shipping "
        f"${item['shipping_minor'] / 100:,.2f}; tax ${item['tax_minor'] / 100:,.2f}",
        f"  TOTAL:     ${item['total_minor'] / 100:,.2f} {item['currency'].upper()}",
        f"  Ship to:   {p['ship_to']}",
        "  Escalated because:",
        *[f"    - {r}" for r in p["reasons"]],
        f'  Request:   "{p["request_text"]}"',
        f"  Expires:   {req.expires_at.isoformat()} (auto-denied after this)",
        f"  Approve:   spenddat approve {req.approval_id}",
        f"  Limit:     spenddat approve {req.approval_id} --max-total <dollars>",
        f"  Deny:      spenddat deny {req.approval_id}",
    ]
    return "\n".join(lines)


class ConsoleNotifier:
    def __init__(self, stream: TextIO | None = None) -> None:
        self._stream = stream or sys.stderr

    def approval_requested(self, req: ApprovalRequest) -> None:
        print(render_request(req), file=self._stream)

    def user_update(self, txn_id: str, message: str) -> None:
        print(f"[{txn_id}] {message}", file=self._stream)


class WebhookNotifier:
    """Posts to a Slack/Teams/Discord-style incoming webhook (``{"text": ...}``)."""

    def __init__(self, url: str, client: httpx.Client | None = None) -> None:
        self._url = url
        self._http = client or httpx.Client(timeout=10.0)

    def _post(self, text: str) -> None:
        try:
            self._http.post(self._url, json={"text": text}).raise_for_status()
        except httpx.HTTPError as e:  # notification failure must not lose the approval
            log(_log, 40, "webhook_failed", error=type(e).__name__)

    def approval_requested(self, req: ApprovalRequest) -> None:
        self._post("```\n" + render_request(req) + "\n```")

    def user_update(self, txn_id: str, message: str) -> None:
        self._post(f"[{txn_id}] {message}")


class RecordingNotifier:
    """Captures notifications in memory (tests)."""

    def __init__(self) -> None:
        self.requests: list[ApprovalRequest] = []
        self.updates: list[tuple[str, str]] = []

    def approval_requested(self, req: ApprovalRequest) -> None:
        self.requests.append(req)

    def user_update(self, txn_id: str, message: str) -> None:
        self.updates.append((txn_id, message))


@dataclass(frozen=True)
class ApprovalDecision:
    approval_id: str
    txn_id: str
    status: ApprovalStatus
    action: ApprovalAction | None
    decided_by: str
    modification: Modification | None


class ApprovalService:
    def __init__(self, store: AuditStore, notifiers: list[Notifier], timeout: timedelta) -> None:
        self._store = store
        self._notifiers = notifiers
        self._timeout = timeout

    def open(
        self, txn_id: str, mode: RuntimeMode, presented: dict[str, Any], now: datetime
    ) -> ApprovalRequest:
        approval_id = f"apr_{uuid.uuid4().hex[:12]}"
        req = ApprovalRequest(approval_id, txn_id, mode, now + self._timeout, presented)
        with self._store.atomic():
            self._store.create_approval(approval_id, txn_id, mode, now, self._timeout, presented)
            self._store.append(
                txn_id,
                "approval_requested",
                mode,
                now,
                {
                    "approval_id": approval_id,
                    "expires_at": req.expires_at.isoformat(),
                    "presented": presented,
                },
            )
        for n in self._notifiers:
            n.approval_requested(req)
        log(_log, 20, "approval_requested", approval_id=approval_id)
        return req

    def decide(
        self,
        approval_id: str,
        action: ApprovalAction,
        decided_by: str,
        now: datetime,
        modification: Modification | None = None,
    ) -> ApprovalDecision:
        if not decided_by.strip():
            raise ApprovalError("approver identity is required")
        if action is ApprovalAction.APPROVE_WITH_MODIFICATION and not modification:
            raise ApprovalError("approve_with_modification requires a modification")
        row = self._store.get_approval(approval_id)
        if row is None:
            raise ApprovalError(f"unknown approval {approval_id}")
        if row["status"] != ApprovalStatus.PENDING.value:
            raise ApprovalError(f"approval {approval_id} is already {row['status']}")
        if now >= datetime.fromisoformat(row["expires_at"]):
            self.sweep_expired(now)
            raise ApprovalError(f"approval {approval_id} expired at {row['expires_at']}")

        mode = RuntimeMode(row["mode"])
        status = ApprovalStatus.DENIED if action is ApprovalAction.DENY else ApprovalStatus.APPROVED
        mod = modification.model_dump(exclude_none=True) if modification else None
        with self._store.atomic():
            if not self._store.decide_approval(
                approval_id, status, decided_by, now, action.value, mod
            ):
                raise ApprovalError(f"approval {approval_id} was decided concurrently")
            self._store.append(
                row["txn_id"],
                "approval_decided",
                mode,
                now,
                {
                    "approval_id": approval_id,
                    "action": action.value,
                    "decided_by": decided_by,
                    "modification": mod,
                    "presented": json.loads(row["presented"]),
                },
            )
        log(_log, 20, "approval_decided", approval_id=approval_id, action=action.value)
        return ApprovalDecision(
            approval_id, row["txn_id"], status, action, decided_by, modification
        )

    def sweep_expired(self, now: datetime) -> list[ApprovalDecision]:
        expired = []
        for row in self._store.pending_approvals():
            if now < datetime.fromisoformat(row["expires_at"]):
                continue
            mode = RuntimeMode(row["mode"])
            with self._store.atomic():
                if not self._store.decide_approval(
                    row["approval_id"],
                    ApprovalStatus.EXPIRED,
                    "system:timeout",
                    now,
                    "expire",
                    None,
                ):
                    continue
                self._store.append(
                    row["txn_id"],
                    "approval_expired",
                    mode,
                    now,
                    {
                        "approval_id": row["approval_id"],
                        "expires_at": row["expires_at"],
                    },
                )
            expired.append(
                ApprovalDecision(
                    row["approval_id"],
                    row["txn_id"],
                    ApprovalStatus.EXPIRED,
                    None,
                    "system:timeout",
                    None,
                )
            )
        return expired

    def notify_user(self, txn_id: str, message: str) -> None:
        for n in self._notifiers:
            n.user_update(txn_id, message)
