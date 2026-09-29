# ADR-0006: SQLite with a hash-chained, append-only event log

**Status:** accepted · 2026-09-29

SQLite (WAL) holds `audit_events` (system of record) plus `transactions` and `approvals`
projections. Events are hash-chained and guarded by UPDATE/DELETE-rejecting triggers;
`verify_chain()` detects any out-of-band edit (tested by dropping the trigger and editing a row).
Check-then-write sequences (hard ceilings + spend reservation) use `BEGIN IMMEDIATE` for
serialisation.

For stronger tamper evidence in production, periodically anchor the latest chain hash
somewhere the operator cannot rewrite (e.g. an object-lock S3 bucket). Moving to Postgres
requires only an `AuditStore` implementation.
