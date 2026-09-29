# ADR-0007: Synchronous pipeline with persisted state and reconciliation

**Status:** accepted · 2026-09-29

A durable workflow engine (Temporal etc.) was considered. Volume is one operator's purchases,
and the dangerous window is a single vendor call. Chosen: a synchronous orchestrator whose
every state transition is persisted with its audit event, plus:

* idempotency keys on SPT issuance (`txn:credential`) and ACP completion (`txn_id`);
* an unknown outcome (timeout/5xx after submit, any unexpected exception) is resolved by
  reading vendor state (`order_status`) — completed → record receipt; not completed → release;
  still unknown → `needs_reconciliation`, spend held, operator alerted;
* `spenddat reconcile` does the same after a crash for anything left `executing`;
* client-supplied `--request-id` makes a whole submission idempotent.

No path retries a payment.
