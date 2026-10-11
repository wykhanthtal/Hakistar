# hakistar

HAUIGAKI x RUIJIE hybrid Telegram bot — the build running on the haki VPS instance.
Includes the 140 success-hit cap per scan and the fail-and-drop Telegram flood
handling (calls are discarded while 429-muted instead of queueing; 1s call
spacing for the first 2 minutes after a mute lifts).

## Files

- `bot.py` — the whole bot (single file). The bot token is intentionally
  left blank: set `HYBRID_BOT_TOKEN=''` in the CONFIG section, or put the
  token in a `bot_token.txt` file next to `bot.py`.
- `requirements.txt` — Python dependencies (versions as deployed).
- `proxies.txt` — empty placeholder. Add proxies in the bot with `/addproxy`
  (format `ip:port:user:pass`), or fill this file before starting. Never
  commit real proxy credentials.

Runtime files are created automatically and are NOT part of this repo:
`hauigaki.db` (users/keys/results), `exp_setting.json`,
`chkproxy_strikes.json`.

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
