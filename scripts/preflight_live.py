#!/usr/bin/env python3
"""Read-only live-credential preflight. Places no orders, cancels nothing.

Answers the three questions that decide whether the node can trade, without
touching money: do the credentials authenticate, does the CLOB see collateral
for this wallet, and is that collateral enough for the configured stake.

Deliberately does NOT require ``POLYTEMPO_LIVE_CONFIRM=1`` — that interlock
guards placing orders, and an operator should be able to check funding before
arming the node. Reads ``POLYMARKET_PRIVATE_KEY`` (and optional
``POLYMARKET_WALLET_ADDRESS``) from the environment or a repo-root ``.env``.
The key is never printed.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from polytempo.live.config import (  # noqa: E402
    ENV_PRIVATE_KEY,
    ENV_WALLET_ADDRESS,
    DEFAULT_LIVE_CONFIG_PATH,
    LiveCredentials,
    load_live_node_config,
)
from polytempo.live.execution import PolymarketExecutionClient  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only live credential preflight")
    parser.add_argument("--config", type=Path, default=DEFAULT_LIVE_CONFIG_PATH)
    args = parser.parse_args()

    try:
        from dotenv import load_dotenv

        load_dotenv(REPO_ROOT / ".env")
    except ImportError:
        pass

    config = load_live_node_config(args.config)
    key = os.environ.get(ENV_PRIVATE_KEY)
    wallet = os.environ.get(ENV_WALLET_ADDRESS)

    if not key:
        print(f"{ENV_PRIVATE_KEY} is not set (put it in {REPO_ROOT / '.env'}).")
        return 1

    print(f"mode        : {config.mode}")
    print(f"knob        : {config.knob.id}")
    print(f"stake       : ${config.stake.fixed_usd:.2f}/ticket")
    print(f"signer key  : {key[:4]}...{key[-4:]} (len {len(key)})")
    print(f"wallet      : {wallet or '(unset — SDK derives from signer)'}")
    print()

    print("authenticating (derives API credentials; places nothing)...")
    try:
        client = PolymarketExecutionClient(LiveCredentials(private_key=key, wallet_address=wallet))
    except Exception as exc:  # noqa: BLE001 — this is the diagnostic
        print(f"  FAILED: {exc}")
        return 1
    print("  ok\n")

    balance = client.collateral_balance_usd()
    print(f"collateral  : {'unavailable' if balance is None else f'${balance:,.2f}'}")
    try:
        print(f"positions   : {len(client.positions())}")
        print(f"open orders : {len(client.open_orders())}")
    except Exception as exc:  # noqa: BLE001
        print(f"  position/order read failed: {exc}")

    if balance is None:
        print("\nCould not read collateral — treat as NOT ready.")
        return 1

    # The node can hold several tickets at once, so the cap is what must be
    # funded, not one stake.
    needed = config.risk.max_open_exposure_usd
    print()
    if balance >= needed:
        print(f">>> READY — ${balance:,.2f} covers max_open_exposure_usd (${needed:,.2f}).")
        return 0
    if balance >= config.stake.fixed_usd:
        print(
            f">>> PARTIAL — ${balance:,.2f} funds a ${config.stake.fixed_usd:.2f} ticket but not "
            f"the ${needed:,.2f} exposure cap; the node will open until funds run out."
        )
        return 0
    print(f">>> NOT READY — ${balance:,.2f} is below one ${config.stake.fixed_usd:.2f} ticket.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
