# ADR-0011: Escalations without a concrete item go back to the requester

**Status:** accepted · 2026-09-29

The PRD requires both that missing data (no match, missing address, missing price) ESCALATE
and that an approver is never shown a vague or unresolved request. When there is no resolved
item there is nothing concrete to approve, so the ESCALATE decision is recorded in the audit
log and the transaction goes to `needs_clarification` with the specific reasons, asking the
requester to supply the missing data.

Ambiguous matches (several plausible products) do go to a human: the best candidate is fully
resolved and presented, with the alternatives listed, and `ambiguous_product_match` as the
reason.
