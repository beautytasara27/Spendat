"""Mode selection & startup checks, audit tamper-evidence, secrets and log redaction."""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from spenddat.audit import AuditStore, ChainError
from spenddat.config import LIVE_CONFIRM_VALUE, Settings, StartupError, build_orchestrator
from spenddat.config import startup_checks as checks
from spenddat.models import RuntimeMode
from spenddat.observability import JsonFormatter, current_mode, current_txn, redact
from spenddat.policy import load_policy
from spenddat.secrets import CommandSecrets, EnvSecrets, build_secrets
from spenddat.vendors import build_adapter
from tests.conftest import POLICY_FILE, ROOT

NOW = datetime(2026, 9, 29, tzinfo=UTC)


@pytest.fixture
def files(tmp_path: Path) -> dict[str, str]:
    prof = tmp_path / "profile.yaml"
    prof.write_text((ROOT / "config" / "profile.example.yaml").read_text())
    vendors = tmp_path / "vendors.yaml"
    vendors.write_text("""
vendors:
  - id: acp_merchant
    type: acp
    sandbox: {base_url: "http://127.0.0.1:8787", feed_url: "http://127.0.0.1:8787/feed.json",
              seller_profile: profile_test_x, api_key_secret: acp_merchant_api_key}
    live: {base_url: "https://shop.example/acp", feed_url: "https://shop.example/feed.json",
           seller_profile: profile_live_x, api_key_secret: acp_merchant_api_key}
""")
    return {
        "SPENDDAT_PROFILE_FILE": str(prof),
        "SPENDDAT_VENDORS_FILE": str(vendors),
        "SPENDDAT_POLICY_FILE": str(POLICY_FILE),
        "SPENDDAT_DB": str(tmp_path / "t.db"),
    }


def _live_env(files, **over) -> dict[str, str]:
    env = {
        **files,
        "SPENDDAT_MODE": "live",
        "SPENDDAT_LIVE_CONFIRM": LIVE_CONFIRM_VALUE,
        "SPENDDAT_LIVE_STRIPE_SECRET_KEY": "rk_live_abcdef123",
        "SPENDDAT_LIVE_STRIPE_PAYMENT_METHOD_ID": "pm_live1",
        "SPENDDAT_LIVE_ANTHROPIC_API_KEY": "sk-ant-x",
        "SPENDDAT_LIVE_ACP_MERCHANT_API_KEY": "mk",
    }
    env.update(over)
    return {k: v for k, v in env.items() if v is not None}


def _check(env):
    s = Settings.from_env(env)
    checks(s, build_secrets(s.mode, env), env)


def test_default_mode_is_sandbox():
    assert Settings.from_env({}).mode is RuntimeMode.SANDBOX


def test_invalid_mode_fails():
    with pytest.raises(StartupError):
        Settings.from_env({"SPENDDAT_MODE": "production"})


def test_valid_live_configuration_passes(files):
    _check(_live_env(files))


@pytest.mark.parametrize(
    "override,msg",
    [
        ({"SPENDDAT_LIVE_CONFIRM": None}, "SPENDDAT_LIVE_CONFIRM"),
        ({"SPENDDAT_LIVE_STRIPE_SECRET_KEY": "sk_test_abcdef"}, "not a live-mode key"),
        ({"SPENDDAT_LIVE_STRIPE_SECRET_KEY": None}, "missing secret stripe_secret_key"),
        ({"SPENDDAT_LIVE_STRIPE_PAYMENT_METHOD_ID": "tok_1"}, "stripe_payment_method_id"),
        ({"SPENDDAT_LIVE_ANTHROPIC_API_KEY": None}, "anthropic_api_key"),
        ({"SPENDDAT_LIVE_ACP_MERCHANT_API_KEY": None}, "acp_merchant_api_key"),
        ({"SPENDDAT_PAYMENT_BACKEND": "fake"}, "requires SPENDDAT_PAYMENT_BACKEND=stripe"),
    ],
)
def test_live_mode_fails_loudly(files, override, msg):
    with pytest.raises(StartupError, match=msg):
        _check(_live_env(files, **override))


def test_live_mode_never_uses_sandbox_secrets(files):
    env = _live_env(files, SPENDDAT_LIVE_STRIPE_SECRET_KEY=None)
    env["SPENDDAT_SANDBOX_STRIPE_SECRET_KEY"] = "sk_test_abcdef"
    with pytest.raises(StartupError, match="stripe_secret_key"):
        _check(env)


def test_live_rejects_http_vendor_endpoints(files, tmp_path):
    v = Path(files["SPENDDAT_VENDORS_FILE"])
    v.write_text(v.read_text().replace("https://shop.example/acp", "http://shop.example/acp"))
    with pytest.raises(StartupError, match="https"):
        _check(_live_env(files))


def test_sandbox_rejects_live_key(files):
    env = {
        **files,
        "SPENDDAT_SANDBOX_STRIPE_SECRET_KEY": "sk_live_abcdef",
        "SPENDDAT_SANDBOX_STRIPE_PAYMENT_METHOD_ID": "pm_1",
        "SPENDDAT_SANDBOX_ANTHROPIC_API_KEY": "k",
        "SPENDDAT_SANDBOX_ACP_MERCHANT_API_KEY": "m",
    }
    with pytest.raises(StartupError, match="not a test-mode key"):
        _check(env)


def test_live_build_runs_checks_even_with_injected_parts(files):
    from spenddat.intent import ScriptedLlm
    from spenddat.vendors.base import AdapterRegistry

    env = _live_env(files, SPENDDAT_LIVE_CONFIRM=None)
    with pytest.raises(StartupError):
        build_orchestrator(env, llm=ScriptedLlm(), payments=object(), registry=AdapterRegistry())


def test_sandbox_build_from_config(files):
    env = {
        **files,
        "SPENDDAT_PAYMENT_BACKEND": "fake",
        "SPENDDAT_SANDBOX_ANTHROPIC_API_KEY": "sk-ant-x",
        "SPENDDAT_SANDBOX_ACP_MERCHANT_API_KEY": "m",
    }
    orch = build_orchestrator(env, notifiers=[])
    assert orch.mode is RuntimeMode.SANDBOX


def test_stub_vendor_forbidden_in_live():
    with pytest.raises(ValueError, match="live"):
        build_adapter(
            {"id": "s", "type": "stub", "live": {}},
            RuntimeMode.LIVE,
            EnvSecrets(RuntimeMode.LIVE, {}),
            None,
        )


def test_hard_limits_validated():
    with pytest.raises(StartupError):
        Settings.from_env({"SPENDDAT_HARD_MAX_DAILY_MINOR": "lots"})


# ----------------------------------------------------------------------------- secrets


def test_env_secrets_are_mode_namespaced():
    env = {"SPENDDAT_SANDBOX_X": "s", "SPENDDAT_LIVE_X": "l"}
    assert EnvSecrets(RuntimeMode.SANDBOX, env).get("x") == "s"
    assert EnvSecrets(RuntimeMode.LIVE, env).get("x") == "l"


def test_command_secrets_backend():
    c = CommandSecrets(RuntimeMode.SANDBOX, "echo value-for-{mode}-{name}")
    assert c.get("stripe_secret_key") == "value-for-sandbox-stripe_secret_key"


# ----------------------------------------------------------------------------- audit


def test_audit_log_is_append_only(tmp_path):
    s = AuditStore(tmp_path / "a.db")
    s.append("t", "e", RuntimeMode.SANDBOX, NOW, {"x": 1})
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        s._db.execute("UPDATE audit_events SET payload='{}'")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        s._db.execute("DELETE FROM audit_events")


def test_tampering_is_detected(tmp_path):
    s = AuditStore(tmp_path / "a.db")
    for i in range(3):
        s.append("t", "e", RuntimeMode.SANDBOX, NOW, {"i": i})
    assert s.verify_chain() == 3
    # An attacker with raw DB access drops the guard and edits history:
    s._db.execute("DROP TRIGGER audit_no_update")
    s._db.execute("UPDATE audit_events SET payload=? WHERE seq=2", (json.dumps({"i": 99}),))
    with pytest.raises(ChainError, match="seq 2"):
        s.verify_chain()


# ----------------------------------------------------------------------------- logging


@pytest.mark.parametrize(
    "secret",
    [
        "sk_live_51Habcdefghijk",
        "rk_test_abcdefghijk",
        "sk-ant-api03-abcdefg",
        "Bearer abcdef123456789",
        "4242 4242 4242 4242",
        "4000056655665556",
        "spt_1RgaZcFPC5QUO6",
    ],
)
def test_redaction(secret):
    assert secret not in redact(f"value={secret} end")


def test_json_logs_carry_correlation_id_and_mode():
    current_txn.set("txn_abc")
    current_mode.set("live")
    rec = logging.LogRecord(
        "spenddat.x", logging.INFO, "f", 1, "hello %s", ("sk_live_abcdefghi",), None
    )
    out = json.loads(JsonFormatter().format(rec))
    assert out["txn_id"] == "txn_abc" and out["mode"] == "live"
    assert "sk_live_abcdefghi" not in out["msg"]


def test_placeholder_vendor_config_is_rejected(files):
    v = Path(files["SPENDDAT_VENDORS_FILE"])
    v.write_text(v.read_text().replace("profile_live_x", "profile_REPLACE_ME"))
    with pytest.raises(StartupError, match="placeholder"):
        _check(_live_env(files))


# ----------------------------------------------------------------------------- model provider


def test_openrouter_key_alone_satisfies_live_checks(files):
    env = _live_env(files, SPENDDAT_LIVE_ANTHROPIC_API_KEY=None)
    env["SPENDDAT_LIVE_OPENROUTER_API_KEY"] = "sk-or-abc"
    _check(env)


def test_explicit_provider_requires_its_own_key(files):
    env = _live_env(files, SPENDDAT_LLM_PROVIDER="openrouter")
    with pytest.raises(StartupError, match="openrouter_api_key"):
        _check(env)


@pytest.mark.parametrize(
    "env,provider,model",
    [
        ({"SPENDDAT_SANDBOX_OPENROUTER_API_KEY": "k"}, "openrouter", "anthropic/claude-sonnet-5.5"),
        ({"SPENDDAT_SANDBOX_ANTHROPIC_API_KEY": "k"}, "anthropic", "claude-sonnet-5-5"),
        (
            {"SPENDDAT_SANDBOX_OPENROUTER_API_KEY": "k", "SPENDDAT_SANDBOX_ANTHROPIC_API_KEY": "k"},
            "openrouter",
            "anthropic/claude-sonnet-5.5",
        ),
        (
            {
                "SPENDDAT_SANDBOX_OPENROUTER_API_KEY": "k",
                "SPENDDAT_LLM_MODEL": "anthropic/claude-haiku-4.5",
            },
            "openrouter",
            "anthropic/claude-haiku-4.5",
        ),
        ({}, None, None),
    ],
)
def test_choose_llm(env, provider, model):
    from spenddat.llm import choose_llm

    c = choose_llm(env, EnvSecrets(RuntimeMode.SANDBOX, env))
    assert (c.provider, c.model) == (provider, model)


def test_dotenv_loading(tmp_path):
    from spenddat.dotenv import load_dotenv

    f = tmp_path / ".env"
    f.write_text(
        "# comment\nSPENDDAT_A=1\nexport SPENDDAT_B='two words'\nSPENDDAT_C=x # note\n"
        "SPENDDAT_D=keep\n\nnot a line\n"
    )
    env = {"SPENDDAT_D": "real"}
    loaded = load_dotenv(f, env)
    assert env == {
        "SPENDDAT_A": "1",
        "SPENDDAT_B": "two words",
        "SPENDDAT_C": "x",
        "SPENDDAT_D": "real",
    }
    assert "SPENDDAT_D" not in loaded
    assert load_dotenv(tmp_path / "missing", env) == []


def test_store_config_preserves_comments_and_validates(tmp_path):
    from spenddat.stores import StoreConfig, StoreConfigError

    vendors = tmp_path / "v.yaml"
    vendors.write_text((ROOT / "config" / "vendors.yaml").read_text())
    policy = tmp_path / "p.yaml"
    policy.write_text(POLICY_FILE.read_text())
    cfg = StoreConfig(vendors, policy, RuntimeMode.LIVE)
    cfg.set_trust("acp_merchant", "blocked")
    text = policy.read_text()
    assert "# Hard blocks" in text and "acp_merchant" in text.split("deny:")[1].split("\n")[0]
    assert cfg.trust_of("acp_merchant") == "blocked"
    with pytest.raises(StoreConfigError, match="https"):
        cfg.add(
            {
                "name": "Shop",
                "type": "acp",
                "base_url": "http://x.example/acp",
                "feed_url": "https://x.example/f",
                "seller_profile": "profile_abc",
            }
        )
    with pytest.raises(StoreConfigError, match="sandbox"):
        cfg.add({"name": "Demo", "type": "reference"})
    vid = cfg.add(
        {
            "name": "Real Shop",
            "type": "acp",
            "base_url": "https://x.example/acp",
            "feed_url": "https://x.example/f.json",
            "seller_profile": "profile_abc",
        }
    )
    assert vid == "real_shop" and cfg.trust_of(vid) == "ask"
    assert cfg.secret_env_name(vid) == "SPENDDAT_LIVE_REAL_SHOP_API_KEY"
    assert "# Vendor integrations" in vendors.read_text()
    load_policy(policy)  # still a valid policy
