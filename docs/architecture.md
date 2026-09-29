# Architecture

## Components and boundaries

| Component | Module | Knows about | Never knows about |
|---|---|---|---|
| Intent parser | `intent/` | user text, supported vendor/category ids | payment data, addresses, policy caps |
| Policy engine | `policy/engine.py` | typed facts + spend aggregates | LLMs, I/O, clock, runtime mode, vendors |
| Circuit breakers | `guards.py` | hard limits, spend ledger | policy file, approvals |
| Approval service | `approval/` | escalation lifecycle, notifiers | vendors, payments |
| Orchestrator | `orchestrator.py` | the interfaces above | any specific vendor or provider |
| Vendor adapters | `vendors/` | one vendor's protocol | policy, approvals, other vendors |
| Payment service | `payments/` | Stripe SPT API | vendors' checkout details |
| Audit store | `audit.py` | events, projections | business rules |

Mode (sandbox/live) is consumed only in `config.py` (which credentials and endpoints to load)
and recorded by the orchestrator on every event. No component branches on it.

## Data flow

1. **Receive.** `request_received` is appended (with mode) before anything else happens.
2. **Parse.** The LLM returns a forced tool call; `IntentParser` validates it with pydantic and
   deterministic checks (price limits must literally appear in the text; unknown category →
   rejected with explanation; several possible vendors → clarifying question).
3. **Resolve.** The vendor adapter searches, then resolves the best candidate into an
   authoritative, priced cart (`ResolvedItem`, including shipping and tax). Nothing is charged.
4. **Decide.** The orchestrator builds `TransactionFacts` and a `SpendContext` (rolling spend,
   velocity, reference price — all per mode) and calls the pure engine. The decision, its
   reasons, the facts, the context and the policy version+hash are appended as `policy_decided`.
5. **Route.**
   * DENY → cart cancelled, user notified, done.
   * ESCALATE with a concrete item → `approval_requested` with exactly what the approver sees;
     notifiers fire. With no concrete item (no match / no address / no price) → back to the
     requester (ADR-0011).
   * ALLOW → straight to the execution gate.
6. **Approve (ESCALATE only).** Approve / deny / approve-with-modification (lower total or
   quantity only). Expiry is enforced both by `sweep` and at decision time. On approval the
   cart is re-resolved so price and stock are current, and the new total must fit the ceiling.
7. **Execute.** `_authorize` mints an `ExecutionPermit` only from a recorded ALLOW or a
   recorded human approval. `_execute` then, in one DB transaction, checks hard ceilings,
   reserves spend and appends `execution_started`. It issues a scoped SPT (capped at the total,
   bound to the seller, 15-minute expiry), completes checkout with an idempotency key, and in a
   `finally` block revokes the token.
8. **Outcome.** `execution_succeeded` / `execution_failed` / `needs_reconciliation`. A lost
   response is resolved by reading the vendor's session state, never by paying again.

## Execution gate invariants

* `_execute` is the only caller of `payments.issue` and `adapter.purchase`.
* `ExecutionPermit` cannot be constructed without a module-private seal held by `_authorize`.
* `_authorize` reads authority from the store, not from the caller.
* `_execute` only runs from `received` or `pending_approval`, so a completed or in-flight
  transaction can never be executed again.
* Retry/reconcile paths call `order_status` and `cancel` only.

Tests: `test_permit_cannot_be_forged`, `test_gate_refuses_unapproved_escalation`,
`test_gate_refuses_denied_transaction`, `test_execute_refuses_completed_transaction_replay`,
`test_lost_response_is_reconciled_not_repaid`.

## Audit model

`audit_events` is the record of truth: append-only (SQLite triggers reject UPDATE/DELETE),
hash-chained (`hash = sha256(prev_hash, txn_id, type, mode, ts, payload)`), verified by
`spenddat verify-audit`. `transactions` and `approvals` are query projections written in the
same DB transaction as the event that justifies each change.

"Recorded before execution and updated after" is implemented as two events —
`execution_started` then a terminal event — rather than an in-place update, preserving
append-only semantics.

## Durability

State lives in SQLite, not in process memory. After a crash:

* pending approvals survive and can still be decided or expire;
* `spenddat reconcile` finds `executing`/`needs_reconciliation` transactions and asks the
  vendor what happened, completing or releasing each one.

Spend held by an in-flight transaction counts toward caps until reconciled, which errs on the
side of blocking rather than overspending.
