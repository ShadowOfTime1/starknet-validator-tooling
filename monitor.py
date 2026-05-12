#!/home/solana/.starknet/venv/bin/python
"""Starknet validator monitor: balance, attestations, containers, sync. Alerts via Telegram."""

import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request

CONFIG_PATH = "/home/solana/.starknet/monitor_config.json"
STATE_PATH = "/home/solana/.starknet/monitor_state.json"


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


def log(msg):
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def fetch_metrics(url):
    metrics = {}
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            for line in resp.read().decode().splitlines():
                if line.startswith("#") or not line.strip():
                    continue
                name, _, value = line.partition(" ")
                key = name.split("{", 1)[0]
                try:
                    metrics[key] = float(value)
                except ValueError:
                    pass
    except Exception as e:
        log(f"metrics fetch error: {e}")
    return metrics


def rpc_call(url, method, params, timeout=10):
    body = json.dumps({"jsonrpc": "2.0", "method": method, "params": params, "id": 1}).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
            return data.get("result"), data.get("error")
    except Exception as e:
        return None, {"message": str(e)}


def container_status(name):
    try:
        out = subprocess.check_output(
            ["docker", "inspect", "--format", "{{.State.Status}}", name],
            stderr=subprocess.DEVNULL,
            timeout=10,
        ).decode().strip()
        return out
    except Exception:
        return "missing"


def balance_level(balance, cfg):
    if balance < cfg["balance_emerg_strk"]:
        return "emerg"
    if balance < cfg["balance_crit_strk"]:
        return "crit"
    if balance < cfg["balance_warn_strk"]:
        return "warn"
    return "ok"


def days_left(balance, burn_per_day=6.0):
    return balance / burn_per_day if burn_per_day > 0 else float("inf")


SEVERITY = {"ok": 0, "warn": 1, "crit": 2, "emerg": 3}


def main():
    cfg = load_config()
    state = load_state()
    name = cfg.get("validator_name", "validator")
    alerts = []

    metrics = fetch_metrics(cfg["metrics_url"])

    # 1. Operational balance
    bal = metrics.get("validator_attestation_operational_account_balance_strk")
    if bal is not None:
        cur_level = balance_level(bal, cfg)
        prev_level = state.get("balance_level", "ok")
        if SEVERITY[cur_level] > SEVERITY[prev_level]:
            icons = {"warn": "🟡", "crit": "🔴", "emerg": "⛔"}
            alerts.append(
                f"{icons[cur_level]} <b>[{name}]</b> Balance {cur_level.upper()}\n"
                f"Operational: <b>{bal:.2f} STRK</b> (~{days_left(bal):.1f} days)\n"
                f"Threshold: warn {cfg['balance_warn_strk']} / crit {cfg['balance_crit_strk']} / emerg {cfg['balance_emerg_strk']}"
            )
        elif prev_level != "ok" and cur_level == "ok":
            alerts.append(
                f"✅ <b>[{name}]</b> Balance recovered\n"
                f"Operational: <b>{bal:.2f} STRK</b> (~{days_left(bal):.1f} days)"
            )
        state["balance_level"] = cur_level
        state["balance_last"] = bal

    # 2. Missed epochs
    missed = metrics.get("validator_attestation_missed_epochs_count")
    if missed is not None:
        prev = state.get("missed_count", missed)
        if missed > prev:
            delta = int(missed - prev)
            alerts.append(
                f"🚨 <b>[{name}]</b> Missed epoch(s)!\n"
                f"New misses: <b>+{delta}</b> (total {int(missed)})"
            )
        state["missed_count"] = missed

    # 3. Failed attestations
    failed = metrics.get("validator_attestation_attestation_failure_count")
    if failed is not None:
        prev = state.get("failed_count", failed)
        if failed > prev:
            delta = int(failed - prev)
            alerts.append(
                f"🚨 <b>[{name}]</b> Attestation failure(s)!\n"
                f"New failures: <b>+{delta}</b> (total {int(failed)})"
            )
        state["failed_count"] = failed

    # 4. Containers
    container_state = state.get("containers", {})
    new_container_state = {}
    for c in cfg["containers"]:
        cur = container_status(c)
        new_container_state[c] = cur
        prev = container_state.get(c)
        if prev is not None and prev != cur:
            if cur == "running":
                alerts.append(f"✅ <b>[{name}]</b> Container <code>{c}</code> back to running")
            else:
                alerts.append(f"⚠️ <b>[{name}]</b> Container <code>{c}</code> is <b>{cur}</b> (was {prev})")
        elif prev is None and cur != "running":
            alerts.append(f"⚠️ <b>[{name}]</b> Container <code>{c}</code> is <b>{cur}</b>")
    state["containers"] = new_container_state

    # 5. Pathfinder sync lag (compare to public RPC)
    local_block, _ = rpc_call(cfg["rpc_url"], "starknet_blockNumber", [])
    public_block, _ = rpc_call(cfg["public_rpc_url"], "starknet_blockNumber", [])
    if isinstance(local_block, int) and isinstance(public_block, int):
        lag = public_block - local_block
        was_lagging = state.get("lag_alerted", False)
        if lag > cfg["block_lag_threshold"] and not was_lagging:
            alerts.append(
                f"⏰ <b>[{name}]</b> Pathfinder lagging\n"
                f"Local: {local_block}, public: {public_block} (diff <b>{lag}</b> blocks)"
            )
            state["lag_alerted"] = True
        elif lag <= cfg["block_lag_threshold"] and was_lagging:
            alerts.append(f"✅ <b>[{name}]</b> Pathfinder back in sync (diff {lag})")
            state["lag_alerted"] = False
        state["last_local_block"] = local_block

    # 6. Pathfinder stuck (block not advancing)
    last_seen_block = state.get("stuck_block")
    last_seen_at = state.get("stuck_block_at", 0)
    now = time.time()
    if isinstance(local_block, int):
        if last_seen_block == local_block:
            if now - last_seen_at > cfg["stuck_seconds"] and not state.get("stuck_alerted"):
                alerts.append(
                    f"⏸️ <b>[{name}]</b> Pathfinder stuck\n"
                    f"Block <code>{local_block}</code> hasn't advanced for {int(now - last_seen_at)}s"
                )
                state["stuck_alerted"] = True
        else:
            if state.get("stuck_alerted"):
                alerts.append(f"✅ <b>[{name}]</b> Pathfinder advancing again (block {local_block})")
            state["stuck_block"] = local_block
            state["stuck_block_at"] = now
            state["stuck_alerted"] = False

    # Send all alerts
    for a in alerts:
        tg_send(cfg, a)
        log(f"alert sent: {a.splitlines()[0]}")

    save_state(state)

    if not alerts:
        log("ok")


if __name__ == "__main__":
    main()
