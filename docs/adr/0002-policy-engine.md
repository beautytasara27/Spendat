# ADR-0002: Custom deterministic rules module instead of OPA/Rego

**Status:** accepted · 2026-09-29

## Decision
A pure Python module: `evaluate(policy, facts, context) -> Decision`. Each rule is an
independent function yielding `(ALLOW|ESCALATE|DENY, Reason)` findings; precedence is fixed
(any DENY → DENY; else any ESCALATE → ESCALATE; else ALLOW). Policy values live in versioned
YAML; the file's SHA-256 is appended to the version and recorded on every decision.

The engine performs no I/O and takes no mode; aggregates (rolling spend, velocity, reference
price) are computed by the orchestrator and passed in, so the engine is trivially testable.

## Why not OPA
OPA would add a second language and a sidecar/WASM runtime for eleven rules. The PRD's
requirements (deterministic, typed, versioned config, independently testable rules) are met
with less machinery. Swapping to OPA later only replaces `PolicyEngine.evaluate`.

## Extending
New rule type → write a `Rule` function and pass it as `extra_rules` (or add to
`DEFAULT_RULES`); new tunables → add a field to `policy/config.py`.
