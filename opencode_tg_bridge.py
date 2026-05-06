"""
Telegram <-> OpenCode bridge.

Design:
- Each TG chat has a "current working directory" (cwd).
- For each cwd we lazily spin up `opencode serve` on an auto-picked local port,
  and reuse one opencode session per cwd.
- TG messages are sent as user messages to the current session; replies are
  posted back to the chat.

Commands (TG side):
    /start           hello + show chat id
    /whoami          print your user id and current cwd
    /cd <path>       switch current working directory (absolute or ~-expanded)
    /pwd             print current working directory
    /new             reset session for current cwd (forget context)
    /stop            abort currently running reply
    /status          show running servers / sessions

Security:
- Whitelist via config.yaml (allowed_user_ids). Messages from other users are
  silently ignored.
- `/cd` is unrestricted per user's explicit choice. Obviously only run the
  bridge under a user whose permissions you are comfortable exposing.
"""
from __future__ import annotations

import asyncio
import contextlib
import faulthandler
import logging
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import yaml
from telegram import Update
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.request import HTTPXRequest

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

CONFIG_PATH = Path(
    os.environ.get("OPENCODE_TG_CONFIG", Path(__file__).with_name("config.yaml"))
)

log = logging.getLogger("opencode-tg-bridge")


# ---------------------------------------------------------------------------
# Liveness heartbeat (for the watchdog)
# ---------------------------------------------------------------------------
# We record monotonic timestamps of the last-known-healthy activity. A separate
# watchdog thread periodically compares these against a deadline; if nothing
# has happened for too long, it writes a diagnostic dump and exits the process
# so launchd can restart us.
#
# Using a plain module global + lock is deliberately simple: avoids any
# reliance on asyncio primitives, because the very bug we're hunting may be
# the event loop itself being wedged.
class _Heartbeat:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        now = time.monotonic()
        self.last_poll_ok: float = now       # last successful getUpdates
        self.last_reply_ok: float = now      # last on_text reply sent
        self.start_time: float = now

    def mark_poll(self) -> None:
        with self._lock:
            self.last_poll_ok = time.monotonic()

    def mark_reply(self) -> None:
        with self._lock:
            self.last_reply_ok = time.monotonic()

    def snapshot(self) -> dict[str, float]:
        with self._lock:
            return {
                "last_poll_ok": self.last_poll_ok,
                "last_reply_ok": self.last_reply_ok,
                "start_time": self.start_time,
            }


HEARTBEAT = _Heartbeat()


def _dump_all_stacks(where: str) -> str:
    """Return a multi-line string containing:
    - liveness timestamps
    - every OS thread's Python stack
    - every asyncio task's stack (if an event loop exists)
    """
    lines: list[str] = []
    lines.append(f"=== stack dump: {where} @ {time.strftime('%Y-%m-%d %H:%M:%S')} ===")

    snap = HEARTBEAT.snapshot()
    now = time.monotonic()
    lines.append(
        "heartbeat: last_poll={poll:.1f}s ago, last_reply={reply:.1f}s ago, "
        "uptime={up:.0f}s".format(
            poll=now - snap["last_poll_ok"],
            reply=now - snap["last_reply_ok"],
            up=now - snap["start_time"],
        )
    )

    lines.append("--- all threads ---")
    current_frames = sys._current_frames()
    for tid, frame in current_frames.items():
        lines.append(f"# thread {tid}")
        lines.extend("  " + s.rstrip() for s in traceback.format_stack(frame))

    # asyncio tasks — must inspect the running loop if any. We can't call
    # asyncio.all_tasks() from another thread safely, so we try to find the
    # loop and access its tasks via the threadsafe interfaces available.
    try:
        # If called from within the event loop (signal handler in the main
        # thread), asyncio.all_tasks() works.
        tasks = asyncio.all_tasks()
        lines.append(f"--- asyncio tasks ({len(tasks)}) ---")
        for t in tasks:
            lines.append(f"# task: {t!r}")
            try:
                frames = t.get_stack()
                # get_stack returns frame objects; format them.
                if frames:
                    rendered = "".join(traceback.format_list(
                        traceback.extract_stack(frames[-1])
                    ))
                    lines.extend("  " + ln for ln in rendered.rstrip("\n").splitlines())
                else:
                    lines.append("  <no stack (task not running?)>")
            except Exception as e:  # noqa: BLE001
                lines.append(f"  <failed to get stack: {e}>")
    except RuntimeError:
        lines.append("--- asyncio tasks: no running loop in current thread ---")

    lines.append("=== end stack dump ===")
    return "\n".join(lines)


def _install_sigusr1_handler() -> None:
    """SIGUSR1 → dump stacks to log. Send with `kill -USR1 <pid>`."""
    def _handler(_signum: int, _frame: Any) -> None:
        dump = _dump_all_stacks("SIGUSR1")
        log.warning("\n%s", dump)
    with contextlib.suppress(Exception):
        signal.signal(signal.SIGUSR1, _handler)
    # Also register faulthandler for SIGUSR2 → low-level C stack (useful if
    # Python is stuck inside a native call).
    with contextlib.suppress(Exception):
        faulthandler.register(signal.SIGUSR2, chain=False)


def _start_watchdog(max_silence_sec: float = 300.0) -> None:
    """Background thread: if neither getUpdates nor an on_text reply has
    succeeded in the last `max_silence_sec` seconds, dump stacks and exit
    the process so launchd can restart us.

    Runs in a plain OS thread so it cannot itself be wedged by the asyncio
    loop we are trying to monitor.
    """
    def _run() -> None:
        # Give the bridge a fair startup window before the watchdog can fire.
        time.sleep(min(60.0, max_silence_sec))
        while True:
            time.sleep(30.0)
            snap = HEARTBEAT.snapshot()
            now = time.monotonic()
            silence = now - max(snap["last_poll_ok"], snap["last_reply_ok"])
            if silence <= max_silence_sec:
                continue
            # Wedged. Dump and exit.
            try:
                dump = _dump_all_stacks(f"WATCHDOG after {silence:.0f}s silence")
                log.error(
                    "watchdog: no healthy activity for %.0fs (threshold %.0fs), "
                    "dumping stacks and exiting for launchd restart\n%s",
                    silence,
                    max_silence_sec,
                    dump,
                )
            finally:
                # Force exit — can't rely on clean shutdown if the loop is stuck.
                # Exit code 77 is arbitrary non-zero so launchd's KeepAlive
                # (Crashed=true) will trigger a restart.
                os._exit(77)

    t = threading.Thread(target=_run, name="watchdog", daemon=True)
    t.start()


@dataclass
class Config:
    bot_token: str
    allowed_user_ids: list[int]
    default_cwd: str
    opencode_binary: str = "opencode"
    server_password: str = ""  # optional
    state_file: str = "~/.local/share/opencode-tg-bridge/state.yaml"
    # HTTP/HTTPS proxy for reaching api.telegram.org.
    # - "" or "none" / "direct"  -> connect directly (no proxy)
    # - e.g. "http://127.0.0.1:7897"  -> use that HTTP CONNECT proxy
    # WARNING: do NOT use socks5:// here for TG long-polling unless you have
    # tested it thoroughly — many SOCKS proxies silently stall on long polls.
    telegram_proxy: str = ""

    @classmethod
    def load(cls, path: Path) -> "Config":
        with open(path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        return cls(
            bot_token=str(raw["bot_token"]),
            allowed_user_ids=[int(x) for x in raw.get("allowed_user_ids", [])],
            default_cwd=os.path.expanduser(str(raw.get("default_cwd", str(Path.home())))),
            opencode_binary=str(raw.get("opencode_binary", "opencode")),
            server_password=str(raw.get("server_password", "") or ""),
            state_file=os.path.expanduser(
                str(raw.get("state_file", "~/.local/share/opencode-tg-bridge/state.yaml"))
            ),
            telegram_proxy=str(raw.get("telegram_proxy", "") or ""),
        )


# ---------------------------------------------------------------------------
# State persistence (chat_id -> cwd)
# ---------------------------------------------------------------------------


class State:
    def __init__(self, path: str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.chat_cwd: dict[int, str] = {}
        if self.path.exists():
            with open(self.path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            self.chat_cwd = {int(k): str(v) for k, v in (data.get("chat_cwd") or {}).items()}

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            yaml.safe_dump({"chat_cwd": self.chat_cwd}, f, allow_unicode=True)
        tmp.replace(self.path)

    def get_cwd(self, chat_id: int, default: str) -> str:
        return self.chat_cwd.get(chat_id, default)

    def set_cwd(self, chat_id: int, cwd: str) -> None:
        self.chat_cwd[chat_id] = cwd
        self.save()


# ---------------------------------------------------------------------------
# OpenCode server pool (one serve per cwd)
# ---------------------------------------------------------------------------


def _pick_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@dataclass
class ServerEntry:
    cwd: str
    port: int
    process: subprocess.Popen
    session_id: str | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)  # serialize requests per cwd
    password: str = ""

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def auth(self) -> tuple[str, str] | None:
        return ("opencode", self.password) if self.password else None


class OpenCodePool:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.servers: dict[str, ServerEntry] = {}
        self._pool_lock = asyncio.Lock()
        # Local opencode servers are on 127.0.0.1; never route them through a
        # system proxy (SOCKS/HTTP) that may be set in the environment.
        # A single message to opencode can block for minutes (tool calls,
        # long LLM responses). Health checks and new-session requests can
        # happen concurrently. httpx's default pool of 5 connections will
        # get exhausted very quickly in that case, causing PoolTimeout that
        # takes down dependent requests (incl. reaction calls on TG side).
        limits = httpx.Limits(
            max_connections=100,
            max_keepalive_connections=20,
            keepalive_expiry=60.0,
        )
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(600.0, connect=10.0, pool=30.0),
            mounts={"all://127.0.0.1": httpx.AsyncHTTPTransport(limits=limits)},
            limits=limits,
            trust_env=False,
        )

    async def close(self) -> None:
        await self.client.aclose()
        for entry in list(self.servers.values()):
            await self._kill(entry)

    async def _kill(self, entry: ServerEntry) -> None:
        log.info("stopping opencode serve for %s (pid=%s)", entry.cwd, entry.process.pid)
        with contextlib.suppress(ProcessLookupError):
            entry.process.terminate()
        try:
            entry.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError):
                entry.process.kill()

    async def _wait_healthy(self, entry: ServerEntry, timeout: float = 30.0) -> None:
        deadline = asyncio.get_event_loop().time() + timeout
        url = f"{entry.base_url}/global/health"
        last_err: Exception | None = None
        while asyncio.get_event_loop().time() < deadline:
            if entry.process.poll() is not None:
                raise RuntimeError(
                    f"opencode serve exited prematurely with code {entry.process.returncode}"
                )
            try:
                r = await self.client.get(url, auth=entry.auth)
                if r.status_code == 200 and r.json().get("healthy"):
                    return
            except Exception as e:  # noqa: BLE001
                last_err = e
            await asyncio.sleep(0.3)
        raise RuntimeError(f"opencode serve did not become healthy: {last_err}")

    async def get_server(self, cwd: str) -> ServerEntry:
        cwd = os.path.abspath(os.path.expanduser(cwd))
        if not os.path.isdir(cwd):
            raise ValueError(f"cwd does not exist or is not a directory: {cwd}")
        async with self._pool_lock:
            entry = self.servers.get(cwd)
            if entry and entry.process.poll() is None:
                return entry
            if entry:
                # dead, drop it
                self.servers.pop(cwd, None)

            port = _pick_free_port()
            env = os.environ.copy()
            password = self.cfg.server_password or ""
            if password:
                env["OPENCODE_SERVER_PASSWORD"] = password
                env["OPENCODE_SERVER_USERNAME"] = "opencode"
            log.info("spawning opencode serve in %s on port %d", cwd, port)
            proc = subprocess.Popen(
                [
                    self.cfg.opencode_binary,
                    "serve",
                    "--hostname",
                    "127.0.0.1",
                    "--port",
                    str(port),
                ],
                cwd=cwd,
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
            entry = ServerEntry(cwd=cwd, port=port, process=proc, password=password)
            try:
                await self._wait_healthy(entry)
            except Exception:
                await self._kill(entry)
                raise
            self.servers[cwd] = entry
            return entry

    async def ensure_session(self, entry: ServerEntry, reset: bool = False) -> str:
        if reset and entry.session_id:
            with contextlib.suppress(Exception):
                await self.client.delete(
                    f"{entry.base_url}/session/{entry.session_id}", auth=entry.auth
                )
            entry.session_id = None
        if entry.session_id:
            return entry.session_id
        title = f"TG: {Path(entry.cwd).name}"
        r = await self.client.post(
            f"{entry.base_url}/session", json={"title": title}, auth=entry.auth
        )
        r.raise_for_status()
        entry.session_id = r.json()["id"]
        log.info("created session %s for %s", entry.session_id, entry.cwd)
        return entry.session_id

    async def send_message(self, entry: ServerEntry, text: str) -> str:
        """Send user message to opencode, return the plain-text reply.

        Wraps the entire critical section (including the per-cwd lock) in a
        hard timeout. Without this, a wedged opencode serve process can hold
        the lock forever and block every subsequent message for that cwd.
        """
        async def _run() -> str:
            async with entry.lock:
                session_id = await self.ensure_session(entry)
                body: dict[str, Any] = {
                    "parts": [{"type": "text", "text": text}],
                }
                r = await self.client.post(
                    f"{entry.base_url}/session/{session_id}/message",
                    json=body,
                    auth=entry.auth,
                )
                r.raise_for_status()
                data = r.json()
                return _extract_text(data)

        # 10 minutes is generous for most LLM replies incl. tool calls.
        # If we blow past that, something is actually stuck.
        try:
            return await asyncio.wait_for(_run(), timeout=600.0)
        except asyncio.TimeoutError:
            # Best-effort abort on the opencode side so the stuck request gets
            # released; ignore any error from the abort itself.
            with contextlib.suppress(Exception):
                await self.abort(entry)
            raise

    async def abort(self, entry: ServerEntry) -> bool:
        if not entry.session_id:
            return False
        r = await self.client.post(
            f"{entry.base_url}/session/{entry.session_id}/abort", auth=entry.auth
        )
        return r.status_code == 200


def _extract_text(message: dict[str, Any]) -> str:
    """Pull readable text out of a message response."""
    parts = message.get("parts") or []
    out: list[str] = []
    for p in parts:
        t = p.get("type")
        if t == "text":
            txt = p.get("text") or ""
            if txt.strip():
                out.append(txt)
        elif t == "tool":
            tool = p.get("tool") or p.get("name") or "tool"
            state = (p.get("state") or {}).get("status") or ""
            out.append(f"[{tool} {state}]".strip())
        elif t == "reasoning":
            # skip: thinking text, not meant for the user
            continue
    return "\n\n".join(out).strip() or "(no text reply)"


# ---------------------------------------------------------------------------
# Telegram handlers
# ---------------------------------------------------------------------------


def _authorized(cfg: Config, user_id: int | None) -> bool:
    if not cfg.allowed_user_ids:
        return False  # fail closed if nothing configured
    return user_id in cfg.allowed_user_ids


def _chunks(text: str, limit: int = 3800) -> list[str]:
    """Split long text into TG-safe chunks, preferring blank-line boundaries."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        cut = remaining.rfind("\n\n", 0, limit)
        if cut < limit // 2:
            cut = remaining.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = limit
        chunks.append(remaining[:cut])
        remaining = remaining[cut:].lstrip("\n")
    if remaining:
        chunks.append(remaining)
    return chunks


async def _send_long(update: Update, text: str) -> None:
    for i, chunk in enumerate(_chunks(text)):
        # Wrap in <pre> if it looks code-heavy, otherwise plain
        await update.effective_chat.send_message(chunk)
        if i < len(_chunks(text)) - 1:
            await asyncio.sleep(0.1)


# Telegram's bot `setMessageReaction` only accepts a small whitelist of
# emojis. See ReactionEmoji in telegram/constants.py. Passing an emoji that
# is not in the list raises `BadRequest: Reaction is invalid`.
# We pick 3 from that whitelist that map well to request lifecycle:
REACTION_WORKING = "⚡"   # received, processing (same as tg-claude-code-bridge)
REACTION_DONE = "👌"      # finished successfully (no ✅ in TG's whitelist)
REACTION_FAILED = "👎"    # failed


async def _set_reaction(
    update: Update, ctx: ContextTypes.DEFAULT_TYPE, emoji: str | None
) -> None:
    """Set (or clear) a reaction on the user's message.

    Passing emoji=None clears the reaction. Failures are logged at debug
    level so we can diagnose issues without cluttering normal logs; the
    caller is not affected.
    """
    msg = update.message
    chat = update.effective_chat
    if not msg or not chat:
        return
    try:
        await ctx.bot.set_message_reaction(
            chat_id=chat.id,
            message_id=msg.message_id,
            reaction=[emoji] if emoji else None,
        )
    except Exception as e:  # noqa: BLE001
        log.debug("set_message_reaction(%r) failed: %s", emoji, e)


class Handlers:
    def __init__(self, cfg: Config, state: State, pool: OpenCodePool):
        self.cfg = cfg
        self.state = state
        self.pool = pool

    def guard(self, update: Update) -> bool:
        user = update.effective_user
        if not _authorized(self.cfg, user.id if user else None):
            log.warning("unauthorized user %s", user.id if user else None)
            return False
        return True

    async def cmd_start(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        chat = update.effective_chat
        hello = (
            f"hi {user.first_name if user else 'there'}\n"
            f"user_id: <code>{user.id if user else '?'}</code>\n"
            f"chat_id: <code>{chat.id}</code>\n"
            f"authorized: {'yes' if _authorized(self.cfg, user.id if user else None) else 'NO'}"
        )
        await chat.send_message(hello, parse_mode=ParseMode.HTML)

    async def cmd_whoami(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.guard(update):
            return
        chat = update.effective_chat
        cwd = self.state.get_cwd(chat.id, self.cfg.default_cwd)
        user = update.effective_user
        await chat.send_message(
            f"user_id: {user.id}\nchat_id: {chat.id}\ncwd: {cwd}",
        )

    async def cmd_pwd(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.guard(update):
            return
        chat = update.effective_chat
        cwd = self.state.get_cwd(chat.id, self.cfg.default_cwd)
        await chat.send_message(cwd)

    async def cmd_cd(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.guard(update):
            return
        chat = update.effective_chat
        if not ctx.args:
            await chat.send_message("usage: /cd <path>")
            return
        raw = " ".join(ctx.args)
        target = os.path.abspath(os.path.expanduser(raw))
        if not os.path.isdir(target):
            await chat.send_message(f"not a directory: {target}")
            return
        self.state.set_cwd(chat.id, target)
        await chat.send_message(f"cwd -> {target}")

    async def cmd_new(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.guard(update):
            return
        chat = update.effective_chat
        cwd = self.state.get_cwd(chat.id, self.cfg.default_cwd)
        try:
            entry = await self.pool.get_server(cwd)
            await self.pool.ensure_session(entry, reset=True)
            await chat.send_message(f"new session in {cwd}")
        except Exception as e:  # noqa: BLE001
            await chat.send_message(f"failed: {e}")

    async def cmd_stop(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.guard(update):
            return
        chat = update.effective_chat
        cwd = self.state.get_cwd(chat.id, self.cfg.default_cwd)
        entry = self.pool.servers.get(cwd)
        if not entry:
            await chat.send_message("no running session for this cwd")
            return
        ok = await self.pool.abort(entry)
        await chat.send_message("aborted" if ok else "no active reply to abort")

    async def cmd_status(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.guard(update):
            return
        chat = update.effective_chat
        lines = []
        for cwd, e in self.pool.servers.items():
            alive = "alive" if e.process.poll() is None else f"dead(rc={e.process.returncode})"
            lines.append(f"{cwd} :{e.port} sid={e.session_id or '-'} {alive}")
        await chat.send_message("\n".join(lines) if lines else "no servers running")

    async def on_text(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.guard(update):
            return
        chat = update.effective_chat
        text = update.message.text if update.message else ""
        if not text.strip():
            return
        cwd = self.state.get_cwd(chat.id, self.cfg.default_cwd)

        # Immediate "received, working on it" reaction on the user's message.
        await _set_reaction(update, ctx, REACTION_WORKING)
        await ctx.bot.send_chat_action(chat.id, ChatAction.TYPING)

        # periodic typing
        stop_typing = asyncio.Event()

        async def keep_typing() -> None:
            while not stop_typing.is_set():
                with contextlib.suppress(Exception):
                    await ctx.bot.send_chat_action(chat.id, ChatAction.TYPING)
                try:
                    await asyncio.wait_for(stop_typing.wait(), timeout=4.5)
                except asyncio.TimeoutError:
                    pass

        typing_task = asyncio.create_task(keep_typing())
        ok = False
        try:
            entry = await self.pool.get_server(cwd)
            reply = await self.pool.send_message(entry, text)
            ok = True
        except Exception as e:  # noqa: BLE001
            log.exception("send_message failed")
            reply = f"error: {e}"
        finally:
            stop_typing.set()
            with contextlib.suppress(Exception):
                await typing_task

        # Flip the reaction to reflect the final outcome.
        await _set_reaction(update, ctx, REACTION_DONE if ok else REACTION_FAILED)
        await _send_long(update, reply)
        # Mark liveness: a full user round-trip just succeeded/failed. The
        # watchdog uses this (together with last_poll_ok) to decide if we're
        # stuck.
        HEARTBEAT.mark_reply()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def _build_app(cfg: Config, state: State, pool: OpenCodePool) -> Application:
    # Resolve the proxy for Telegram API.
    # Priority: env OPENCODE_TG_PROXY > cfg.telegram_proxy > no proxy
    # Empty / "none" / "direct" means connect directly and IGNORE
    # HTTPS_PROXY/ALL_PROXY from the environment.
    raw_proxy = os.environ.get("OPENCODE_TG_PROXY", cfg.telegram_proxy)
    proxy_url: str | None
    if raw_proxy.strip().lower() in ("", "none", "direct"):
        proxy_url = None
    else:
        proxy_url = raw_proxy
    log.info("telegram proxy: %s", proxy_url or "(direct)")

    # Build request objects with explicit proxy control; this is necessary
    # because python-telegram-bot's default HTTPXRequest reads HTTPS_PROXY /
    # ALL_PROXY which can silently break long-polling.
    # connection_pool_size defaults to 1 in PTB which is way too small —
    # while a long-poll getUpdates holds the single slot, any reaction /
    # send_message call queues up and eventually PoolTimeouts.
    def _req(
        read_timeout: float,
        connect_timeout: float = 10.0,
        pool_size: int = 32,
    ) -> HTTPXRequest:
        kwargs: dict[str, Any] = {
            "connect_timeout": connect_timeout,
            "read_timeout": read_timeout,
            "write_timeout": 30.0,
            "pool_timeout": 30.0,
            "connection_pool_size": pool_size,
        }
        if proxy_url is not None:
            kwargs["proxy"] = proxy_url
        return HTTPXRequest(**kwargs)

    builder = ApplicationBuilder().token(cfg.bot_token)
    # Main bot client (send_message, set_reaction, etc.) — needs real
    # concurrency, so the pool is generous.
    builder = builder.request(_req(read_timeout=35.0, pool_size=32))
    # Dedicated client just for getUpdates long-polling. A single slot is
    # enough since there's only one poll in flight at a time, but keep a
    # small buffer so a slow response doesn't block a retry.
    builder = builder.get_updates_request(_req(read_timeout=35.0, pool_size=4))
    app = builder.build()
    h = Handlers(cfg, state, pool)
    app.add_handler(CommandHandler("start", h.cmd_start))
    app.add_handler(CommandHandler("whoami", h.cmd_whoami))
    app.add_handler(CommandHandler("pwd", h.cmd_pwd))
    app.add_handler(CommandHandler("cd", h.cmd_cd))
    app.add_handler(CommandHandler("new", h.cmd_new))
    app.add_handler(CommandHandler("stop", h.cmd_stop))
    app.add_handler(CommandHandler("status", h.cmd_status))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, h.on_text))

    # Global error handler. Without this, a transient TimedOut / PoolTimeout
    # (e.g. during a temporary network blip) can bubble up and PTB will stop
    # the whole application. We log-and-continue; PTB's polling loop will
    # just try the next getUpdates.
    async def _on_error(_update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        err = ctx.error
        if err is None:
            return
        log.warning("telegram error (swallowed): %s: %s", type(err).__name__, err)

    app.add_error_handler(_on_error)

    # Liveness canary: periodically call get_me() to prove the TG HTTP path
    # is alive. This is independent from user-driven traffic, so if nobody
    # messages us for hours but polling is wedged, the watchdog still fires.
    async def _canary(_app: Application) -> None:
        log.info("liveness canary started (every 45s)")
        while True:
            try:
                await asyncio.wait_for(_app.bot.get_me(), timeout=30.0)
                HEARTBEAT.mark_poll()
            except Exception as e:  # noqa: BLE001
                # Don't mark heartbeat: let the watchdog see the silence.
                log.warning("canary get_me failed: %s: %s", type(e).__name__, e)
            await asyncio.sleep(45.0)

    async def _on_startup(_app: Application) -> None:
        # asyncio.create_task (not _app.create_task) because at post_init
        # time the Application.start() hasn't been called yet, and PTB will
        # warn about untracked tasks. We just want a fire-and-forget daemon.
        asyncio.create_task(_canary(_app), name="liveness-canary")

    app.post_init = _on_startup  # type: ignore[assignment]
    return app


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("OPENCODE_TG_LOG", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if not CONFIG_PATH.exists():
        print(f"config not found: {CONFIG_PATH}", file=sys.stderr)
        print("copy config.example.yaml to config.yaml and edit it.", file=sys.stderr)
        sys.exit(2)
    cfg = Config.load(CONFIG_PATH)
    state = State(cfg.state_file)
    pool = OpenCodePool(cfg)
    app = _build_app(cfg, state, pool)

    async def _shutdown(_app: Application) -> None:
        await pool.close()

    app.post_shutdown = _shutdown  # type: ignore[assignment]

    # Handle SIGTERM from launchd gracefully
    def _sig(*_a: Any) -> None:
        log.info("got signal, stopping")
        # PTB will handle via its own loop; just raise KeyboardInterrupt-equivalent
        asyncio.get_event_loop().call_soon_threadsafe(
            lambda: app.stop_running()  # type: ignore[attr-defined]
        )

    with contextlib.suppress(Exception):
        signal.signal(signal.SIGTERM, _sig)

    # Diagnostic plumbing:
    # - SIGUSR1 → dump all Python stacks to the log (non-destructive)
    # - SIGUSR2 → dump low-level C stack via faulthandler
    # - background watchdog exits the process if nothing healthy happens
    #   for OPENCODE_TG_WATCHDOG_SEC seconds (default 300 = 5 min)
    _install_sigusr1_handler()
    _start_watchdog(
        max_silence_sec=float(os.environ.get("OPENCODE_TG_WATCHDOG_SEC", "300"))
    )

    log.info(
        "bridge starting (allowed_user_ids=%s, pid=%d, watchdog=%s s)",
        cfg.allowed_user_ids or "EMPTY!",
        os.getpid(),
        os.environ.get("OPENCODE_TG_WATCHDOG_SEC", "300"),
    )
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
