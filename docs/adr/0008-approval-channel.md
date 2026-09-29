# ADR-0008: CLI approval channel plus webhook notifications

**Status:** accepted · 2026-09-29

Approvals are recorded through the CLI (`approve`, `deny`, `review` interactive prompt),
which supports approve, deny and approve-with-modification (lower total and/or quantity).
Notifications go to the console and, if `approval_webhook_url` is set, to a Slack/Teams-style
incoming webhook with the fully resolved transaction, reasons, expiry and the exact commands.

Approver identity is `--as` or the OS user; what was shown and what was decided are stored in
the `approval_decided` event. Modifications may only narrow the purchase.

A new channel is a class implementing `Notifier` (two methods); interactive channels (e.g.
Slack buttons) would call `Orchestrator.decide_approval`, the same entry point as the CLI.
