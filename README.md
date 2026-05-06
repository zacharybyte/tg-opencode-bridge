# tg-opencode-bridge

Control OpenCode remotely from Telegram.

- Your TG bot receives messages → this bridge forwards them to a local
  `opencode serve` → replies are posted back to the chat.
- Each chat has its own "current working directory" (cwd). Switch with `/cd`.
- For each cwd the bridge lazily spawns a dedicated `opencode serve` process
  and reuses one session per cwd. Context is preserved until you `/new`.

## Prerequisites

- Python 3.10+
- `opencode` on `$PATH` (tested with 1.14.x)
- A Telegram bot token from [@BotFather](https://t.me/BotFather)

## Setup

```bash
# from the project directory:
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt  # or use the pinned versions below
./.venv/bin/pip install 'python-telegram-bot==21.6' httpx pyyaml

cp config.example.yaml config.yaml
# edit config.yaml: bot_token (required), allowed_user_ids (leave empty for
# first-run discovery), default_cwd

./.venv/bin/python opencode_tg_bridge.py
```

Open Telegram, find your bot, send `/start`. You will see:

```
user_id: 123456789
chat_id: 123456789
authorized: NO
```

Put that `user_id` into `allowed_user_ids` in `config.yaml` and restart.

## Commands

| cmd         | effect                                              |
|-------------|-----------------------------------------------------|
| `/start`    | hello + show your `user_id` / `chat_id`             |
| `/whoami`   | print your id and the current cwd                   |
| `/pwd`      | print current cwd                                   |
| `/cd <dir>` | switch cwd for this chat (accepts `~`)              |
| `/new`      | reset the opencode session for the current cwd      |
| `/stop`     | abort a running reply in the current cwd            |
| `/status`   | list running opencode servers / sessions            |
| *(text)*    | sent as a user message to opencode                  |

## Running as a background service (macOS launchd)

The launchd label is `ai.opencode.tg-bridge`, namespaced to avoid clashing
with other Telegram bridges (e.g. a Claude one) on the same machine.

```bash
bash launchd/install.sh
tail -f /tmp/opencode-tg-bridge.out /tmp/opencode-tg-bridge.err
```

Uninstall:

```bash
launchctl unload -w ~/Library/LaunchAgents/ai.opencode.tg-bridge.plist
rm ~/Library/LaunchAgents/ai.opencode.tg-bridge.plist
```

## How it works

- One `opencode serve` instance per cwd, bound to `127.0.0.1` on a random port.
  Killed when the bridge exits.
- Each TG chat keeps a persisted cwd (in `~/.local/share/opencode-tg-bridge/state.yaml`).
- On every message: look up cwd → get/spawn server → ensure session → POST
  `/session/:id/message` → format reply → send back (split into 3800-char chunks).
- Per-cwd `asyncio.Lock` so parallel messages in the same directory are
  serialized, different directories run in parallel.

## Network / proxy notes

In regions where `api.telegram.org` is blocked, set `telegram_proxy` in
`config.yaml` to an HTTP CONNECT proxy, for example:

```yaml
telegram_proxy: "http://127.0.0.1:7897"   # clash / mihomo / etc.
```

Important:

- Use **HTTP** proxy, not SOCKS. Many SOCKS proxies silently stall Telegram's
  long-polling (`getUpdates` request hangs indefinitely, no log output).
- The bridge does **not** inherit `HTTPS_PROXY` / `ALL_PROXY` from the
  environment — this is deliberate. If you had `all_proxy=socks5://...` set
  in your shell, the bridge still connects directly or via the configured
  HTTP proxy only.
- Connections to the local `opencode serve` instances are always direct
  (`127.0.0.1`), bypassing any proxy.

## Security notes

- `allowed_user_ids` is a hard whitelist. Unknown users are silently ignored.
- `/cd` is unrestricted (as you asked). Anything the user running the bridge
  can do on disk, TG messages can trigger via opencode tools. Don't expose
  this bot to anyone you wouldn't give shell access to.
- The `opencode serve` instances listen only on `127.0.0.1`. Set
  `server_password` in config if you want basic-auth on top.
- Never commit `config.yaml`. It's in `.gitignore`.

## Troubleshooting

- **"opencode serve did not become healthy"**: check `opencode` is on PATH
  and runs (`opencode --version`). Launchd inherits a tiny PATH — the plist
  template adds `~/.opencode/bin`, adjust if your binary lives elsewhere.
- **No reply ever comes**: check `/status` — if a server is marked
  `dead(rc=...)`, look at your opencode auth (run `opencode` interactively
  once and log in to your provider).
- **Long outputs get cut**: each chunk is max 3800 chars; a single reply is
  split across messages preserving paragraph boundaries.
