# Starknet validator tooling

Small set of Python scripts that run alongside a Starknet validator (Pathfinder + starknet-attestation) to keep things healthy.

## Scripts

### `monitor.py`
Every-2-minutes health check. Reads attestation metrics + queries local and public RPC, then sends Telegram alerts on level changes only (no spam). Watches:

- Operational STRK balance — escalating thresholds `warn / crit / emerg`
- Missed epochs counter (`validator_attestation_missed_epochs_count`)
- Failed attestations counter (`validator_attestation_attestation_failure_count`)
- Container state (`pathfinder`, `starknet-attestation`)
- Pathfinder sync lag vs public RPC (`block_lag_threshold`)
- Pathfinder stuck block (no advance for `stuck_seconds`)

State stored in `monitor_state.json` so alerts only fire on transitions.

### `autotopup.py`
Every-10-minutes operational top-up from reward address. When operational STRK balance falls below `--low`, transfers `min(target − current, max_topup)` STRK from reward to operational via Argent v0.4 invoke v3 with auto-estimated resource bounds. Built-in rate limit (`--min-interval-sec`, default 1h) and dry-run mode.

Modes:
- `--check` (default) — print status, never send
- `--dry-run` — build + sign + estimate fee, do not send
- `--execute` — send the signed tx

### `reminder_guardian.py`
One-shot reminder triggered by a dated cron entry: checks whether the reward wallet's Argent guardian has been removed, sends a Telegram update, and removes its own cron line on completion.

## Layout

```
~/.starknet/
├── venv/                    # python venv with starknet_py
├── monitor.py
├── monitor_config.json      # secrets — NEVER commit (see .example)
├── monitor_state.json       # runtime state
├── monitor.log
├── autotopup.py
├── autotopup_state.json     # last_topup_ts, last_tx, ...
├── autotopup.log
├── reminder_guardian.py
└── topup.key                # reward private key, chmod 600 — NEVER commit
```

## Setup

```bash
python3 -m venv ~/.starknet/venv
~/.starknet/venv/bin/pip install 'starknet-py>=0.30'
cp monitor_config.example.json ~/.starknet/monitor_config.json
# edit ~/.starknet/monitor_config.json with real values
chmod 600 ~/.starknet/monitor_config.json
echo "0xYOUR_REWARD_PRIVATE_KEY" > ~/.starknet/topup.key
chmod 600 ~/.starknet/topup.key
```

Suggested crontab:

```cron
*/2  * * * * /home/$USER/.starknet/monitor.py    >> /home/$USER/.starknet/monitor.log    2>&1
*/10 * * * * /home/$USER/.starknet/autotopup.py --execute >> /home/$USER/.starknet/autotopup.log 2>&1
```

## Notes

- Tested against Pathfinder serving RPC `v0_10` (spec `0.10.2`) on `:9545`. `starknet_py` 0.30 talks to it without compatibility warnings.
- `autotopup.py` assumes Argent v0.4 with `guardian = 0`. If the reward wallet still has a guardian, the script will sign but the on-chain validation will reject.
