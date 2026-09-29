# ADR-0001: Python 3.11, pydantic, httpx, SQLite

**Status:** accepted · 2026-09-29

## Context
Safety-critical decision logic, strict typed I/O contracts, several HTTP integrations, and a
test suite that must run offline in CI.

## Decision
Python 3.11+ with pydantic v2 (typed, frozen, `extra="forbid"` models), httpx (one client
library for Stripe, Anthropic and ACP — and `MockTransport` for offline tests), PyYAML for
policy, SQLite for storage, pytest + ruff. No web framework: the CLI is the interface.

## Consequences
Three runtime dependencies. Everything is testable in-process without network. Not a
high-throughput service; a single operator's purchasing agent does not need one.
