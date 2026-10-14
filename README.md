# hakistar

HAUIGAKI x RUIJIE hybrid Telegram bot — the build running on the haki VPS instance.
Includes the 140 success-hit cap per scan. Telegram calls are stock telebot
direct calls (Minmay-style, since 2026-10-12): the tg-flood wrapper is NOT
installed, so a 429-muted call simply fails and drops at its call site — no
queueing, no spacing lock, no mute parking.

## Files

- `bot.py` — the whole bot (single file). No token is stored in this repo.
  Token sources, in order: the `BOT_TOKEN` environment variable (e.g. a
  Railway variable), a `bot_token.txt` file next to `bot.py`, then the
  embedded `HYBRID_BOT_TOKEN=''` literal in the CONFIG section (blank here).
- `Procfile` — `worker: python bot.py` (used by Railway and similar hosts).
- `requirements.txt` — Python dependencies (versions as deployed).
- `proxies.txt` — empty placeholder. Add proxies in the bot with `/addproxy`
  (format `ip:port:user:pass`), or fill this file before starting. Never
  commit real proxy credentials.

Runtime files are created automatically and are NOT part of this repo:
`hauigaki.db` (users/keys/results), `exp_setting.json`,
`proxy_setting.json`, `chkproxy_strikes.json`.

## Proxy mode (`/proxy on|off`, admin)

Default is **ON**: scan traffic goes through `proxies.txt` only, and a scan
will not start with an empty proxy pool (the host's own IP is never used).
`/proxy off` switches the bot to **direct mode** — workers, the scan-start
session check, `/input` validation, `/code` and `/recheck` all connect
directly from the host's IP (e.g. a Railway IP) and no proxies are needed.
The setting is persisted in `proxy_setting.json` and survives restarts;
`/proxy` shows the current mode. Turning it off exposes the host IP to the
portal — use it only where that is acceptable.

## Run

```bash
python3 -m venv venv
venv/bin/pip install -r requirements.txt
pm2 start bot.py --name hauigaki-hybrid \
  --interpreter ./venv/bin/python --cwd . --no-autorestart
pm2 save
```

The bot serves a small web status page on 127.0.0.1:8100 and needs an
admin Telegram ID in `ADMIN_ID` inside `bot.py`.

## Run on Railway

1. New Project → Deploy from GitHub repo → pick this repo. Railway reads
   `requirements.txt` and the `Procfile` (`worker: python bot.py`).
2. In the service's **Variables**, set `BOT_TOKEN` to the bot token.
   Nothing else is required; `proxies.txt` can stay empty if you use
   `/proxy off` (direct mode), otherwise add proxies with `/addproxy`
   after the bot is up.
3. Note: Railway's filesystem is ephemeral — `hauigaki.db` (users, keys,
   results) resets on redeploy/restart unless you attach a Volume and run
   the bot from that mount.
