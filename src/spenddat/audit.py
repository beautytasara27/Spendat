"""Append-only, tamper-evident audit log plus the state projection built from it.

* ``audit_events`` is the system of record. Rows are hash-chained (each row stores
  SHA-256(prev_hash || canonical row)) and SQLite triggers reject UPDATE/DELETE, so any
  edit made outside the application breaks ``verify_chain()``.
* ``transactions`` / ``approvals`` are convenience projections for fast queries
  (current state, cumulative spend). They are updated in the same DB transaction as the
  event that justifies the change, and every change is itself recorded as an event.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from spenddat.models import ApprovalStatus, RuntimeMode, TxnState

GENESIS = "0" * 64

# States whose amount counts toward spend (money committed or possibly committed).
SPEND_STATES = (TxnState.EXECUTING, TxnState.COMPLETED, TxnState.NEEDS_RECONCILIATION)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_events (
    seq        INTEGER PRIMARY KEY AUTOINCREMENT,
    txn_id     TEXT NOT NULL,
    event_type TEXT NOT NULL,
    mode       TEXT NOT NULL,
    ts         TEXT NOT NULL,
    payload    TEXT NOT NULL,
    prev_hash  TEXT NOT NULL,
    hash       TEXT NOT NULL UNIQUE
);
CREATE INDEX IF NOT EXISTS ix_events_txn ON audit_events(txn_id);
CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit_events
BEGIN SELECT RAISE(ABORT, 'audit_events is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit_events
BEGIN SELECT RAISE(ABORT, 'audit_events is append-only'); END;

CREATE TABLE IF NOT EXISTS transactions (
    txn_id          TEXT PRIMARY KEY,
    mode            TEXT NOT NULL,
    state           TEXT NOT NULL,
    request_text    TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    vendor          TEXT,
    category        TEXT,
    sku             TEXT,
    unit_price_minor INTEGER,
    amount_minor    INTEGER,
    vendor_ref      TEXT,
    committed_at    TEXT,
    completed_at    TEXT,
    snapshot        TEXT
);
CREATE INDEX IF NOT EXISTS ix_txn_mode_state ON transactions(mode, state);

CREATE TABLE IF NOT EXISTS approvals (
    approval_id  TEXT PRIMARY KEY,
    txn_id       TEXT NOT NULL UNIQUE,
    mode         TEXT NOT NULL,
    status       TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    expires_at   TEXT NOT NULL,
    presented    TEXT NOT NULL,
    decided_by   TEXT,
    decided_at   TEXT,
    action       TEXT,
    modification TEXT
);
"""


def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def _row_hash(prev: str, txn_id: str, event_type: str, mode: str, ts: str, payload: str) -> str:
    material = _canonical([prev, txn_id, event_type, mode, ts, payload])
    return hashlib.sha256(material.encode()).hexdigest()


@dataclass(frozen=True)
class AuditEvent:
    seq: int
    txn_id: str
    event_type: str
    mode: str
    ts: str
    payload: dict[str, Any]
    hash: str


class ChainError(RuntimeError):
    pass


class AuditStore:
    def __init__(self, path: str | Path) -> None:
        self._path = str(path)
        if self._path != ":memory:":
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self._path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL" if self._path != ":memory:" else "SELECT 1")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.executescript(_SCHEMA)

    # ------------------------------------------------------------------ transactions

    @contextmanager
    def atomic(self) -> Iterator[sqlite3.Connection]:
        """Serialised write transaction (BEGIN IMMEDIATE) for check-then-write sequences."""
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield self._db
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            else:
                self._db.execute("COMMIT")

    def append(
        self, txn_id: str, event_type: str, mode: RuntimeMode, ts: datetime, payload: dict
    ) -> AuditEvent:
        """Append one event. Must be called inside ``atomic()`` or opens its own."""
        with self._lock:
            if self._db.in_transaction:
                return self._append(txn_id, event_type, mode, ts, payload)
            with self.atomic():
                return self._append(txn_id, event_type, mode, ts, payload)

    def _append(
        self, txn_id: str, event_type: str, mode: RuntimeMode, ts: datetime, payload: dict
    ) -> AuditEvent:
        row = self._db.execute("SELECT hash FROM audit_events ORDER BY seq DESC LIMIT 1").fetchone()
        prev = row["hash"] if row else GENESIS
        ts_s = ts.isoformat()
        body = _canonical(payload)
        h = _row_hash(prev, txn_id, event_type, mode.value, ts_s, body)
        cur = self._db.execute(
            "INSERT INTO audit_events(txn_id,event_type,mode,ts,payload,prev_hash,hash) "
            "VALUES (?,?,?,?,?,?,?)",
            (txn_id, event_type, mode.value, ts_s, body, prev, h),
        )
        return AuditEvent(cur.lastrowid or 0, txn_id, event_type, mode.value, ts_s, payload, h)

    def events(self, txn_id: str | None = None) -> list[AuditEvent]:
        q = "SELECT * FROM audit_events"
        args: tuple = ()
        if txn_id:
            q += " WHERE txn_id=?"
            args = (txn_id,)
        rows = self._db.execute(q + " ORDER BY seq", args).fetchall()
        return [
            AuditEvent(
                r["seq"],
                r["txn_id"],
                r["event_type"],
                r["mode"],
                r["ts"],
                json.loads(r["payload"]),
                r["hash"],
            )
            for r in rows
        ]

    def verify_chain(self) -> int:
        """Recompute every hash. Returns the number of verified events; raises on tampering."""
        prev = GENESIS
        n = 0
        for r in self._db.execute("SELECT * FROM audit_events ORDER BY seq"):
            if r["prev_hash"] != prev:
                raise ChainError(f"chain broken at seq {r['seq']}: prev_hash mismatch")
            expect = _row_hash(prev, r["txn_id"], r["event_type"], r["mode"], r["ts"], r["payload"])
            if expect != r["hash"]:
                raise ChainError(f"chain broken at seq {r['seq']}: content hash mismatch")
            prev = r["hash"]
            n += 1
        return n

    # ------------------------------------------------------------------ projection

    def create_txn(self, txn_id: str, mode: RuntimeMode, request_text: str, now: datetime) -> None:
        self._db.execute(
            "INSERT INTO transactions(txn_id,mode,state,request_text,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?)",
            (
                txn_id,
                mode.value,
                TxnState.RECEIVED.value,
                request_text,
                now.isoformat(),
                now.isoformat(),
            ),
        )

    def update_txn(self, txn_id: str, now: datetime, **fields: Any) -> None:
        allowed = {
            "state",
            "vendor",
            "category",
            "sku",
            "unit_price_minor",
            "amount_minor",
            "vendor_ref",
            "committed_at",
            "completed_at",
            "snapshot",
        }
        bad = set(fields) - allowed
        if bad:
            raise ValueError(f"unknown txn fields {bad}")
        sets = {k: (v.value if isinstance(v, TxnState) else v) for k, v in fields.items()}
        sets["updated_at"] = now.isoformat()
        cols = ",".join(f"{k}=?" for k in sets)
        self._db.execute(
            f"UPDATE transactions SET {cols} WHERE txn_id=?",  # noqa: S608
            (*sets.values(), txn_id),
        )

    def get_txn(self, txn_id: str) -> sqlite3.Row | None:
        return self._db.execute("SELECT * FROM transactions WHERE txn_id=?", (txn_id,)).fetchone()

    def list_txns(self, mode: RuntimeMode | None = None, limit: int = 50) -> list[sqlite3.Row]:
        if mode:
            return self._db.execute(
                "SELECT * FROM transactions WHERE mode=? ORDER BY created_at DESC LIMIT ?",
                (mode.value, limit),
            ).fetchall()
        return self._db.execute(
            "SELECT * FROM transactions ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()

    def txns_in_state(self, mode: RuntimeMode, *states: TxnState) -> list[sqlite3.Row]:
        marks = ",".join("?" * len(states))
        return self._db.execute(
            f"SELECT * FROM transactions WHERE mode=? AND state IN ({marks})",  # noqa: S608
            (mode.value, *(s.value for s in states)),
        ).fetchall()

    # ------------------------------------------------------------------ spend queries

    def spend_since(self, mode: RuntimeMode, since: datetime) -> int:
        marks = ",".join("?" * len(SPEND_STATES))
        row = self._db.execute(
            f"SELECT COALESCE(SUM(amount_minor),0) AS s FROM transactions "  # noqa: S608
            f"WHERE mode=? AND state IN ({marks}) AND committed_at >= ?",
            (mode.value, *(s.value for s in SPEND_STATES), since.isoformat()),
        ).fetchone()
        return int(row["s"])

    def purchases_since(self, mode: RuntimeMode, since: datetime) -> int:
        marks = ",".join("?" * len(SPEND_STATES))
        row = self._db.execute(
            f"SELECT COUNT(*) AS c FROM transactions "  # noqa: S608
            f"WHERE mode=? AND state IN ({marks}) AND committed_at >= ?",
            (mode.value, *(s.value for s in SPEND_STATES), since.isoformat()),
        ).fetchone()
        return int(row["c"])

    def reference_price(
        self,
        mode: RuntimeMode,
        vendor: str,
        sku: str | None,
        category: str,
        since: datetime,
        min_history: int,
    ) -> int | None:
        """Rolling average unit price of completed purchases: same SKU, else same category."""
        for col, val in (("sku", sku), ("category", category)):
            if val is None:
                continue
            rows = self._db.execute(
                f"SELECT unit_price_minor FROM transactions WHERE mode=? AND state=? "  # noqa: S608
                f"AND {col}=? AND unit_price_minor IS NOT NULL AND completed_at >= ?",
                (mode.value, TxnState.COMPLETED.value, val, since.isoformat()),
            ).fetchall()
            if len(rows) >= min_history:
                return round(sum(r["unit_price_minor"] for r in rows) / len(rows))
        return None

    # ------------------------------------------------------------------ approvals

    def create_approval(
        self,
        approval_id: str,
        txn_id: str,
        mode: RuntimeMode,
        now: datetime,
        timeout: timedelta,
        presented: dict,
    ) -> None:
        self._db.execute(
            "INSERT INTO approvals(approval_id,txn_id,mode,status,created_at,expires_at,presented)"
            " VALUES (?,?,?,?,?,?,?)",
            (
                approval_id,
                txn_id,
                mode.value,
                ApprovalStatus.PENDING.value,
                now.isoformat(),
                (now + timeout).isoformat(),
                _canonical(presented),
            ),
        )

    def get_approval(self, approval_id: str) -> sqlite3.Row | None:
        return self._db.execute(
            "SELECT * FROM approvals WHERE approval_id=?", (approval_id,)
        ).fetchone()

    def approval_for_txn(self, txn_id: str) -> sqlite3.Row | None:
        return self._db.execute("SELECT * FROM approvals WHERE txn_id=?", (txn_id,)).fetchone()

    def decide_approval(
        self,
        approval_id: str,
        status: ApprovalStatus,
        decided_by: str,
        now: datetime,
        action: str,
        modification: dict | None,
    ) -> bool:
        cur = self._db.execute(
            "UPDATE approvals SET status=?, decided_by=?, decided_at=?, action=?, modification=? "
            "WHERE approval_id=? AND status=?",
            (
                status.value,
                decided_by,
                now.isoformat(),
                action,
                _canonical(modification) if modification else None,
                approval_id,
                ApprovalStatus.PENDING.value,
            ),
        )
        return cur.rowcount == 1

    def pending_approvals(self, mode: RuntimeMode | None = None) -> list[sqlite3.Row]:
        q = "SELECT * FROM approvals WHERE status=?"
        args: list = [ApprovalStatus.PENDING.value]
        if mode:
            q += " AND mode=?"
            args.append(mode.value)
        return self._db.execute(q + " ORDER BY created_at", args).fetchall()

    def close(self) -> None:
        self._db.close()
