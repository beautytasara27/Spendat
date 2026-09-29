"""Policy engine: every rule independently, combinations, and edge cases.

The engine is a pure function, so these tests construct facts/context directly.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from spenddat.models import DataQuality, Outcome, SpendContext, TransactionFacts
from spenddat.policy import PolicyEngine, load_policy
from spenddat.policy.engine import DEFAULT_RULES, Reason
from tests.conftest import POLICY_FILE

NOW = datetime(2026, 9, 29, 12, tzinfo=UTC)
CLEAN = DataQuality(candidate_count=1, has_shipping_address=True, has_price=True)


@pytest.fixture(scope="module")
def engine() -> PolicyEngine:
    return PolicyEngine(load_policy(POLICY_FILE))


def facts(**kw) -> TransactionFacts:
    base = dict(
        vendor="acp_merchant",
        category="electronics_accessories",
        quantity=1,
        unit_price_minor=1999,
        total_minor=2498,
        currency="usd",
        sku="SKU-1",
        data=CLEAN,
    )
    base.update(kw)
    return TransactionFacts(**base)


def ctx(**kw) -> SpendContext:
    return SpendContext(now=NOW, **kw)


# ----------------------------------------------------------------------------- ALLOW


def test_in_policy_transaction_is_allowed(engine):
    d = engine.evaluate(facts(), ctx())
    assert d.outcome is Outcome.ALLOW
    assert d.reasons == ()
    assert d.policy_version.startswith("2026-09-29.1#")


def test_allow_at_exact_caps(engine):
    # $30.00 exactly equals the electronics cap: allowed (limits are inclusive).
    d = engine.evaluate(facts(unit_price_minor=3000, total_minor=3000), ctx())
    assert d.outcome is Outcome.ALLOW


def test_allow_uses_remaining_cumulative_headroom(engine):
    d = engine.evaluate(
        facts(total_minor=3000, unit_price_minor=3000),
        ctx(daily_spend_minor=12000, weekly_spend_minor=37000),
    )
    assert d.outcome is Outcome.ALLOW


def test_engine_is_deterministic(engine):
    f, c = facts(total_minor=9999, unit_price_minor=9999), ctx(daily_spend_minor=14000)
    assert engine.evaluate(f, c) == engine.evaluate(f, c)


# ----------------------------------------------------------------------------- DENY


def test_blocklisted_vendor_denied(engine):
    d = engine.evaluate(facts(vendor="sketchy_mart"), ctx())
    assert d.outcome is Outcome.DENY
    assert d.reason_codes == ("vendor_blocklisted",)


@pytest.mark.parametrize("category", ["gift_cards", "subscriptions", "weapons"])
def test_denylisted_category_denied(engine, category):
    d = engine.evaluate(facts(category=category), ctx())
    assert d.outcome is Outcome.DENY
    assert "category_denied" in d.reason_codes


def test_currency_mismatch_denied(engine):
    assert engine.evaluate(facts(currency="eur"), ctx()).reason_codes == ("currency_unsupported",)


def test_inconsistent_total_denied(engine):
    d = engine.evaluate(facts(quantity=3, unit_price_minor=1000, total_minor=2000), ctx())
    assert d.outcome is Outcome.DENY and d.reason_codes == ("inconsistent_total",)


def test_negative_price_denied(engine):
    d = engine.evaluate(facts(unit_price_minor=-1, total_minor=0), ctx())
    assert d.outcome is Outcome.DENY and "malformed_price" in d.reason_codes


def test_deny_takes_precedence_over_escalate(engine):
    # Blocklisted AND over every cap: DENY, never sent to a human.
    d = engine.evaluate(
        facts(vendor="sketchy_mart", total_minor=999999, unit_price_minor=999999),
        ctx(daily_spend_minor=15000),
    )
    assert d.outcome is Outcome.DENY
    assert all(r.code in ("vendor_blocklisted",) for r in d.reasons)


# ----------------------------------------------------------------------------- ESCALATE


def test_per_transaction_cap(engine):
    d = engine.evaluate(facts(category="household", unit_price_minor=5001, total_minor=5001), ctx())
    assert d.outcome is Outcome.ESCALATE and d.reason_codes == ("price_cap_exceeded",)
    assert "$50.01 > $50.00" in d.explanation()


def test_category_cap_reason_is_specific(engine):
    d = engine.evaluate(facts(unit_price_minor=3499, total_minor=3499), ctx())
    assert d.outcome is Outcome.ESCALATE
    assert d.reasons == (
        Reason(
            code="category_cap_exceeded",
            detail="category_cap_exceeded: $34.99 > $30.00 limit for "
            "category electronics_accessories",
        ),
    )


def test_daily_cumulative_cap(engine):
    d = engine.evaluate(facts(), ctx(daily_spend_minor=14000))
    assert d.reason_codes == ("daily_cap_exceeded",)


def test_weekly_cumulative_cap(engine):
    d = engine.evaluate(facts(), ctx(weekly_spend_minor=39000))
    assert d.reason_codes == ("weekly_cap_exceeded",)


def test_unrecognized_vendor(engine):
    d = engine.evaluate(facts(vendor="new_store"), ctx())
    assert d.outcome is Outcome.ESCALATE and d.reason_codes == ("vendor_not_preapproved",)


def test_unrecognized_category(engine):
    d = engine.evaluate(facts(category="garden"), ctx())
    assert d.reason_codes == ("category_not_preapproved",)


def test_no_product_match(engine):
    d = engine.evaluate(
        facts(
            unit_price_minor=None,
            total_minor=None,
            sku=None,
            data=DataQuality(candidate_count=0, has_shipping_address=True, has_price=False),
        ),
        ctx(),
    )
    assert set(d.reason_codes) == {"no_product_match", "missing_price"}


def test_ambiguous_match(engine):
    d = engine.evaluate(
        facts(
            data=DataQuality(
                candidate_count=3, has_shipping_address=True, has_price=True, ambiguous=True
            )
        ),
        ctx(),
    )
    assert d.reason_codes == ("ambiguous_product_match",)


def test_missing_shipping_address(engine):
    d = engine.evaluate(
        facts(data=DataQuality(candidate_count=1, has_shipping_address=False, has_price=True)),
        ctx(),
    )
    assert d.reason_codes == ("missing_shipping_address",)


def test_missing_price(engine):
    d = engine.evaluate(
        facts(
            total_minor=None,
            unit_price_minor=None,
            data=DataQuality(candidate_count=1, has_shipping_address=True, has_price=False),
        ),
        ctx(),
    )
    assert d.reason_codes == ("missing_price",)


def test_user_stated_ceiling_applies_to_item_price(engine):
    # $19.99 item + shipping = $24.98 total; user said "under $20" -> item price is fine.
    assert engine.evaluate(facts(user_max_price_minor=2000), ctx()).outcome is Outcome.ALLOW
    d = engine.evaluate(facts(user_max_price_minor=1500), ctx())
    assert d.reason_codes == ("user_max_price_exceeded",)


def test_price_anomaly_above_and_below(engine):
    assert engine.evaluate(facts(), ctx(reference_price_minor=1000)).reason_codes == (
        "price_anomaly",
    )
    assert engine.evaluate(
        facts(unit_price_minor=500, total_minor=999), ctx(reference_price_minor=1000)
    ).reason_codes == ("price_anomaly",)
    # within 40%
    assert engine.evaluate(facts(), ctx(reference_price_minor=1800)).outcome is Outcome.ALLOW


def test_price_anomaly_needs_history(engine):
    assert engine.evaluate(facts(), ctx(reference_price_minor=None)).outcome is Outcome.ALLOW


def test_velocity(engine):
    assert engine.evaluate(facts(), ctx(recent_purchase_count=4)).outcome is Outcome.ALLOW
    assert engine.evaluate(facts(), ctx(recent_purchase_count=5)).reason_codes == (
        "velocity_exceeded",
    )


def test_cap_and_anomaly_simultaneously_reports_both(engine):
    d = engine.evaluate(
        facts(unit_price_minor=3499, total_minor=3499),
        ctx(reference_price_minor=1500, daily_spend_minor=14000),
    )
    assert d.outcome is Outcome.ESCALATE
    assert set(d.reason_codes) == {"category_cap_exceeded", "daily_cap_exceeded", "price_anomaly"}


# ----------------------------------------------------------------------------- config


def test_policy_rejects_contradictions(tmp_path: Path):
    bad = POLICY_FILE.read_text().replace("deny: [sketchy_mart]", "deny: [acp_merchant]")
    p = tmp_path / "bad.yaml"
    p.write_text(bad)
    with pytest.raises(ValidationError, match="both allow and deny"):
        load_policy(p)


def test_policy_version_changes_with_content(tmp_path: Path):
    p = tmp_path / "p.yaml"
    p.write_text(
        POLICY_FILE.read_text().replace(
            "per_transaction_minor: 5000", "per_transaction_minor: 6000"
        )
    )
    assert load_policy(p).full_version != load_policy(POLICY_FILE).full_version


def test_custom_rule_extends_engine_without_changes(engine):
    def no_weekend(policy, t, s):
        if s.now.weekday() >= 5:
            yield Outcome.ESCALATE, Reason(code="weekend", detail="weekend: purchases reviewed")

    e = PolicyEngine(engine.policy, extra_rules=[no_weekend])
    sat = SpendContext(now=datetime(2026, 10, 3, tzinfo=UTC))
    assert e.evaluate(facts(), sat).reason_codes == ("weekend",)
    assert len(DEFAULT_RULES) == 11


def test_engine_has_no_mode_input():
    """Mode can't influence a decision: evaluate() takes no mode and the engine holds none."""
    import inspect

    params = inspect.signature(PolicyEngine.evaluate).parameters
    assert set(params) == {"self", "facts", "context"}
