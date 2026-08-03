"""Tests for the live node config loader."""

from __future__ import annotations

from pathlib import Path

import pytest

from polytempo.live.config import (
    DEFAULT_LIVE_CONFIG_PATH,
    KnobConfig,
    load_live_node_config,
    resolve_live_credentials,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_checked_in_config_loads_and_is_dry_run() -> None:
    config = load_live_node_config(DEFAULT_LIVE_CONFIG_PATH)
    assert config.mode == "dry_run"
    assert config.knob.id == "es_maxedge_lead3042_v1"
    assert config.stake.fixed_usd > 0
    assert config.risk.min_price < config.risk.max_price


def test_to_trading_profiles_one_per_gate() -> None:
    config = load_live_node_config(DEFAULT_LIVE_CONFIG_PATH)
    profiles = config.to_trading_profiles()
    assert len(profiles) == len(config.knob.lead_gates)
    for profile, gate in zip(profiles, config.knob.lead_gates, strict=True):
        assert profile.entry_gate.target_lead_hours == gate
        assert profile.model_strategy == config.knob.model_strategy
        assert profile.trade_strategy == config.knob.trade_strategy
        assert profile.id.startswith(f"live_{config.knob.id}_lead")


def test_unknown_model_strategy_rejected() -> None:
    with pytest.raises(ValueError, match="model_strategy"):
        KnobConfig(
            id="x",
            model_strategy="nope",
            trade_strategy="max_edge",
            lead_gates=(30.0,),
        )


def test_unknown_trade_strategy_rejected() -> None:
    with pytest.raises(ValueError):
        KnobConfig(
            id="x",
            model_strategy="ensemble_spread",
            trade_strategy="nope",
            lead_gates=(30.0,),
        )


def test_empty_lead_gates_rejected() -> None:
    with pytest.raises(ValueError, match="lead_gates"):
        KnobConfig(
            id="x",
            model_strategy="ensemble_spread",
            trade_strategy="max_edge",
            lead_gates=(),
        )


def test_live_credentials_require_confirm_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("POLYTEMPO_LIVE_CONFIRM", raising=False)
    monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", "0xabc")
    with pytest.raises(RuntimeError, match="POLYTEMPO_LIVE_CONFIRM"):
        resolve_live_credentials()


def test_live_credentials_require_private_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POLYTEMPO_LIVE_CONFIRM", "1")
    monkeypatch.delenv("POLYMARKET_PRIVATE_KEY", raising=False)
    with pytest.raises(RuntimeError, match="POLYMARKET_PRIVATE_KEY"):
        resolve_live_credentials()


def test_live_credentials_read_optional_wallet_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("POLYTEMPO_LIVE_CONFIRM", "1")
    monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", "0xabc")
    monkeypatch.setenv("POLYMARKET_WALLET_ADDRESS", "0xwallet")
    creds = resolve_live_credentials()
    assert creds.private_key == "0xabc"
    assert creds.wallet_address == "0xwallet"

    # Absent is fine: the SDK falls back to the signer's deposit wallet.
    monkeypatch.delenv("POLYMARKET_WALLET_ADDRESS", raising=False)
    assert resolve_live_credentials().wallet_address is None
