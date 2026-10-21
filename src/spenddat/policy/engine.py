"""Deterministic policy engine.

Pure function of (Policy, TransactionFacts, SpendContext) -> Decision.
No I/O, no clock reads, no randomness, no LLM. Every rule is an independent function
so it can be unit-tested alone; the engine composes them with a fixed precedence:

    any DENY rule fires      -> DENY     (never reaches a human)
    else any ESCALATE fires  -> ESCALATE (human review, exception path)
    else                     -> ALLOW    (executes autonomously)

Adding a rule type = write one ``Rule`` function and pass it in ``extra_rules``
(or append it to ``DEFAULT_RULES``). Nothing else in the system changes.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence

from spenddat.models import Decision, Outcome, Reason, SpendContext, TransactionFacts
from spenddat.policy.config import Policy

Finding = tuple[Outcome, Reason]
Rule = Callable[[Policy, TransactionFacts, SpendContext], Iterable[Finding]]


def money(minor: int) -> str:
    return f"${minor / 100:,.2f}"


def _deny(code: str, detail: str) -> Finding:
    return Outcome.DENY, Reason(code=code, detail=f"{code}: {detail}")


def _esc(code: str, detail: str) -> Finding:
    return Outcome.ESCALATE, Reason(code=code, detail=f"{code}: {detail}")


# --------------------------------------------------------------------------- DENY rules


def rule_vendor_blocklist(p: Policy, t: TransactionFacts, _: SpendContext) -> Iterable[Finding]:
    if t.vendor in p.vendors.deny:
        yield _deny("vendor_blocklisted", f"vendor '{t.vendor}' is on the deny list")


def rule_category_denylist(p: Policy, t: TransactionFacts, _: SpendContext) -> Iterable[Finding]:
    if t.category in p.categories.deny:
        yield _deny("category_denied", f"category '{t.category}' is on the deny list")


def rule_malformed(p: Policy, t: TransactionFacts, _: SpendContext) -> Iterable[Finding]:
    if t.currency != p.currency:
        yield _deny(
            "currency_unsupported", f"transaction currency '{t.currency}' != policy '{p.currency}'"
        )
    if t.quantity < 1:
        yield _deny("malformed_quantity", f"quantity {t.quantity} must be >= 1")
    if t.unit_price_minor is not None and t.unit_price_minor < 0:
        yield _deny("malformed_price", "unit price is negative")
    if t.total_minor is not None and t.total_minor < 0:
        yield _deny("malformed_price", "total is negative")
    if (
        t.unit_price_minor is not None
        and t.total_minor is not None
        and t.total_minor < t.unit_price_minor * t.quantity
    ):
        yield _deny(
            "inconsistent_total",
            f"total {money(t.total_minor)} < unit {money(t.unit_price_minor)} x {t.quantity}",
        )


# --------------------------------------------------------------------------- ESCALATE rules


def rule_vendor_unrecognized(p: Policy, t: TransactionFacts, _: SpendContext) -> Iterable[Finding]:
    if t.vendor not in p.vendors.allow and t.vendor not in p.vendors.deny:
        yield _esc("vendor_not_preapproved", f"vendor '{t.vendor}' is not on the allow list")


def rule_category_unrecognized(
    p: Policy, t: TransactionFacts, _: SpendContext
) -> Iterable[Finding]:
    if t.category not in p.categories.allow and t.category not in p.categories.deny:
        yield _esc("category_not_preapproved", f"category '{t.category}' is not on the allow list")


def rule_data_quality(_: Policy, t: TransactionFacts, __: SpendContext) -> Iterable[Finding]:
    d = t.data
    if d.candidate_count == 0:
        yield _esc("no_product_match", "no product matched the request")
    elif d.ambiguous or d.candidate_count > 1:
        yield _esc(
            "ambiguous_product_match",
            f"{d.candidate_count} products matched; no single resolvable match",
        )
    if not d.has_shipping_address:
        yield _esc("missing_shipping_address", "no shipping address on file")
    if not d.has_price or t.total_minor is None:
        yield _esc("missing_price", "vendor did not return an authoritative price")


def rule_user_ceiling(_: Policy, t: TransactionFacts, __: SpendContext) -> Iterable[Finding]:
    """The user's stated limit applies to the item price x quantity (before tax/shipping)."""
    if t.user_max_price_minor is None or t.unit_price_minor is None:
        return
    subtotal = t.unit_price_minor * t.quantity
    if subtotal > t.user_max_price_minor:
        yield _esc(
            "user_max_price_exceeded",
            f"item price {money(subtotal)} > {money(t.user_max_price_minor)} limit stated "
            "in request",
        )


def rule_transaction_caps(p: Policy, t: TransactionFacts, _: SpendContext) -> Iterable[Finding]:
    if t.total_minor is None:
        return
    if t.total_minor > p.caps.per_transaction_minor:
        yield _esc(
            "price_cap_exceeded",
            f"{money(t.total_minor)} > {money(p.caps.per_transaction_minor)} per-transaction limit",
        )
    cat_cap = p.caps.per_category_minor.get(t.category)
    if cat_cap is not None and t.total_minor > cat_cap:
        yield _esc(
            "category_cap_exceeded",
            f"{money(t.total_minor)} > {money(cat_cap)} limit for category {t.category}",
        )


def rule_cumulative_caps(p: Policy, t: TransactionFacts, s: SpendContext) -> Iterable[Finding]:
    if t.total_minor is None:
        return
    day = s.daily_spend_minor + t.total_minor
    week = s.weekly_spend_minor + t.total_minor
    if day > p.caps.daily_minor:
        yield _esc(
            "daily_cap_exceeded",
            f"{money(s.daily_spend_minor)} spent today + {money(t.total_minor)} "
            f"> {money(p.caps.daily_minor)} daily limit",
        )
    if week > p.caps.weekly_minor:
        yield _esc(
            "weekly_cap_exceeded",
            f"{money(s.weekly_spend_minor)} spent this week + {money(t.total_minor)} "
            f"> {money(p.caps.weekly_minor)} weekly limit",
        )


def rule_price_anomaly(p: Policy, t: TransactionFacts, s: SpendContext) -> Iterable[Finding]:
    ref = s.reference_price_minor
    if ref is None or ref <= 0 or t.unit_price_minor is None:
        return
    deviation = abs(t.unit_price_minor - ref) / ref * 100
    if deviation > p.anomaly.price_deviation_pct:
        yield _esc(
            "price_anomaly",
            f"unit price {money(t.unit_price_minor)} deviates {deviation:.1f}% from rolling "
            f"average {money(ref)} (threshold {p.anomaly.price_deviation_pct:g}%)",
        )


def rule_velocity(p: Policy, _: TransactionFacts, s: SpendContext) -> Iterable[Finding]:
    v = p.anomaly.velocity
    if s.recent_purchase_count + 1 > v.max_purchases:
        yield _esc(
            "velocity_exceeded",
            f"{s.recent_purchase_count + 1} purchases in {v.window_minutes} min "
            f"> {v.max_purchases} allowed",
        )


def rule_merchant_confirmation(
    _: Policy, t: TransactionFacts, __: SpendContext
) -> Iterable[Finding]:
    if t.data.merchant_requires_confirmation:
        yield _esc(
            "merchant_requires_buyer_approval",
            f"store '{t.vendor}' requires you to approve every order",
        )


DEFAULT_RULES: tuple[Rule, ...] = (
    rule_vendor_blocklist,
    rule_category_denylist,
    rule_malformed,
    rule_vendor_unrecognized,
    rule_category_unrecognized,
    rule_data_quality,
    rule_user_ceiling,
    rule_transaction_caps,
    rule_cumulative_caps,
    rule_price_anomaly,
    rule_velocity,
    rule_merchant_confirmation,
)


class PolicyEngine:
    def __init__(self, policy: Policy, extra_rules: Sequence[Rule] = ()) -> None:
        self._policy = policy
        self._rules: tuple[Rule, ...] = (*DEFAULT_RULES, *extra_rules)

    @property
    def policy(self) -> Policy:
        return self._policy

    def evaluate(self, facts: TransactionFacts, context: SpendContext) -> Decision:
        findings: list[Finding] = []
        for rule in self._rules:
            findings.extend(rule(self._policy, facts, context))

        denies = tuple(r for o, r in findings if o is Outcome.DENY)
        escalations = tuple(r for o, r in findings if o is Outcome.ESCALATE)
        version = self._policy.full_version
        if denies:
            return Decision(outcome=Outcome.DENY, reasons=denies, policy_version=version)
        if escalations:
            return Decision(outcome=Outcome.ESCALATE, reasons=escalations, policy_version=version)
        return Decision(outcome=Outcome.ALLOW, reasons=(), policy_version=version)
