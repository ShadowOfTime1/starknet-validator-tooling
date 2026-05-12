#!/home/solana/.starknet/venv/bin/python
"""Autotopup operational from reward when operational STRK balance is low.

Modes:
  --check     (default) Print status; never sends. Used by cron.
  --dry-run   Build the transfer call, estimate fee, but do NOT send. For manual testing.
  --execute   Build, sign, and send the transfer.

Parameters can be overridden for testing:
  --low N      Threshold STRK below which autotopup fires (default 100)
  --target N   Target STRK to fill operational up to (default 300)
  --min-interval-sec N   Minimum seconds between executions (default 3600)
"""

import argparse
import asyncio
import json
import os
import sys
import time
import urllib.parse
import urllib.request

from starknet_py.hash.selector import get_selector_from_name
from starknet_py.net.account.account import Account
from starknet_py.net.client_models import Call
from starknet_py.net.full_node_client import FullNodeClient
from starknet_py.net.models import StarknetChainId
from starknet_py.net.signer.stark_curve_signer import KeyPair, StarkCurveSigner

REWARD = "0x07e6980efc0ed4381f23c3cb54a0cc14b030b1e38f6b01ad736f74c61f36fddd"
OP = "0x07478f2ce3c80ec603ec9ddca858005663fe12f3f77c3a74d26a309ace1db7bb"
STRK = "0x04718f5a0fc34cc1af16a1cdee98ffb20c31f5cd61d6ab07201858f4287c938d"

KEY_PATH = "/home/solana/.starknet/topup.key"
CONFIG_PATH = "/home/solana/.starknet/monitor_config.json"
STATE_PATH = "/home/solana/.starknet/autotopup_state.json"
RPC_URL = "http://localhost:9545/rpc/v0_10"

DECIMALS = 18
WEI = 10 ** DECIMALS
U128_MASK = (1 << 128) - 1


def log(msg):
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] autotopup: {msg}", flush=True)


def load_config():
    with open(CONFIG_PATH) as f:
        return json.load(f)


def load_state():
    if not os.path.exists(STATE_PATH):
        return {}
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state):
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, STATE_PATH)


def tg_send(cfg, text):
    url = f"https://api.telegram.org/bot{cfg['tg_token']}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": cfg["tg_chat_id"],
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }).encode()
    try:
        with urllib.request.urlopen(url, data=data, timeout=10) as resp:
            return resp.status == 200
    except Exception as e:
        log(f"telegram error: {e}")
        return False


def load_private_key():
    with open(KEY_PATH) as f:
        raw = f.read().strip()
    return int(raw, 16) if raw.startswith("0x") else int(raw, 16)


def make_account(client):
    pk = load_private_key()
    kp = KeyPair.from_private_key(pk)
    signer = StarkCurveSigner(
        account_address=int(REWARD, 16),
        key_pair=kp,
        chain_id=StarknetChainId.MAINNET,
    )
    return Account(
        client=client,
        address=int(REWARD, 16),
        signer=signer,
        chain=StarknetChainId.MAINNET,
    )


async def balance_strk(client, address):
    sel = get_selector_from_name("balanceOf")
    res = await client.call_contract(
        Call(
            to_addr=int(STRK, 16),
            selector=sel,
            calldata=[int(address, 16)],
        )
    )
    low, high = res[0], res[1]
    return (low | (high << 128)) / WEI


async def run(args):
    cfg = load_config()
    state = load_state()
    name = cfg.get("validator_name", "validator")
    client = FullNodeClient(node_url=RPC_URL)

    bal_op = await balance_strk(client, OP)
    bal_rw = await balance_strk(client, REWARD)
    log(f"operational={bal_op:.4f} STRK, reward={bal_rw:.4f} STRK, "
        f"low={args.low}, target={args.target}, mode={args.mode}")

    if bal_op >= args.low:
        log(f"ok, operational {bal_op:.2f} >= low {args.low}")
        return 0

    # Rate limit
    last_ts = state.get("last_topup_ts", 0)
    now = time.time()
    if now - last_ts < args.min_interval_sec:
        wait = args.min_interval_sec - (now - last_ts)
        log(f"rate limited, wait {wait:.0f}s (last topup {time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(last_ts))} UTC)")
        return 0

    if bal_op >= args.target:
        log(f"refuse: op {bal_op:.2f} already >= target {args.target}, nothing to fill")
        return 0

    fill = args.target - bal_op
    amount_strk = min(fill, args.max_topup)
    amount_wei = int(round(amount_strk * WEI))
    if amount_strk < fill:
        log(f"capped to max_topup: would fill {fill:.2f} STRK but max is {args.max_topup} STRK")

    # Reward must cover amount + fee buffer
    fee_buffer_strk = 2.0
    if bal_rw < amount_strk + fee_buffer_strk:
        msg = (
            f"⛔ <b>[{name}] Autotopup BLOCKED</b>\n"
            f"Reward balance too low: {bal_rw:.2f} STRK\n"
            f"Need ≥ {amount_strk + fee_buffer_strk:.2f} STRK to topup +fee buffer"
        )
        log(msg.replace("\n", " | "))
        tg_send(cfg, msg)
        return 1

    log(f"plan: transfer {amount_strk:.4f} STRK ({amount_wei} wei) reward → operational")

    account = make_account(client)

    call = Call(
        to_addr=int(STRK, 16),
        selector=get_selector_from_name("transfer"),
        calldata=[int(OP, 16), amount_wei & U128_MASK, amount_wei >> 128],
    )

    # Build & sign invoke v3 with auto-estimated resource bounds.
    # This runs estimate_fee under the hood and embeds bounds into the signed tx.
    try:
        signed = await account.sign_invoke_v3(calls=[call], auto_estimate=True)
    except Exception as e:
        msg = f"⛔ <b>[{name}] Autotopup sign/estimate failed</b>: {type(e).__name__}: {e}"
        log(msg.replace("\n", " | "))
        if args.mode != "dry-run":
            tg_send(cfg, msg)
        return 1

    bounds = signed.resource_bounds
    fee_strk = 0.0
    try:
        for cat in ("l1_gas", "l2_gas", "l1_data_gas"):
            rb = getattr(bounds, cat, None)
            if rb is not None:
                fee_strk += (rb.max_amount * rb.max_price_per_unit) / WEI
    except Exception:
        pass
    log(f"signed v3 invoke: max_fee≈{fee_strk:.6f} STRK, nonce={signed.nonce}")

    if args.mode in ("check", "dry-run"):
        log("not executing (mode=%s)" % args.mode)
        if args.mode == "dry-run":
            print(json.dumps({
                "from": REWARD,
                "to": OP,
                "token": STRK,
                "amount_strk": amount_strk,
                "amount_wei": amount_wei,
                "max_fee_strk": fee_strk,
                "nonce": signed.nonce,
                "resource_bounds": {
                    cat: {
                        "max_amount": getattr(bounds, cat).max_amount,
                        "max_price_per_unit": getattr(bounds, cat).max_price_per_unit,
                    }
                    for cat in ("l1_gas", "l2_gas", "l1_data_gas")
                    if getattr(bounds, cat, None) is not None
                },
            }, indent=2))
        return 0

    # mode == execute: send the already-signed tx
    try:
        result = await client.send_transaction(signed)
    except Exception as e:
        msg = f"⛔ <b>[{name}] Autotopup SEND FAILED</b>: {type(e).__name__}: {e}"
        log(msg.replace("\n", " | "))
        tg_send(cfg, msg)
        return 1

    tx_hash = hex(result.transaction_hash)
    log(f"sent tx {tx_hash}")

    # Wait for L2 acceptance with timeout
    accepted = False
    for _ in range(60):  # up to ~5 min
        await asyncio.sleep(5)
        try:
            r = await client.get_transaction_receipt(result.transaction_hash)
            status = getattr(r, "finality_status", None) or getattr(r, "status", None)
            exec_status = getattr(r, "execution_status", None)
            log(f"receipt: finality={status}, exec={exec_status}")
            if exec_status and str(exec_status).endswith("REVERTED"):
                msg = (f"⛔ <b>[{name}] Autotopup REVERTED</b>\n"
                       f"Tx: <code>{tx_hash}</code>\n"
                       f"Reason: {getattr(r, 'revert_reason', 'n/a')}")
                log(msg.replace("\n", " | "))
                tg_send(cfg, msg)
                return 1
            if status and str(status).endswith("ACCEPTED_ON_L2"):
                accepted = True
                break
        except Exception:
            # Receipt may not be available yet
            continue

    state["last_topup_ts"] = now
    state["last_tx"] = tx_hash
    state["last_amount_strk"] = amount_strk
    state["last_fee_strk"] = fee_strk
    save_state(state)

    new_op = bal_op + amount_strk
    if accepted:
        msg = (f"💰 <b>[{name}] Autotopup OK</b>\n"
               f"+{amount_strk:.2f} STRK → operational (now ≈ {new_op:.2f})\n"
               f"Fee: {fee_strk:.4f} STRK\n"
               f"Tx: <code>{tx_hash}</code>")
    else:
        msg = (f"⚠️ <b>[{name}] Autotopup sent, L2 not seen yet</b>\n"
               f"Tx: <code>{tx_hash}</code>\n"
               f"Will not retry — check manually.")
    log(msg.replace("\n", " | "))
    tg_send(cfg, msg)
    return 0


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--check", action="store_const", dest="mode", const="check")
    p.add_argument("--dry-run", action="store_const", dest="mode", const="dry-run")
    p.add_argument("--execute", action="store_const", dest="mode", const="execute")
    p.set_defaults(mode="check")
    p.add_argument("--low", type=float, default=100.0)
    p.add_argument("--target", type=float, default=300.0)
    p.add_argument("--max-topup", type=float, default=100.0,
                   help="Maximum STRK to send in a single topup (default 100)")
    p.add_argument("--min-interval-sec", type=int, default=3600)
    return p.parse_args()


def main():
    args = parse_args()
    rc = asyncio.run(run(args))
    sys.exit(rc)


if __name__ == "__main__":
    main()
