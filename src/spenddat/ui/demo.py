"""Sandbox demo wiring for the UI: everything in-process, no credentials needed.

Files live in ``data/demo/`` (git-ignored) and are created on first run:

* ``stores.yaml`` - the demo store list (copied from ``ui/demo_stores.yaml``); the
  Stores page edits it,
* ``policy.yaml`` - a copy of your policy, so trust changes made in the demo never touch
  your real policy file,
* ``spenddat.db``  - the demo audit log.

Stores are bundled in-process agent-checkout stores (plus one "stub" store), payments
are the sandbox-only fake provider. Requests are understood by a real model if an
OpenRouter or Anthropic key is configured (see ``spenddat.llm``), otherwise by a small
offline keyword parser. The demo refuses to run in live mode.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from importlib import resources
from pathlib import Path

import yaml

from spenddat.approval import ApprovalService
from spenddat.audit import AuditStore
from spenddat.guards import HardLimits
from spenddat.intent import IntentParser, LlmClient
from spenddat.llm import build_llm, choose_llm
from spenddat.models import Address, ApprovalAction, Modification, RuntimeMode
from spenddat.orchestrator import Orchestrator
from spenddat.payments import FakePaymentProvider
from spenddat.policy import PolicyEngine, load_policy
from spenddat.secrets import EnvSecrets
from spenddat.stores import StoreConfig
from spenddat.ui.offline_llm import OfflineIntentLlm
from spenddat.vendors import build_adapter
from spenddat.vendors.base import AdapterRegistry

DEMO_SHIP_TO = Address(
    name="Demo Buyer",
    line_one="123 Example St",
    city="New York",
    state="NY",
    postal_code="10001",
    country="US",
)
MODE = RuntimeMode.SANDBOX


class _Clock:
    """Real time, except while seeding history into the past."""

    def __init__(self) -> None:
        self.offset = timedelta(0)

    def __call__(self) -> datetime:
        return datetime.now(UTC) + self.offset


@dataclass
class DemoApp:
    orch: Orchestrator
    label: str
    config: StoreConfig
    rebuild: Callable[[], Orchestrator]


def _prepare_files(data_dir: Path, policy_template: str, reset: bool) -> tuple[Path, Path, Path]:
    data_dir.mkdir(parents=True, exist_ok=True)
    stores = data_dir / "stores.yaml"
    policy = data_dir / "policy.yaml"
    db = data_dir / "spenddat.db"
    if reset:
        for p in (stores, policy, db, Path(f"{db}-wal"), Path(f"{db}-shm")):
            p.unlink(missing_ok=True)
    if not stores.exists():
        template = resources.files("spenddat.ui") / "demo_stores.yaml"
        stores.write_text(template.read_text(encoding="utf-8"), encoding="utf-8")
    if not policy.exists():
        shutil.copyfile(policy_template, policy)
    return stores, policy, db


def _read_stores(stores_file: Path) -> list[dict]:
    spec = yaml.safe_load(stores_file.read_text(encoding="utf-8")) or {}
    return list(spec.get("vendors") or [])


def build_demo(
    data_dir: str | Path, policy_template: str, *, seed: bool = True, reset: bool = False
) -> DemoApp:
    if os.environ.get("SPENDDAT_MODE", "sandbox").lower() == "live":
        raise RuntimeError("the UI demo runs in sandbox mode only; unset SPENDDAT_MODE=live")
    stores_file, policy_file, db = _prepare_files(Path(data_dir), policy_template, reset)
    store = AuditStore(db)
    payments = FakePaymentProvider(MODE)
    clock = _Clock()
    secrets = EnvSecrets(MODE)
    choice = choose_llm(dict(os.environ), secrets)
    real_llm: LlmClient | None = build_llm(choice, secrets) if choice.provider else None

    def make(llm_override: LlmClient | None = None) -> Orchestrator:
        policy = load_policy(policy_file)
        registry = AdapterRegistry()
        for v in _read_stores(stores_file):
            if v.get("enabled", True) and v.get(MODE.value) is not None:
                registry.register(build_adapter(v, MODE, secrets, None))
        names = registry.display_names()
        llm = (
            llm_override
            or real_llm
            or OfflineIntentLlm(MODE, registry.vendor_ids() + sorted(policy.vendors.deny), names)
        )
        parser = IntentParser(
            llm,
            supported_vendors=set(registry.vendor_ids()) | policy.vendors.deny,
            autonomous_vendors=policy.vendors.allow,
            supported_categories=policy.categories.allow | policy.categories.deny,
            vendor_names=names,
        )
        return Orchestrator(
            mode=MODE,
            store=store,
            parser=parser,
            engine=PolicyEngine(policy),
            approvals=ApprovalService(
                store, [], timedelta(minutes=policy.approval.timeout_minutes)
            ),
            registry=registry,
            payments=payments,
            hard_limits=HardLimits(20000, 30000, 75000),
            ship_to=DEMO_SHIP_TO,
            clock=clock,
        )

    if seed and not store.list_txns(MODE, 1):
        # seeding always uses the offline parser, so it never spends model credits
        vendors = _read_stores(stores_file)
        offline = OfflineIntentLlm(
            MODE,
            [str(v["id"]) for v in vendors] + ["sketchy_mart"],
            {str(v["id"]): str(v.get("name") or v["id"]) for v in vendors},
        )
        _seed(make(offline), clock)
    return DemoApp(
        orch=make(),
        label=choice.label,
        config=StoreConfig(stores_file, policy_file, MODE),
        rebuild=make,
    )


def _seed(orch: Orchestrator, clock: _Clock) -> None:
    """Replay about ten days of plausible activity through the real pipeline."""
    script: list[tuple[float, str, str | None]] = [
        (9.0, "buy a standard office stapler", None),
        (8.2, "buy a usb-c charging cable", None),
        (7.1, "buy an a5 dot grid notebook pack", None),
        (6.3, "buy a $50 gift card", None),
        (5.4, "buy noise cancelling wireless headphones", "approve"),
        (4.8, "buy a heavyweight cotton t-shirt in black size m", "approve"),
        (4.2, "buy a clear phone case for iphone 15 under $25", None),
        (3.5, "buy a felt desk mat from desk & co", "approve"),
        (2.9, "buy converse chuck taylor high tops in black size 9", "approve"),
        (2.6, "buy a stapler from sketchy mart", None),
        (1.8, "buy 3 led desk lamps", "approve_low"),
        (1.1, "buy a usb-c charging cable", None),
        (0.4, "buy a 27 inch 4k monitor from desk & co", "deny"),
        (0.0, "buy a wireless mouse", None),  # left pending for the dashboard
    ]
    for days_ago, text, action in script:
        clock.offset = -timedelta(days=days_ago)
        res = orch.submit(text)
        if res.approval_id and action:
            clock.offset += timedelta(minutes=7)
            if action == "approve":
                orch.decide_approval(res.approval_id, ApprovalAction.APPROVE, "demo reviewer")
            elif action == "approve_low":
                orch.decide_approval(
                    res.approval_id,
                    ApprovalAction.APPROVE_WITH_MODIFICATION,
                    "demo reviewer",
                    Modification(quantity=1),
                )
            elif action == "deny":
                orch.decide_approval(res.approval_id, ApprovalAction.DENY, "demo reviewer")
    clock.offset = timedelta(0)
