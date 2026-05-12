#!/home/solana/.starknet/venv/bin/python
"""One-shot reminder: guardian escape window check. Sends Telegram, then removes its own cron entry."""

import json
import os
import subprocess
import sys
import urllib.parse
import urllib.request

CONFIG_PATH = "/home/solana/.starknet/monitor_config.json"
REWARD = "0x07e6980efc0ed4381f23c3cb54a0cc14b030b1e38f6b01ad736f74c61f36fddd"
RPC = "http://localhost:9545/rpc/v0_9"
SELF_PATH = os.path.realpath(__file__)


def tg_send(cfg, text):
    url = f"https://api.telegram.org/bot{cfg['tg_token']}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": cfg["tg_chat_id"],
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }).encode()
    with urllib.request.urlopen(url, data=data, timeout=10) as resp:
        return resp.status == 200


def rpc_call(method, params):
    body = json.dumps({"jsonrpc": "2.0", "method": method, "params": params, "id": 1}).encode()
    req = urllib.request.Request(RPC, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode()).get("result")


def get_selector(name):
    out = subprocess.check_output([
        "/home/solana/.starknet/venv/bin/python", "-c",
        f"from starknet_py.hash.selector import get_selector_from_name; print(hex(get_selector_from_name('{name}')))",
    ]).decode().strip()
    return out


def check_guardian_state():
    """Returns (guardian_set: bool, escape_ready: bool, escape_ready_at: int|None)."""
    g_sel = get_selector("get_guardian")
    g = rpc_call("starknet_call", {
        "request": {"contract_address": REWARD, "entry_point_selector": g_sel, "calldata": []},
        "block_id": "latest",
    })
    guardian_set = int(g[0], 16) != 0

    e_sel = get_selector("get_escape_and_status")
    e = rpc_call("starknet_call", {
        "request": {"contract_address": REWARD, "entry_point_selector": e_sel, "calldata": []},
        "block_id": "latest",
    })
    ready_at = int(e[0], 16) if e and len(e) >= 1 else 0
    import time
    now = int(time.time())
    return guardian_set, (ready_at > 0 and now >= ready_at), ready_at


def remove_self_from_cron():
    try:
        cur = subprocess.check_output(["crontab", "-l"]).decode()
    except subprocess.CalledProcessError:
        return False
    new = "\n".join(line for line in cur.splitlines() if "reminder_guardian.py" not in line)
    if new and not new.endswith("\n"):
        new += "\n"
    p = subprocess.run(["crontab", "-"], input=new.encode(), check=False)
    return p.returncode == 0


def main():
    with open(CONFIG_PATH) as f:
        cfg = json.load(f)

    try:
        guardian_set, escape_ready, ready_at = check_guardian_state()
    except Exception as e:
        tg_send(cfg, f"⚠️ <b>Guardian reminder error</b>: {e}\nПроверь вручную.")
        return

    if not guardian_set:
        msg = (
            "✅ <b>Guardian уже снят</b> с reward address.\n"
            "Можно включать автотопап операционки."
        )
    elif escape_ready:
        msg = (
            "🔔 <b>Guardian можно снимать прямо сейчас</b>\n"
            "Период безопасности 7 дней истёк.\n\n"
            "Открой Argent → Reward wallet → Settings → Security → Complete escape."
        )
    else:
        import time
        wait_h = max(0, (ready_at - int(time.time())) / 3600)
        msg = (
            f"⏳ Escape ещё не готов. Осталось <b>{wait_h:.1f} часов</b>.\n"
            f"Эта проверка повторится завтра."
        )
        tg_send(cfg, msg)
        return  # don't remove cron, let it run again tomorrow

    tg_send(cfg, msg)
    remove_self_from_cron()


if __name__ == "__main__":
    main()
