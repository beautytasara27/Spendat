"""Runtime configuration, mode selection, startup checks and dependency wiring.

Mode is chosen with ``SPENDDAT_MODE=sandbox|live`` (default: sandbox). The same code runs
in both; mode only selects which credentials and endpoints are loaded. Live mode fails
loudly at startup - it never falls back to sandbox - if anything is missing or looks
like a test credential.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

import yaml

from spenddat.approval import ApprovalService, ConsoleNotifier, Notifier, WebhookNotifier
from spenddat.audit import AuditStore
from spenddat.guards import HardLimits
from spenddat.intent import AnthropicClient, IntentParser, LlmClient
from spenddat.models import Address, RuntimeMode
from spenddat.orchestrator import Orchestrator
from spenddat.payments import FakePaymentProvider, PaymentProvider, StripeSharedPaymentTokens
from spenddat.policy import PolicyEngine, load_policy
from spenddat.secrets import SecretMissing, SecretsProvider, build_secrets, require
from spenddat.vendors import build_adapter
from spenddat.vendors.base import AdapterRegistry

LIVE_CONFIRM_VALUE = "I_ACCEPT_REAL_PURCHASES"
DEFAULT_LLM_MODEL = "claude-sonnet-5-5"


class StartupError(RuntimeError):
    pass


@dataclass(frozen=True)
class Settings:
    mode: RuntimeMode
    db_path: str
    policy_file: str
    vendors_file: str
    profile_file: str
    hard_limits: HardLimits
    payment_backend: str
    llm_model: str

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Settings:
        e = env if env is not None else os.environ
        raw_mode = e.get("SPENDDAT_MODE", "sandbox").strip().lower()
        try:
            mode = RuntimeMode(raw_mode)
        except ValueError as err:
            raise StartupError(
                f"SPENDDAT_MODE must be 'sandbox' or 'live', got {raw_mode!r}"
            ) from err
        try:
            limits = HardLimits(
                max_transaction_minor=int(e.get("SPENDDAT_HARD_MAX_TRANSACTION_MINOR", "20000")),
                max_daily_minor=int(e.get("SPENDDAT_HARD_MAX_DAILY_MINOR", "30000")),
                max_weekly_minor=int(e.get("SPENDDAT_HARD_MAX_WEEKLY_MINOR", "75000")),
            )
        except ValueError as err:
            raise StartupError(f"invalid hard limit: {err}") from err
        return cls(
            mode=mode,
            db_path=e.get("SPENDDAT_DB", "data/spenddat.db"),
            policy_file=e.get("SPENDDAT_POLICY_FILE", "policies/default.yaml"),
            vendors_file=e.get("SPENDDAT_VENDORS_FILE", "config/vendors.yaml"),
            profile_file=e.get("SPENDDAT_PROFILE_FILE", "config/profile.yaml"),
            hard_limits=limits,
            payment_backend=e.get("SPENDDAT_PAYMENT_BACKEND", "stripe"),
            llm_model=e.get("SPENDDAT_LLM_MODEL", DEFAULT_LLM_MODEL),
        )


def _load_yaml(path: str, what: str) -> dict[str, Any]:
    p = Path(path)
    if not p.exists():
        raise StartupError(f"{what} file not found: {path}")
    data = yaml.safe_load(p.read_text()) or {}
    if not isinstance(data, dict):
        raise StartupError(f"{what} file must be a mapping: {path}")
    return data


def startup_checks(settings: Settings, secrets: SecretsProvider, env: dict[str, str]) -> None:
    """Fail loudly on any misconfiguration. Stricter in live mode."""
    problems: list[str] = []
    live = settings.mode is RuntimeMode.LIVE
    if live and env.get("SPENDDAT_LIVE_CONFIRM") != LIVE_CONFIRM_VALUE:
        problems.append(f"live mode requires SPENDDAT_LIVE_CONFIRM={LIVE_CONFIRM_VALUE}")
    if settings.payment_backend not in ("stripe", "fake"):
        problems.append(f"unknown SPENDDAT_PAYMENT_BACKEND {settings.payment_backend!r}")
    if live and settings.payment_backend != "stripe":
        problems.append("live mode requires SPENDDAT_PAYMENT_BACKEND=stripe")
    if settings.payment_backend == "stripe":
        key = secrets.get("stripe_secret_key") or ""
        want = "live" if live else "test"
        if not key:
            problems.append("missing secret stripe_secret_key")
        elif not key.startswith((f"sk_{want}_", f"rk_{want}_")):
            problems.append(f"stripe_secret_key is not a {want}-mode key (sk_{want}_/rk_{want}_)")
        pm = secrets.get("stripe_payment_method_id") or ""
        if not pm.startswith("pm_"):
            problems.append("missing or malformed secret stripe_payment_method_id (pm_...)")
    if not secrets.get("anthropic_api_key"):
        problems.append("missing secret anthropic_api_key (intent parsing)")
    try:
        vendors = _load_yaml(settings.vendors_file, "vendors").get("vendors", [])
        for v in vendors:
            section = v.get(settings.mode.value)
            if not section:
                problems.append(f"vendor {v.get('id')} has no '{settings.mode.value}' section")
                continue
            if live:
                for url_key in ("base_url", "feed_url"):
                    if not str(section.get(url_key, "")).startswith("https://"):
                        problems.append(f"vendor {v['id']}: live {url_key} must be https")
                if "test" in str(section.get("seller_profile", "")):
                    problems.append(f"vendor {v['id']}: live seller_profile looks like a test id")
            placeholders = [k for k, val in section.items() if "REPLACE" in str(val)]
            if placeholders:
                problems.append(f"vendor {v.get('id')}: placeholder values in {placeholders}")
            secret_name = section.get("api_key_secret")
            if secret_name and not secrets.get(secret_name):
                problems.append(f"vendor {v['id']}: missing secret {secret_name}")
        _load_yaml(settings.profile_file, "profile")
    except StartupError as e:
        problems.append(str(e))
    if problems:
        raise StartupError(
            f"refusing to start in {settings.mode.value.upper()} mode:\n  - "
            + "\n  - ".join(problems)
        )


def load_profile(path: str) -> tuple[Address | None, dict[str, str] | None]:
    data = _load_yaml(path, "profile")
    ship = Address.model_validate(data["ship_to"]) if data.get("ship_to") else None
    buyer = data.get("buyer")
    return ship, buyer


def build_orchestrator(
    env: dict[str, str] | None = None,
    *,
    llm: LlmClient | None = None,
    payments: PaymentProvider | None = None,
    registry: AdapterRegistry | None = None,
    notifiers: list[Notifier] | None = None,
) -> Orchestrator:
    e = dict(env if env is not None else os.environ)
    settings = Settings.from_env(e)
    secrets = build_secrets(settings.mode, e)
    if settings.mode is RuntimeMode.LIVE or llm is None or payments is None or registry is None:
        startup_checks(settings, secrets, e)

    policy = load_policy(settings.policy_file)
    ship_to, buyer = load_profile(settings.profile_file)
    store = AuditStore(settings.db_path)

    if registry is None:
        registry = AdapterRegistry()
        for v in _load_yaml(settings.vendors_file, "vendors").get("vendors", []):
            registry.register(build_adapter(v, settings.mode, secrets, buyer))

    if payments is None:
        if settings.payment_backend == "fake":
            payments = FakePaymentProvider(settings.mode)
        else:
            payments = StripeSharedPaymentTokens(
                settings.mode,
                require(secrets, "stripe_secret_key"),
                require(secrets, "stripe_payment_method_id"),
            )

    if llm is None:
        llm = AnthropicClient(require(secrets, "anthropic_api_key"), settings.llm_model)

    if notifiers is None:
        notifiers = [ConsoleNotifier()]
        webhook = secrets.get("approval_webhook_url")
        if webhook:
            notifiers.append(WebhookNotifier(webhook))

    supported_categories = policy.categories.allow | policy.categories.deny
    parser = IntentParser(
        llm,
        supported_vendors=set(registry.vendor_ids()) | policy.vendors.deny,
        autonomous_vendors=policy.vendors.allow,
        supported_categories=supported_categories,
    )
    approvals = ApprovalService(
        store, notifiers, timedelta(minutes=policy.approval.timeout_minutes)
    )
    return Orchestrator(
        mode=settings.mode,
        store=store,
        parser=parser,
        engine=PolicyEngine(policy),
        approvals=approvals,
        registry=registry,
        payments=payments,
        hard_limits=settings.hard_limits,
        ship_to=ship_to,
    )


__all__ = [
    "LIVE_CONFIRM_VALUE",
    "SecretMissing",
    "Settings",
    "StartupError",
    "build_orchestrator",
    "startup_checks",
]
