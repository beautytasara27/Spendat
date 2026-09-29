"""CLI smoke tests over a populated sandbox database."""

from __future__ import annotations

import os

import pytest

from spenddat.cli import main
from spenddat.models import TxnState
from tests.conftest import POLICY_FILE, Harness


@pytest.fixture
def populated(tmp_path, monkeypatch):
    h = Harness(tmp_path)
    ok = h.buy()
    esc = h.buy("buy headphones", product_description="noise cancelling wireless headphones")
    assert ok.state is TxnState.COMPLETED and esc.state is TxnState.PENDING_APPROVAL
    monkeypatch.setenv("SPENDDAT_DB", str(tmp_path / "audit.db"))
    monkeypatch.setenv("SPENDDAT_POLICY_FILE", str(POLICY_FILE))
    monkeypatch.setenv("SPENDDAT_MODE", "sandbox")
    return ok, esc


def test_history_and_show(populated, capsys):
    ok, _ = populated
    assert main(["history", "--mode", "sandbox"]) == 0
    out = capsys.readouterr().out
    assert ok.txn_id in out and "completed" in out and "pending_approval" in out
    assert main(["show", ok.txn_id]) == 0
    assert "execution_succeeded" in capsys.readouterr().out
    assert main(["show", "txn_missing"]) == 1


def test_approvals_listing_filters_by_mode(populated, capsys):
    _, esc = populated
    main(["approvals", "--mode", "sandbox"])
    assert esc.approval_id in capsys.readouterr().out
    main(["approvals", "--mode", "live"])
    assert "No pending approvals" in capsys.readouterr().out


def test_spend_and_verify(populated, capsys):
    assert main(["spend", "--mode", "sandbox"]) == 0
    assert "$24.98" in capsys.readouterr().out
    assert main(["verify-audit"]) == 0
    assert "audit chain OK" in capsys.readouterr().out


def test_live_check_fails_loudly(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    for k in list(os.environ):
        if k.startswith("SPENDDAT_"):
            monkeypatch.delenv(k)
    monkeypatch.setenv("SPENDDAT_MODE", "live")
    assert main(["check"]) == 78
    assert "refusing to start in LIVE mode" in capsys.readouterr().err
