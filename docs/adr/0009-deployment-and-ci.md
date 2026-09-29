# ADR-0009: Local/containerised process, GitHub Actions CI

**Status:** accepted · 2026-09-29

Runs as a CLI on the operator's machine or in a container; `spenddat sweep` should be run
periodically (cron) to expire approvals promptly (expiry is also enforced at decision time).
CI (`.github/workflows/ci.yml`) runs ruff and the full suite in sandbox mode with no secrets.
An optional job runs the `stripe_sandbox` tests only when Stripe **test** credentials are
configured as repository secrets. CI never has live credentials.
