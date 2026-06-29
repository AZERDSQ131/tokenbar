#!/usr/bin/env python3
"""OpenCode Token Bar — OpenCode + Claude Code."""

import json
import os
import sqlite3
import re
import subprocess
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime, timezone, timedelta
from pathlib import Path

import objc
from AppKit import (
    NSApp, NSApplication, NSApplicationActivationPolicyAccessory,
    NSObject, NSPopover, NSPopoverBehaviorTransient,
    NSStatusBar, NSVariableStatusItemLength, NSViewController,
    NSView, NSMakeRect, NSSize, NSAppearance, NSVisualEffectView, NSColor,
    NSWindow, NSBackingStoreBuffered,
    NSUserNotificationCenter, NSUserNotification,
    NSWindowWillCloseNotification,
)
from WebKit import WKWebView, WKWebViewConfiguration, WKUserScript
from Foundation import NSTimer, NSURL, NSNotificationCenter

OC_DB       = Path.home() / ".local/share/opencode/opencode.db"
OC_DB_DEV   = Path.home() / ".local/share/opencode/opencode-dev.db"
CC_DIR      = Path.home() / ".claude/projects"
CODEX_DB    = Path.home() / ".codex/state_5.sqlite"
OC_WF_DIR   = Path.home() / ".config/opencode/workflows"

W, H   = 360, 320
DEFAULT_REFRESH = 15.0

DEFAULT_EXCLUDED = {"qwen122b", "qwen3.5"}

SETTINGS_FILE = Path.home() / ".tokenbar_settings.json"
_SETTINGS = {}

_cc_cache = {"ts": 0.0, "data": None}
_ds_balance_cache = {"ts": 0.0, "data": None}
_limits_cache = {"ts": 0.0, "data": None, "fetching": False}
_git_cache = {"ts": 0.0, "heatmap": {}, "fetching": False}
_start_file = Path.home() / ".tokenbar_start"
if not _start_file.exists():
    _start_file.write_text(str(int(time.time())))
START_S = float(_start_file.read_text().strip())


def load_settings():
    global _SETTINGS
    _SETTINGS = {"excluded_models": list(DEFAULT_EXCLUDED),
                  "refresh_interval": DEFAULT_REFRESH,
                  "chart_style": "bars", "chart_period": "1m",
                  "custom_rates": {},
                  "notify_enabled": False,
                  "notify_time": "20:00",
                  "login_start": False,
                  "alerts": [],
                  "deepseek_api_key": "",
                  "monthly_limit_usd": 0}
    try:
        if SETTINGS_FILE.exists():
            d = json.loads(SETTINGS_FILE.read_text())
            _SETTINGS.update(d)
    except: pass

def save_settings(d):
    global _SETTINGS
    _SETTINGS.update(d)
    try:
        SETTINGS_FILE.write_text(json.dumps(_SETTINGS, indent=2))
    except: pass


def fetch_deepseek_balance_cached():
    key = _SETTINGS.get("deepseek_api_key", "").strip()
    if not key:
        return None
    now = time.time()
    if now - _ds_balance_cache["ts"] < 60 and _ds_balance_cache["ts"] > 0:
        return _ds_balance_cache["data"]
    try:
        req = urllib.request.Request(
            "https://api.deepseek.com/user/balance",
            headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            data = json.loads(r.read())
        _ds_balance_cache["ts"] = now
        _ds_balance_cache["data"] = data
        return data
    except Exception:
        _ds_balance_cache["ts"] = now
        return _ds_balance_cache["data"]

LAUNCH_AGENT_DIR = Path.home() / "Library/LaunchAgents"
LAUNCH_AGENT_PATH = LAUNCH_AGENT_DIR / "com.tokenbar.plist"
SCRIPT_PATH = Path(__file__).resolve()

def enable_login_start():
    LAUNCH_AGENT_DIR.mkdir(parents=True, exist_ok=True)
    plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.tokenbar</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/env</string>
    <string>python3</string>
    <string>{SCRIPT_PATH}</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <false/>
  <key>StandardOutPath</key>
  <string>/tmp/tokenbar.log</string>
  <key>StandardErrorPath</key>
  <string>/tmp/tokenbar.log</string>
</dict>
</plist>"""
    LAUNCH_AGENT_PATH.write_text(plist)
    import subprocess
    subprocess.run(["launchctl", "load", str(LAUNCH_AGENT_PATH)], capture_output=True)

def disable_login_start():
    if LAUNCH_AGENT_PATH.exists():
        import subprocess
        subprocess.run(["launchctl", "unload", str(LAUNCH_AGENT_PATH)], capture_output=True)
        LAUNCH_AGENT_PATH.unlink()

def get_refresh():
    return float(_SETTINGS.get("refresh_interval", DEFAULT_REFRESH))

load_settings()


def fmt(n):
    if not n: return "0"
    if n >= 1_000_000_000: return f"{n/1_000_000_000:.1f}B"
    if n >= 1_000_000:     return f"{n/1_000_000:.1f}M"
    if n >= 1_000:         return f"{n/1_000:.1f}k"
    return str(n)


def _navbar_title(today_tok):
    lim = _limits_cache.get("data")
    tok_s = fmt(today_tok)
    if lim and lim.get("session_used") is not None:
        used = lim["session_used"]
        cd = ""
        if lim.get("session_reset_ts"):
            diff = lim["session_reset_ts"] - time.time()
            if diff > 0:
                h = int(diff // 3600)
                m = int((diff % 3600) // 60)
                cd = f" {h}h{m:02d}m" if h > 0 else f" {m}m"
        return f"{tok_s} / {used}%{cd}"
    return tok_s


def model_id(raw):
    if not raw: return "—"
    try: return json.loads(raw).get("id", raw)
    except: return str(raw).split("/")[-1]


def is_excluded(name):
    excluded = set(_SETTINGS.get("excluded_models", list(DEFAULT_EXCLUDED)))
    nl = name.lower()
    return any(e in nl for e in excluded)


def daily_list(d: dict) -> list:
    return [{"date": k, "tokens": v} for k, v in sorted(d.items())]

def daily_cost_list(d: dict) -> list:
    return [{"date": k, "cost": v} for k, v in sorted(d.items())]


def _local_day_key(ts_iso: str, fallback_ts: float) -> str:
    """Return a local YYYY-MM-DD key from an ISO timestamp string."""
    try:
        dt = datetime.fromisoformat(ts_iso.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone().strftime("%Y-%m-%d")
    except Exception:
        return datetime.fromtimestamp(fallback_ts).strftime("%Y-%m-%d")


def _fetch_git_heatmap_bg():
    global _git_cache
    _git_cache["fetching"] = True
    try:
        try:
            r = subprocess.run(["git", "config", "--global", "user.email"],
                               capture_output=True, text=True, timeout=2)
            author = r.stdout.strip() or None
        except Exception:
            author = None

        today = datetime.now().date()
        counts = {}  # date_str -> commit count
        home = Path.home()
        search_dirs = [d for d in [home / "Desktop", home / "Documents",
                                   home / "Projects", home / "dev", home / "code"]
                       if d.exists()][:4]

        seen = set()
        for sd in search_dirs:
            try:
                res = subprocess.run(
                    ["find", str(sd), "-maxdepth", "3", "-name", ".git", "-type", "d"],
                    capture_output=True, text=True, timeout=6)
                for gd in res.stdout.strip().split("\n"):
                    if not gd:
                        continue
                    repo = str(Path(gd).parent)
                    if repo in seen:
                        continue
                    seen.add(repo)
                    since = (today - timedelta(days=365)).isoformat()
                    cmd = ["git", "-C", repo, "log",
                           f"--since={since}", "--format=%ad", "--date=format:%Y-%m-%d"]
                    if author:
                        cmd += [f"--author={author}"]
                    r2 = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
                    for ds in r2.stdout.strip().split("\n"):
                        if ds:
                            counts[ds] = counts.get(ds, 0) + 1
            except Exception:
                pass
        _git_cache["heatmap"] = counts
        _git_cache["ts"] = time.time()
    except Exception:
        pass
    finally:
        _git_cache["fetching"] = False


def _git_heatmap():
    if time.time() - _git_cache["ts"] > 300 and not _git_cache["fetching"]:
        threading.Thread(target=_fetch_git_heatmap_bg, daemon=True).start()
    return _git_cache["heatmap"]


# (input $/M, output $/M, cache_write_5m $/M, cache_read $/M)
CLAUDE_PRICING = [
    ("opus-4",    5.00, 25.00, 6.25, 0.50),
    ("sonnet-4",  3.00, 15.00, 3.75, 0.30),
    ("haiku-4",   1.00,  5.00, 1.25, 0.10),
    ("opus",     15.00, 75.00, 18.75, 1.50),
    ("sonnet",    3.00, 15.00, 3.75, 0.30),
    ("haiku",     0.25,  1.25, 0.30, 0.03),
]

BLENDED_RATES = [  # $/M blended (70% input + 30% output) pour modèles non-Claude
    ("gpt-5.4-mini",       1.9),   # $0.75 in + $4.50 out
    ("gpt-5.5",           12.5),   # $5.00 in + $30.00 out
    ("gpt-5.4",            7.5),   # $3.00 in + $18.00 out
    ("gpt-5.3-codex",      3.0),   # codex family
    ("gpt-5.2-codex",      3.0),
    ("gpt-5.1-codex",      3.0),
    ("gpt-5.3",            7.5),
    ("gpt-5.2",            7.5),
    ("gpt-5.1",            7.5),
    ("o4-mini",            2.0),
    ("o4",                12.0),
    ("o3",                20.0),
    ("gpt-4o-mini",        0.3),
    ("gpt-4o",             5.0),
    ("deepseek-v4-flash",  0.18),  # $0.14 in + $0.28 out
    ("mimo",               0.18),  # $0.14 in + $0.28 out (Xiaomi API)
]

def claude_cost(model: str, inp: int, out: int,
                cache_write: int = 0, cache_read: int = 0) -> float:
    m = model.lower()
    for key, ri, ro, rw, rr in CLAUDE_PRICING:
        if key in m:
            return (inp * ri + out * ro + cache_write * rw + cache_read * rr) / 1_000_000
    total = inp + out + cache_write + cache_read
    rates = dict(BLENDED_RATES)
    rates.update(_SETTINGS.get("custom_rates", {}))
    for key, rate in rates.items():
        if key in m:
            return total * rate / 1_000_000
    return total * 5.0 / 1_000_000

def estimate_cost(model_name: str, tokens: int) -> float:
    """Coût estimé quand on n'a que le total (OpenCode)."""
    return claude_cost(model_name, tokens // 2, tokens // 2)


# ── Claude Limites ─────────────────────────────────────────────────────────────

_ANSI_RE = re.compile(r'\x1b\[[0-9;]*[mGKHF]|\x1b\][^\x07]*\x07|\r')

def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub('', text)


_RESET_REL_RE = re.compile(r'in\s+(?:about\s+)?(\d+)\s+(hour|minute|day)s?', re.IGNORECASE)
_RESET_ABS_RE = re.compile(
    r'(\w{3})\s+(\d+)\s+at\s+(\d{1,2}):(\d{2})\s*([ap]m)',
    re.IGNORECASE)
_MONTH_MAP = {m: i+1 for i, m in enumerate(
    ['jan','feb','mar','apr','may','jun',
     'jul','aug','sep','oct','nov','dec'])}

def _parse_reset_ts(text):
    if not text:
        return None
    now = time.time()
    # relative: "in X hours/minutes/days"
    m = _RESET_REL_RE.search(text)
    if m:
        n = int(m.group(1))
        unit = m.group(2).lower()
        offset = n * {'hour': 3600, 'minute': 60, 'day': 86400}.get(unit, 3600)
        return now + offset
    # absolute: "Jun 28 at 12:50am"
    m = _RESET_ABS_RE.search(text)
    if m:
        month = _MONTH_MAP.get(m.group(1).lower()[:3])
        if not month:
            return None
        day = int(m.group(2))
        hour = int(m.group(3))
        minute = int(m.group(4))
        ampm = m.group(5).lower()
        if ampm == 'pm' and hour < 12:
            hour += 12
        elif ampm == 'am' and hour == 12:
            hour = 0
        try:
            from datetime import datetime
            dt = datetime.now().replace(
                month=month, day=day, hour=hour, minute=minute,
                second=0, microsecond=0)
        except ValueError:
            return None
        return dt.timestamp()
    return None


def _parse_claude_limits(text: str) -> dict:
    text = _strip_ansi(text)
    now = time.time()
    result: dict = {
        "session_pct": None, "session_used": None, "session_reset": None, "session_reset_ts": None,
        "week_pct": None, "week_used": None, "week_reset": None, "week_reset_ts": None,
        "opus_pct": None, "opus_used": None, "opus_reset": None, "opus_reset_ts": None,
        "plan": None,
        "stats_24h": None, "stats_7d": None,
        "error": None,
    }

    # Plan line
    m = re.search(r'(Claude\s+(?:Pro|Max|Team|Enterprise)[^\n]*)', text, re.IGNORECASE)
    if m:
        result["plan"] = m.group(1).strip()

    def parse_window(label_pattern):
        m = re.search(
            label_pattern + r'[:\s]+(\d+)%\s+(?:used|remaining)[^·\n]*[·•]\s*resets?\s+(.+?)(?:\n|$)',
            text, re.IGNORECASE)
        if not m:
            m = re.search(
                label_pattern + r'[:\s]+(\d+)%[^·\n]*[·•]\s*resets?\s+(.+?)(?:\n|$)',
                text, re.IGNORECASE)
        if m:
            pct_raw = int(m.group(1))
            reset = m.group(2).strip().rstrip(')')
            # "XX% used" → used=XX, left=100-XX ; "XX% remaining" → left=XX, used=100-XX
            full_match = m.group(0).lower()
            if 'remaining' in full_match or 'left' in full_match:
                used = 100 - pct_raw
                left = pct_raw
            else:
                used = pct_raw
                left = 100 - pct_raw
            return used, left, reset
        return None, None, None

    s_used, s_left, s_reset = parse_window(r'Current\s+session')
    if s_used is not None:
        result["session_used"] = s_used
        result["session_pct"]  = s_left
        result["session_reset"] = s_reset
        result["session_reset_ts"] = _parse_reset_ts(s_reset)

    w_used, w_left, w_reset = parse_window(r'Current\s+week\s*\(all\s+models\)')
    if w_used is not None:
        result["week_used"] = w_used
        result["week_pct"]  = w_left
        result["week_reset"] = w_reset
        result["week_reset_ts"] = _parse_reset_ts(w_reset)

    o_used, o_left, o_reset = parse_window(
        r'Current\s+week\s*\((?:Opus|Sonnet\s+only|Sonnet)\)')
    if o_used is not None:
        result["opus_used"] = o_used
        result["opus_pct"]  = o_left
        result["opus_reset"] = o_reset
        result["opus_reset_ts"] = _parse_reset_ts(o_reset)

    def parse_stats_block(header_re, stop_re=None):
        m = re.search(header_re + r'\s*[·•]\s*(\d+)\s+requests?\s*[·•]\s*(\d+)\s+sessions?'
                      r'(.*?)(?=' + (stop_re or r'(?:Last \d+[dw]|$)') + r')',
                      text, re.DOTALL | re.IGNORECASE)
        if not m:
            return None
        body = m.group(3)
        behaviors = []
        for bm in re.finditer(r'(\d+)%\s+of your usage came from\s+(.+?)(?:\n|$)', body):
            behaviors.append({"pct": int(bm.group(1)), "label": bm.group(2).strip()})
        top_sub = re.search(r'Top\s+subagents?:\s+(.+?)(?:\n|$)', body)
        top_mcp = re.search(r'Top\s+MCP\s+servers?:\s+(.+?)(?:\n|$)', body)
        top_ski = re.search(r'Top\s+skills?:\s+(.+?)(?:\n|$)', body)
        return {
            "requests": int(m.group(1)),
            "sessions": int(m.group(2)),
            "behaviors": behaviors,
            "top_subagents": top_sub.group(1).strip() if top_sub else None,
            "top_mcp": top_mcp.group(1).strip() if top_mcp else None,
            "top_skills": top_ski.group(1).strip() if top_ski else None,
        }

    result["stats_24h"] = parse_stats_block(r'Last\s+24h', r'Last\s+7d')
    result["stats_7d"]  = parse_stats_block(r'Last\s+7d')

    if result["session_pct"] is None and result["week_pct"] is None:
        result["error"] = text[:300].strip() or "Aucune donnée."

    return result


def _fetch_limits_impl() -> dict:
    env = {**os.environ, "NO_COLOR": "1", "TERM": "dumb", "CI": "1"}
    try:
        proc = subprocess.run(
            ["claude", "/usage"],
            capture_output=True, text=True, timeout=45, env=env,
            stdin=subprocess.DEVNULL,
        )
        raw = proc.stdout + proc.stderr
        data = _parse_claude_limits(raw)
        return data
    except FileNotFoundError:
        return {"error": "claude CLI introuvable."}
    except subprocess.TimeoutExpired:
        return {"error": "Timeout — claude /usage a pris trop de temps."}
    except Exception as e:
        return {"error": str(e)}


def _refresh_limits_bg():
    if _limits_cache["fetching"]:
        return
    _limits_cache["fetching"] = True
    try:
        data = _fetch_limits_impl()
        _limits_cache["ts"] = time.time()
        _limits_cache["data"] = data
    finally:
        _limits_cache["fetching"] = False


def fetch_claude_limits_cached():
    now = time.time()
    if now - _limits_cache["ts"] > 300:
        threading.Thread(target=_refresh_limits_bg, daemon=True).start()
    return _limits_cache["data"]


# ── OpenCode ──────────────────────────────────────────────────────────────────

def fetch_opencode_workflow_tokens():
    """Read tokens from workflow agent sessions not yet persisted to opencode.db."""
    try:
        if not OC_WF_DIR.exists():
            return {}
        result = {}
        now = time.time()
        for wf_dir in sorted(OC_WF_DIR.iterdir()):
            if not wf_dir.is_dir():
                continue
            tokens_file = wf_dir / "tokens.json"
            wf_file     = wf_dir / "workflow.json"
            if not tokens_file.exists() or not wf_file.exists():
                continue
            try:
                tokens = json.loads(tokens_file.read_text())
                wf     = json.loads(wf_file.read_text())
                wf_created_ms = (wf.get("time") or {}).get("created", 0)
                if wf_created_ms < (now - 30 * 86400) * 1000:
                    continue
                total = sum(v for v in tokens.values() if isinstance(v, (int, float)))
                if total > 0:
                    result[wf_dir.name] = {
                        "total": int(total),
                        "created_ms": wf_created_ms,
                    }
            except Exception:
                continue
        return result
    except Exception:
        return {}


def _oc_read_db(db_path, day_ms, week_ms, month_ms):
    """Read a single OpenCode DB and return raw aggregates."""
    if not db_path.exists():
        return None
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        c = con.cursor()

        def one(q, *a):
            c.execute(q, a); return c.fetchone()[0] or 0

        today  = one("SELECT COALESCE(SUM(tokens_input+tokens_output+tokens_reasoning+tokens_cache_read+tokens_cache_write),0) FROM session WHERE time_archived IS NULL AND time_updated>=?", day_ms)
        week   = one("SELECT COALESCE(SUM(tokens_input+tokens_output+tokens_reasoning+tokens_cache_read+tokens_cache_write),0) FROM session WHERE time_archived IS NULL AND time_updated>=?", week_ms)
        total  = one("SELECT COALESCE(SUM(tokens_input+tokens_output+tokens_reasoning+tokens_cache_read+tokens_cache_write),0) FROM session WHERE time_archived IS NULL")
        t_sess = one("SELECT COUNT(*) FROM session WHERE time_archived IS NULL AND time_updated>=?", day_ms)
        a_sess = one("SELECT COUNT(*) FROM session WHERE time_archived IS NULL")

        c.execute("""SELECT date(time_updated/1000,'unixepoch','localtime'),
                            COALESCE(SUM(tokens_input+tokens_output+tokens_reasoning+tokens_cache_read+tokens_cache_write),0)
                     FROM session WHERE time_archived IS NULL AND time_updated>=? GROUP BY 1""", (month_ms,))
        daily = {r[0]: r[1] for r in c.fetchall()}

        c.execute("""SELECT model, COALESCE(SUM(tokens_input+tokens_output+tokens_reasoning+tokens_cache_read+tokens_cache_write),0)
                     FROM session WHERE time_archived IS NULL AND model IS NOT NULL
                     GROUP BY model ORDER BY 2 DESC""")
        models = {}
        for r in c.fetchall():
            mid = model_id(r[0])
            if is_excluded(mid): continue
            models[mid] = models.get(mid, 0) + r[1]

        cost_today = one("SELECT COALESCE(SUM(cost),0.0) FROM session WHERE time_archived IS NULL AND time_updated>=?", day_ms)
        cost_all   = one("SELECT COALESCE(SUM(cost),0.0) FROM session WHERE time_archived IS NULL")
        c.execute("""SELECT date(time_updated/1000,'unixepoch','localtime'),
                            COALESCE(SUM(cost),0.0)
                     FROM session WHERE time_archived IS NULL AND time_updated>=?
                     GROUP BY 1""", (month_ms,))
        daily_cost_raw = {r[0]: r[1] for r in c.fetchall()}

        def mq(since):
            c.execute("""SELECT model, COALESCE(SUM(tokens_input+tokens_output+tokens_reasoning+tokens_cache_read+tokens_cache_write),0)
                         FROM session WHERE time_archived IS NULL AND model IS NOT NULL
                         AND time_updated>=? GROUP BY model ORDER BY 2 DESC""", (since,))
            return {model_id(r[0]): r[1] for r in c.fetchall() if not is_excluded(model_id(r[0]))}

        def mcq(since):
            c.execute("""SELECT model, COALESCE(SUM(cost),0.0)
                         FROM session WHERE time_archived IS NULL AND model IS NOT NULL
                         AND time_updated>=? GROUP BY model""", (since,))
            return {model_id(r[0]): r[1] for r in c.fetchall() if not is_excluded(model_id(r[0]))}

        c.execute("""SELECT model, COALESCE(SUM(cost),0.0)
                     FROM session WHERE time_archived IS NULL AND model IS NOT NULL
                     GROUP BY model""")
        model_costs = {model_id(r[0]): r[1] for r in c.fetchall() if not is_excluded(model_id(r[0]))}

        c.execute("""SELECT COALESCE(SUM(tokens_input),0), COALESCE(SUM(tokens_output),0),
                            COALESCE(SUM(tokens_reasoning),0),
                            COALESCE(SUM(tokens_cache_read),0), COALESCE(SUM(tokens_cache_write),0)
                     FROM session WHERE time_archived IS NULL AND time_updated>=?""", (day_ms,))
        row = c.fetchone() or (0, 0, 0, 0, 0)
        bd_today = {"input": row[0], "output": row[1], "reasoning": row[2], "cache_read": row[3], "cache_write": row[4]}

        c.execute("""SELECT date(time_updated/1000,'unixepoch','localtime'),
                            COALESCE(SUM(tokens_input),0), COALESCE(SUM(tokens_output),0),
                            COALESCE(SUM(tokens_reasoning),0),
                            COALESCE(SUM(tokens_cache_read),0), COALESCE(SUM(tokens_cache_write),0)
                     FROM session WHERE time_archived IS NULL AND time_updated>=? GROUP BY 1""", (month_ms,))
        daily_bd = {r[0]: {"i": r[1], "o": r[2], "r": r[3], "cr": r[4], "cw": r[5]} for r in c.fetchall()}

        result = {
            "today": today, "week": week, "total": total,
            "today_sess": t_sess, "all_sess": a_sess,
            "daily": daily, "models": models,
            "models_1d": mq(day_ms), "models_7d": mq(week_ms), "models_1m": mq(month_ms),
            "model_costs": model_costs,
            "model_costs_1d": mcq(day_ms), "model_costs_7d": mcq(week_ms), "model_costs_1m": mcq(month_ms),
            "cost_today": cost_today, "cost_all": cost_all,
            "daily_cost": daily_cost_raw,
            "breakdown_today": bd_today, "daily_breakdown": daily_bd,
        }
        con.close()
        return result
    except Exception:
        return None


def fetch_opencode(day_ms, week_ms, month_ms):
    # Read both DBs (stable + dev) and merge
    stable = _oc_read_db(OC_DB, day_ms, week_ms, month_ms)
    dev    = _oc_read_db(OC_DB_DEV, day_ms, week_ms, month_ms)

    if stable is None and dev is None:
        return {"today": 0, "week": 0, "total": 0,
                "today_sess": 0, "all_sess": 0, "daily": {}, "models": {},
                "models_1d": {}, "models_7d": {}, "models_1m": {},
                "model_costs": {}, "model_costs_1d": {}, "model_costs_7d": {}, "model_costs_1m": {},
                "cost_today": 0.0, "cost_all": 0.0, "cost_exact": False, "daily_cost": {},
                "breakdown_today": {}, "daily_breakdown": {}}

    # Use whichever DB is available
    d = dev if dev is not None else stable

    # Merge the second DB if present
    other = stable if dev is not None and stable is not None else None
    if other is not None:
        d["today"]      += other["today"]
        d["week"]       += other["week"]
        d["total"]      += other["total"]
        d["today_sess"] += other["today_sess"]
        d["all_sess"]   += other["all_sess"]
        for k, v in other["daily"].items():
            d["daily"][k] = d["daily"].get(k, 0) + v
        for k, v in other["models"].items():
            d["models"][k] = d["models"].get(k, 0) + v
        for key in ("models_1d", "models_7d", "models_1m"):
            for k, v in other[key].items():
                d[key][k] = d[key].get(k, 0) + v
        for key in ("model_costs", "model_costs_1d", "model_costs_7d", "model_costs_1m"):
            for k, v in other[key].items():
                d[key][k] = d[key].get(k, 0.0) + v
        d["cost_today"] += other["cost_today"]
        d["cost_all"]   += other["cost_all"]
        for k, v in other["daily_cost"].items():
            d["daily_cost"][k] = d["daily_cost"].get(k, 0.0) + v
        for k in d["breakdown_today"]:
            d["breakdown_today"][k] += other["breakdown_today"].get(k, 0)
        for day_key, bd in other["daily_breakdown"].items():
            if day_key not in d["daily_breakdown"]:
                d["daily_breakdown"][day_key] = bd
            else:
                for k in bd:
                    d["daily_breakdown"][day_key][k] = d["daily_breakdown"][day_key].get(k, 0) + bd[k]

    cost_exact = True
    if d["cost_all"] == 0 and d["total"] > 0:
        d["cost_all"]   = sum(estimate_cost(m, t) for m, t in d["models"].items())
        d["cost_today"] = d["cost_all"] * d["today"] / d["total"] if d["total"] > 0 else 0.0
        d["daily_cost"] = {day: d["cost_all"] * t / d["total"] for day, t in d["daily"].items()} if d["total"] > 0 else {}
        for key in ("model_costs", "model_costs_1d", "model_costs_7d", "model_costs_1m"):
            src = {1: "models_1d", 7: "models_7d", 30: "models_1m"}.get(
                {"model_costs": 0, "model_costs_1d": 1, "model_costs_7d": 7, "model_costs_1m": 30}[key], "models")
            src_dict = d[src] if src == "models" else d[src]
            d[key] = {m: estimate_cost(m, t) for m, t in src_dict.items()}
        cost_exact = False

    d["cost_exact"] = cost_exact

    # Include tokens from workflow agent sessions not yet in the DB
    wf_tokens = fetch_opencode_workflow_tokens()
    today_date = datetime.now().strftime("%Y-%m-%d")
    if wf_tokens:
        wf_total = sum(w["total"] for w in wf_tokens.values())
        d["total"] += wf_total
        d["week"]  += wf_total
        for wf_id, wf_data in wf_tokens.items():
            wf_day = datetime.fromtimestamp(wf_data["created_ms"] / 1000).strftime("%Y-%m-%d")
            d["daily"][wf_day] = d["daily"].get(wf_day, 0) + wf_data["total"]
            if wf_day == today_date:
                d["today"] += wf_data["total"]
            wf_cost = wf_data["total"] * 0.18 / 1_000_000
            d["daily_cost"][wf_day] = d["daily_cost"].get(wf_day, 0.0) + wf_cost
            d["cost_all"] += wf_cost
            if wf_day == today_date:
                d["cost_today"] += wf_cost
        d["models"]["deepseek-v4-flash"] = d["models"].get("deepseek-v4-flash", 0) + wf_total
        d["models_1d"]["deepseek-v4-flash"] = d["models_1d"].get("deepseek-v4-flash", 0) + wf_total
        d["models_7d"]["deepseek-v4-flash"] = d["models_7d"].get("deepseek-v4-flash", 0) + wf_total
        d["models_1m"]["deepseek-v4-flash"] = d["models_1m"].get("deepseek-v4-flash", 0) + wf_total

    return d


# ── Codex ────────────────────────────────────────────────────────────────────

def fetch_codex(day_ms, week_ms, month_ms):
    if not CODEX_DB.exists():
        return {"today": 0, "week": 0, "total": 0,
                "today_sess": 0, "all_sess": 0,
                "daily": {}, "models": {},
                "models_1d": {}, "models_7d": {}, "models_1m": {},
                "model_costs": {}, "model_costs_1d": {}, "model_costs_7d": {}, "model_costs_1m": {},
                "cost_today": 0.0, "cost_all": 0.0, "cost_exact": False,
                "daily_cost": {},
                "breakdown_today": {}, "daily_breakdown": {}}
    try:
        con = sqlite3.connect(f"file:{CODEX_DB}?mode=ro", uri=True)
        c = con.cursor()

        def one(q, *a):
            c.execute(q, a); return c.fetchone()[0] or 0

        today  = one("SELECT COALESCE(SUM(tokens_used),0) FROM threads WHERE archived=0 AND updated_at_ms>=?", day_ms)
        week   = one("SELECT COALESCE(SUM(tokens_used),0) FROM threads WHERE archived=0 AND updated_at_ms>=?", week_ms)
        total  = one("SELECT COALESCE(SUM(tokens_used),0) FROM threads WHERE archived=0")
        t_sess = one("SELECT COUNT(*) FROM threads WHERE archived=0 AND updated_at_ms>=?", day_ms)
        a_sess = one("SELECT COUNT(*) FROM threads WHERE archived=0")

        c.execute("""SELECT date(updated_at_ms/1000,'unixepoch','localtime'),
                            COALESCE(SUM(tokens_used),0)
                     FROM threads WHERE archived=0 AND updated_at_ms>=? GROUP BY 1""", (month_ms,))
        daily = {r[0]: r[1] for r in c.fetchall()}

        c.execute("""SELECT model, COALESCE(SUM(tokens_used),0)
                     FROM threads WHERE archived=0 AND model IS NOT NULL AND model != ''
                     GROUP BY model ORDER BY 2 DESC""")
        models = {}
        for r in c.fetchall():
            m = r[0]
            if is_excluded(m): continue
            models[m] = models.get(m, 0) + r[1]

        # Codex does not store cost in the DB — estimate from BLENDED_RATES / CLAUDE_PRICING
        cost_all   = sum(estimate_cost(m, t) for m, t in models.items())
        cost_today = cost_all * today / total if total > 0 else 0.0
        daily_cost = {d: cost_all * t / total for d, t in daily.items()} if total > 0 else {}

        def mq(since):
            c.execute("""SELECT model, COALESCE(SUM(tokens_used),0)
                         FROM threads WHERE archived=0 AND model IS NOT NULL AND model != ''
                         AND updated_at_ms>=? GROUP BY model ORDER BY 2 DESC""", (since,))
            return {r[0]: r[1] for r in c.fetchall() if not is_excluded(r[0])}

        models_1d = mq(day_ms)
        models_7d = mq(week_ms)
        models_1m = mq(month_ms)

        model_costs    = {m: estimate_cost(m, t) for m, t in models.items()}
        model_costs_1d = {m: estimate_cost(m, t) for m, t in models_1d.items()}
        model_costs_7d = {m: estimate_cost(m, t) for m, t in models_7d.items()}
        model_costs_1m = {m: estimate_cost(m, t) for m, t in models_1m.items()}

        con.close()

        return {"today": today, "week": week, "total": total,
                "today_sess": t_sess, "all_sess": a_sess,
                "daily": daily, "models": models,
                "models_1d": models_1d, "models_7d": models_7d, "models_1m": models_1m,
                "model_costs": model_costs, "model_costs_1d": model_costs_1d,
                "model_costs_7d": model_costs_7d, "model_costs_1m": model_costs_1m,
                "cost_today": cost_today, "cost_all": cost_all, "cost_exact": False,
                "daily_cost": daily_cost,
                "breakdown_today": {"input": today // 2, "output": today - today // 2},
                "daily_breakdown": {d: {"i": t // 2, "o": t - t // 2} for d, t in daily.items()}}
    except Exception:
        return {"today": 0, "week": 0, "total": 0,
                "today_sess": 0, "all_sess": 0, "daily": {}, "models": {},
                "models_1d": {}, "models_7d": {}, "models_1m": {},
                "model_costs": {}, "model_costs_1d": {}, "model_costs_7d": {}, "model_costs_1m": {},
                "cost_today": 0.0, "cost_all": 0.0, "cost_exact": False,
                "daily_cost": {},
                "breakdown_today": {}, "daily_breakdown": {}}


# ── Claude Code ───────────────────────────────────────────────────────────────

def fetch_claude_code(day_s, week_s, month_s):
    global _cc_cache
    now = time.time()
    if now - _cc_cache["ts"] < 30 and _cc_cache["data"]:
        return _cc_cache["data"]

    if not CC_DIR.exists():
        return {"today": 0, "week": 0, "total": 0, "daily": {}, "models": {},
                "models_1d": {}, "models_7d": {}, "models_1m": {},
                "model_costs": {}, "model_costs_1d": {}, "model_costs_7d": {}, "model_costs_1m": {},
                "cost_today": 0.0, "cost_all": 0.0, "breakdown_today": {}, "daily_breakdown": {}}

    models, models_1d, models_7d, models_1m = {}, {}, {}, {}
    model_costs, model_costs_1d, model_costs_7d, model_costs_1m = {}, {}, {}, {}
    total, today, week = 0, 0, 0
    cost_all, cost_today = 0.0, 0.0
    daily, daily_cost = {}, {}
    daily_inp, daily_out, daily_cr_d, daily_cw_d = {}, {}, {}, {}
    bd_i, bd_o, bd_cr, bd_cw = 0, 0, 0, 0

    for jf in CC_DIR.rglob("*.jsonl"):
        try: mtime = jf.stat().st_mtime
        except: continue
        if mtime < month_s: continue

        try:
            with open(jf, encoding="utf-8", errors="ignore") as f:
                for line in f:
                    try:
                        entry = json.loads(line)
                        msg   = entry.get("message", {})
                        usage = msg.get("usage")
                        if not usage: continue
                        ts_iso = entry.get("timestamp")
                        event_ts = mtime
                        if ts_iso:
                            try:
                                event_ts = datetime.fromisoformat(
                                    ts_iso.replace("Z", "+00:00")
                                ).timestamp()
                            except Exception:
                                event_ts = mtime
                        fdate    = _local_day_key(ts_iso, event_ts) if ts_iso else datetime.fromtimestamp(event_ts).strftime("%Y-%m-%d")
                        is_today = event_ts >= day_s
                        is_week  = event_ts >= week_s
                        in_month = event_ts >= month_s
                        i_tok  = usage.get("input_tokens", 0)
                        o_tok  = usage.get("output_tokens", 0)
                        c_writ = usage.get("cache_creation_input_tokens", 0)
                        c_read = usage.get("cache_read_input_tokens", 0)
                        tok    = i_tok + o_tok + c_read + c_writ
                        if not tok: continue
                        m = msg.get("model") or "claude"
                        if is_excluded(m): continue
                        est = claude_cost(m, i_tok, o_tok, c_writ, c_read)
                        models[m]      = models.get(m, 0) + tok
                        model_costs[m] = model_costs.get(m, 0.0) + est
                        total    += tok
                        cost_all += est
                        if is_today:
                            today += tok; cost_today += est
                            models_1d[m]      = models_1d.get(m, 0) + tok
                            model_costs_1d[m] = model_costs_1d.get(m, 0.0) + est
                            bd_i += i_tok; bd_o += o_tok
                            bd_cr += c_read; bd_cw += c_writ
                        if is_week:
                            week += tok
                            models_7d[m]      = models_7d.get(m, 0) + tok
                            model_costs_7d[m] = model_costs_7d.get(m, 0.0) + est
                        if in_month:
                            daily[fdate]      = daily.get(fdate, 0) + tok
                            daily_cost[fdate] = daily_cost.get(fdate, 0.0) + est
                            models_1m[m]      = models_1m.get(m, 0) + tok
                            model_costs_1m[m] = model_costs_1m.get(m, 0.0) + est
                            daily_inp[fdate]  = daily_inp.get(fdate, 0) + i_tok
                            daily_out[fdate]  = daily_out.get(fdate, 0) + o_tok
                            daily_cr_d[fdate] = daily_cr_d.get(fdate, 0) + c_read
                            daily_cw_d[fdate] = daily_cw_d.get(fdate, 0) + c_writ
                    except: pass
        except: pass

    result = {"today": today, "week": week, "total": total,
              "daily": daily, "daily_cost": daily_cost, "models": models,
              "models_1d": models_1d, "models_7d": models_7d, "models_1m": models_1m,
              "model_costs": model_costs, "model_costs_1d": model_costs_1d,
              "model_costs_7d": model_costs_7d, "model_costs_1m": model_costs_1m,
              "cost_today": cost_today, "cost_all": cost_all,
              "breakdown_today": {"input": bd_i, "output": bd_o,
                                  "cache_read": bd_cr, "cache_write": bd_cw},
              "daily_breakdown": {d: {"i": daily_inp.get(d,0), "o": daily_out.get(d,0),
                                      "cr": daily_cr_d.get(d,0), "cw": daily_cw_d.get(d,0)}
                                  for d in daily}}
    _cc_cache = {"ts": now, "data": result}
    return result


# ── Combined ──────────────────────────────────────────────────────────────────

def _top(models: dict) -> str:
    return max(models, key=models.get) if models else "—"


def fetch():
    now_dt   = datetime.now()
    day_s    = now_dt.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    week_s   = day_s  - 6  * 86400
    month_s  = day_s  - 29 * 86400
    day_ms   = int(day_s   * 1000)
    week_ms  = int(week_s  * 1000)
    month_ms = int(month_s * 1000)

    oc = fetch_opencode(day_ms, week_ms, month_ms)
    cc = fetch_claude_code(day_s, week_s, month_s)
    cx = fetch_codex(day_ms, week_ms, month_ms)

    elapsed_h = max(0.5, (time.time() - day_s) / 3600)
    tok_per_hour = int(cc["today"] / elapsed_h) if cc.get("today", 0) > 0 else 0
    ds_balance = fetch_deepseek_balance_cached()

    def merged_daily(*dicts):
        dates = sorted(set().union(*[d.keys() for d in dicts]))
        return [{"date": d, "tokens": sum(dd.get(d, 0) for dd in dicts)} for d in dates]

    def merged_daily_cost(*dicts):
        dates = sorted(set().union(*[d.keys() for d in dicts]))
        return [{"date": d, "cost": sum(dd.get(d, 0.0) for dd in dicts)} for d in dates]

    all_models      = {}
    all_models_today = {}
    for src in (oc["models"], cc["models"], cx["models"]):
        for k, v in src.items():
            all_models[k] = all_models.get(k, 0) + v
    for src in (oc.get("models_1d", {}), cc.get("models_1d", {}), cx.get("models_1d", {})):
        for k, v in src.items():
            all_models_today[k] = all_models_today.get(k, 0) + v

    def merge_bd_today(*srcs):
        r = {"input": 0, "output": 0, "reasoning": 0, "cache_read": 0, "cache_write": 0}
        for s in srcs:
            for k in r:
                r[k] += s.get(k, 0)
        return r

    def merge_daily_bd(*srcs):
        dates = set().union(*(s.keys() for s in srcs))
        return {d: {k: sum(s.get(d, {}).get(k, 0) for s in srcs)
                    for k in ("i", "o", "r", "cr", "cw")}
                for d in dates}

    limits = fetch_claude_limits_cached()

    return {
        "limits": limits,
        "all": {
            "today_tok":  oc["today"] + cc["today"] + cx["today"],
            "week_tok":   oc["week"]  + cc["week"]  + cx["week"],
            "all_tok":    oc["total"] + cc["total"] + cx["total"],
            "today_sess": None,
            "all_sess":   None,
            "top_model":  _top(all_models),
            "top_model_today": _top(all_models_today),
            "daily":      merged_daily(oc["daily"], cc["daily"], cx["daily"]),
            "daily_cost": merged_daily_cost(oc["daily_cost"], cc["daily_cost"], cx["daily_cost"]),
            "cost_today": oc["cost_today"] + cc["cost_today"] + cx["cost_today"],
            "cost_all":   oc["cost_all"]   + cc["cost_all"]   + cx["cost_all"],
            "cost_exact": False,
            "breakdown_today": merge_bd_today(cc.get("breakdown_today", {}),
                                              oc.get("breakdown_today", {}),
                                              cx.get("breakdown_today", {})),
            "daily_breakdown": merge_daily_bd(cc.get("daily_breakdown", {}),
                                              oc.get("daily_breakdown", {}),
                                              cx.get("daily_breakdown", {})),
            "tok_per_hour": tok_per_hour,
            "ds_balance": ds_balance,
        },
        "claude_code": {
            "today_tok":  cc["today"],
            "week_tok":   cc["week"],
            "all_tok":    cc["total"],
            "today_sess": None,
            "all_sess":   None,
            "top_model":  _top(cc["models"]),
            "daily":      daily_list(cc["daily"]),
            "daily_cost": daily_cost_list(cc["daily_cost"]),
            "cost_today": cc["cost_today"],
            "cost_all":   cc["cost_all"],
            "cost_exact": False,
            "breakdown_today": cc.get("breakdown_today", {}),
            "daily_breakdown": cc.get("daily_breakdown", {}),
            "tok_per_hour": tok_per_hour,
        },
        "opencode": {
            "today_tok":  oc["today"],
            "week_tok":   oc["week"],
            "all_tok":    oc["total"],
            "today_sess": oc["today_sess"],
            "all_sess":   oc["all_sess"],
            "top_model":  _top(oc["models"]),
            "daily":      daily_list(oc["daily"]),
            "daily_cost": daily_cost_list(oc["daily_cost"]),
            "cost_today": oc["cost_today"],
            "cost_all":   oc["cost_all"],
            "cost_exact": oc.get("cost_exact", False),
            "breakdown_today": oc.get("breakdown_today", {}),
            "daily_breakdown": oc.get("daily_breakdown", {}),
        },
        "codex": {
            "today_tok":  cx["today"],
            "week_tok":   cx["week"],
            "all_tok":    cx["total"],
            "today_sess": cx["today_sess"],
            "all_sess":   cx["all_sess"],
            "top_model":  _top(cx.get("models_1m") or cx["models"]),
            "daily":      daily_list(cx["daily"]),
            "daily_cost": daily_cost_list(cx["daily_cost"]),
            "cost_today": cx["cost_today"],
            "cost_all":   cx["cost_all"],
            "cost_exact": cx.get("cost_exact", False),
            "breakdown_today": cx.get("breakdown_today", {}),
            "daily_breakdown": cx.get("daily_breakdown", {}),
        },
    }


def fetch_all_models():
    now_dt   = datetime.now()
    day_s    = now_dt.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    week_s   = day_s - 6  * 86400
    month_s  = day_s - 29 * 86400
    day_ms   = int(day_s   * 1000)
    week_ms  = int(week_s  * 1000)
    month_ms = int(month_s * 1000)

    oc = fetch_opencode(day_ms, week_ms, month_ms)
    cc = fetch_claude_code(day_s, week_s, month_s)
    cx = fetch_codex(day_ms, week_ms, month_ms)

    def make_rows(oc_m, oc_mc, cc_m, cc_mc, cx_m, cx_mc):
        rows = []
        for name, tok in oc_m.items():
            cost = oc_mc.get(name, estimate_cost(name, tok))
            rows.append({"name": name, "tokens": tok, "cost": round(cost, 4), "source": "OpenCode"})
        for name, tok in cc_m.items():
            cost = cc_mc.get(name, estimate_cost(name, tok))
            rows.append({"name": name, "tokens": tok, "cost": round(cost, 4), "source": "Claude Code"})
        for name, tok in cx_m.items():
            cost = cx_mc.get(name, estimate_cost(name, tok))
            rows.append({"name": name, "tokens": tok, "cost": round(cost, 4), "source": "Codex"})
        return sorted(rows, key=lambda x: -x["tokens"])

    return {
        "1d":  make_rows(oc["models_1d"], oc["model_costs_1d"], cc["models_1d"], cc["model_costs_1d"], cx["models_1d"], cx["model_costs_1d"]),
        "7d":  make_rows(oc["models_7d"], oc["model_costs_7d"], cc["models_7d"], cc["model_costs_7d"], cx["models_7d"], cx["model_costs_7d"]),
        "1m":  make_rows(oc["models_1m"], oc["model_costs_1m"], cc["models_1m"], cc["model_costs_1m"], cx["models_1m"], cx["model_costs_1m"]),
        "all": make_rows(oc["models"],    oc["model_costs"],    cc["models"],    cc["model_costs"],    cx["models"],    cx["model_costs"]),
    }


# ── HTML ──────────────────────────────────────────────────────────────────────

MAIN_HTML = """\
<!DOCTYPE html><html><head><meta charset="utf-8">
<style>
*{margin:0;padding:0;box-sizing:border-box}
html,body{width:360px;background:#1c1c1e;color:#fff;
  font-family:-apple-system,BlinkMacSystemFont,"SF Pro Text",sans-serif;
  overflow:hidden;-webkit-font-smoothing:antialiased}

/* tabs */
.tabs{display:flex;padding:0 14px;border-bottom:1px solid rgba(255,255,255,.08)}
.tab{padding:10px 11px 9px;font-size:12px;font-weight:500;color:rgba(255,255,255,.38);
  cursor:pointer;border-bottom:2px solid transparent;margin-bottom:-1px;
  user-select:none;transition:color .15s}
.tab:hover:not(.active){color:rgba(255,255,255,.6)}
.tab.active{color:#fff;border-bottom-color:rgba(255,255,255,.65)}
.tab-settings{margin-left:auto;background:none;border:none;color:rgba(255,255,255,.25);
  font-size:18px;padding:9px 8px 8px;cursor:pointer;user-select:none;transition:color .15s;
  line-height:1}
.tab-settings:hover{color:rgba(255,255,255,.65)}

/* stats */
.stats{display:grid;grid-template-columns:1fr 1fr;padding:16px 20px 8px;row-gap:14px}
.lbl{font-size:12px;font-weight:500;color:rgba(255,255,255,.55);margin-bottom:3px}
.val{font-size:26px;font-weight:700;letter-spacing:-.8px;line-height:1}

/* chart */
.chart-wrap{padding:8px 20px 0;position:relative}
canvas{display:block;width:100%}
.chart-controls{display:flex;align-items:center;padding:3px 20px 4px}
.chart-periods{display:flex;gap:1px;flex:1}
.cp{background:none;border:none;color:rgba(255,255,255,.22);font-family:inherit;
  font-size:10px;padding:2px 7px;border-radius:4px;cursor:pointer;user-select:none}
.cp:hover{color:rgba(255,255,255,.55)}
.cp.active{color:rgba(255,255,255,.72);background:rgba(255,255,255,.08)}
.chart-style-btn{background:none;border:none;color:rgba(255,255,255,.22);
  font-family:inherit;font-size:10px;padding:2px 8px;cursor:pointer;
  user-select:none;letter-spacing:.05em}
.chart-style-btn:hover{color:rgba(255,255,255,.55)}
#tip,#tip2{position:fixed;background:rgba(22,22,24,.97);border:1px solid rgba(255,255,255,.13);
  border-radius:6px;padding:5px 9px;font-size:11px;color:rgba(255,255,255,.88);
  pointer-events:none;display:none;white-space:nowrap;z-index:100}
.chart-divider{padding:6px 20px 0;font-size:9px;color:rgba(255,255,255,.22);
  text-transform:uppercase;letter-spacing:.06em}

/* summary */
.summary{padding:9px 20px 6px;font-size:12px;color:rgba(255,255,255,.45);line-height:1.75}
.models-lnk{display:inline;font-size:12px;color:rgba(255,255,255,.28);cursor:pointer;
  text-decoration:underline;text-decoration-color:rgba(255,255,255,.15);text-underline-offset:2px}
.models-lnk:hover{color:rgba(255,255,255,.55)}

/* breakdown */
.perf-section{padding:8px 16px 6px;border-top:1px solid rgba(255,255,255,.06)}
.perf-title{font-size:9px;text-transform:uppercase;letter-spacing:.07em;
  color:rgba(255,255,255,.2);margin-bottom:7px}
.perf-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:5px}
.perf-cell{background:rgba(255,255,255,.04);border-radius:7px;padding:6px 9px}
.perf-lbl{font-size:9px;color:rgba(255,255,255,.3);margin-bottom:2px;letter-spacing:.02em;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.perf-val{font-size:15px;font-weight:700;letter-spacing:-.5px;line-height:1.15;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.perf-sub{font-size:9px;color:rgba(255,255,255,.18);margin-top:1px}
.perf-up{color:#4ade80}
.perf-dn{color:#f87171}
.perf-neu{color:rgba(255,255,255,.7)}
.ds-row{padding:2px 20px 4px;font-size:11px;color:rgba(255,255,255,.38)}
.ds-row span{color:rgba(255,255,255,.72)}
/* quota bar */
.quota-row{padding:7px 20px 9px;border-top:1px solid rgba(255,255,255,.06)}
.quota-header{display:flex;justify-content:space-between;font-size:10px;
  color:rgba(255,255,255,.4);margin-bottom:5px}
.quota-track{height:5px;background:rgba(255,255,255,.1);border-radius:3px;overflow:hidden}
.quota-fill{height:100%;width:0%;border-radius:3px;transition:width .4s,background .4s}
.quota-footer{display:flex;justify-content:space-between;margin-top:4px;font-size:10px}
.quota-spent{color:rgba(255,255,255,.65)}
.quota-proj{color:rgba(255,255,255,.3)}
/* tooltip élargi pour breakdown */
#tip{min-width:160px;max-width:220px;line-height:1.5}

/* footer */
.footer{border-top:1px solid rgba(255,255,255,.08);display:flex;padding:4px 8px}
.btn{flex:1;background:none;border:none;color:rgba(255,255,255,.75);font-family:inherit;
  font-size:13px;padding:7px 10px;border-radius:7px;cursor:pointer;text-align:center}
.btn:hover{background:rgba(255,255,255,.08)}

/* ── Limites page ── */
#page-limits{display:none}
.lim-body{padding:16px 20px 10px}
.lim-plan{font-size:10px;letter-spacing:.05em;text-transform:uppercase;
  color:rgba(255,255,255,.28);margin-bottom:16px}
.lim-bar{margin-bottom:20px}
.lim-bar-top{display:flex;justify-content:space-between;align-items:flex-end;margin-bottom:7px}
.lim-bar-name{font-size:11px;font-weight:600;letter-spacing:.05em;text-transform:uppercase;
  color:rgba(255,255,255,.38)}
.lim-bar-num{font-size:28px;font-weight:700;letter-spacing:-.8px;line-height:1}
.lim-track{height:5px;background:rgba(255,255,255,.1);border-radius:3px;overflow:hidden;margin-bottom:5px}
.lim-fill{height:100%;border-radius:3px;transition:width .5s cubic-bezier(.4,0,.2,1)}
.lim-bar-sub{display:flex;justify-content:space-between;font-size:10px;color:rgba(255,255,255,.25)}
.lim-loading{padding:32px 20px;text-align:center;color:rgba(255,255,255,.3);font-size:12px}
.lim-error{padding:14px 20px;font-size:11px;color:rgba(255,80,80,.6);line-height:1.5}
.lim-ds-row{display:flex;justify-content:space-between;align-items:baseline;
  padding:10px 0 0;border-top:1px solid rgba(255,255,255,.06);margin-top:4px}
.lim-ds-label{font-size:11px;font-weight:600;letter-spacing:.05em;text-transform:uppercase;
  color:rgba(255,255,255,.38)}
.lim-ds-val{font-size:22px;font-weight:700;letter-spacing:-.6px;color:#4ade80}
.lim-ds-cur{font-size:11px;font-weight:500;color:rgba(255,255,255,.3);margin-left:3px}
.btn-q{color:rgba(255,255,255,.3)}
</style></head><body>

<div id="page-main">
<div class="tabs">
  <div class="tab active" data-tab="all"         onclick="switchTab('all')">All</div>
  <div class="tab"        data-tab="claude_code"  onclick="switchTab('claude_code')">Claude</div>
  <div class="tab"        data-tab="opencode"     onclick="switchTab('opencode')">OpenCode</div>
  <div class="tab"        data-tab="codex"        onclick="switchTab('codex')">Codex</div>
  <div class="tab"        data-tab="limits"       onclick="switchToLimits()">Usage</div>
  <button class="tab-settings" onclick="act('settings')" title="Settings">&#x2699;</button>
</div>

<div class="stats">
  <div><div class="lbl">Today</div><div class="val" id="v-today">—</div></div>
  <div><div class="lbl">7d tokens</div><div class="val" id="v-week">—</div></div>
  <div><div class="lbl">All time</div><div class="val" id="v-all">—</div></div>
  <div id="stat-sess"><div class="lbl" id="lbl-sess">Cost today</div><div class="val" id="v-sess">—</div></div>
</div>

<div class="chart-wrap">
  <canvas id="cv"></canvas>
  <div id="tip"></div>
</div>
<div class="chart-divider">estimated cost</div>
<div class="chart-wrap">
  <canvas id="cv2"></canvas>
  <div id="tip2"></div>
</div>
<div class="chart-controls">
  <div class="chart-periods">
    <button class="cp" data-p="1d" onclick="setChartPeriod('1d')">1d</button>
    <button class="cp" data-p="7d" onclick="setChartPeriod('7d')">7d</button>
    <button class="cp active" data-p="1m" onclick="setChartPeriod('1m')">1m</button>
    <button class="cp" data-p="all" onclick="setChartPeriod('all')">All</button>
  </div>
  <button class="chart-style-btn" id="style-btn" onclick="cycleStyle()">bars</button>
</div>

<div class="summary">
  <div id="s-all">—</div>
  <div id="s-model">—</div>
  <span class="models-lnk" onclick="act('models')">All models &#x2192;</span>
</div>

<div id="perf-section" class="perf-section" style="display:none">
  <div class="perf-title">Vitesse &amp; efficacité</div>
  <div class="perf-grid">
    <div class="perf-cell">
      <div class="perf-lbl">Rythme</div>
      <div class="perf-val perf-neu" id="pf-tph">—</div>
      <div class="perf-sub">tok / hr</div>
    </div>
    <div class="perf-cell">
      <div class="perf-lbl">Cache hit</div>
      <div class="perf-val" id="pf-hit">—</div>
      <div class="perf-sub">% lectures</div>
    </div>
    <div class="perf-cell">
      <div class="perf-lbl">Coût / 1M</div>
      <div class="perf-val perf-neu" id="pf-rate">—</div>
      <div class="perf-sub">taux effectif</div>
    </div>
    <div class="perf-cell">
      <div class="perf-lbl">vs moy. 7j</div>
      <div class="perf-val" id="pf-vs7">—</div>
      <div class="perf-sub">comparaison</div>
    </div>
    <div class="perf-cell">
      <div class="perf-lbl">Projection</div>
      <div class="perf-val perf-neu" id="pf-proj">—</div>
      <div class="perf-sub">fin de jour</div>
    </div>
    <div class="perf-cell">
      <div class="perf-lbl">Top modèle</div>
      <div class="perf-val perf-neu" id="pf-model">—</div>
      <div class="perf-sub">aujourd'hui</div>
    </div>
  </div>
</div>


<div id="quota-row" class="quota-row" style="display:none">
  <div class="quota-header">
    <span>Quota mensuel Claude</span><span id="q-pct">—</span>
  </div>
  <div class="quota-track"><div class="quota-fill" id="q-bar"></div></div>
  <div class="quota-footer">
    <span class="quota-spent"><span id="q-spent">—</span> / <span id="q-limit">—</span></span>
    <span class="quota-proj">proj. <span id="q-proj">—</span></span>
  </div>
</div>

<div class="footer">
  <button class="btn" onclick="act('refresh')">&#x21BA; Refresh</button>
  <button class="btn" onclick="act('flex')">&#x1F4E2; Flex</button>
  <button class="btn btn-q" onclick="act('quit')">Quit</button>
</div>
</div>

<div id="page-limits">
<div class="tabs">
  <div class="tab"  data-tab="all"         onclick="switchTab('all')">All</div>
  <div class="tab"  data-tab="claude_code"  onclick="switchTab('claude_code')">Claude</div>
  <div class="tab"  data-tab="opencode"     onclick="switchTab('opencode')">OpenCode</div>
  <div class="tab"  data-tab="codex"        onclick="switchTab('codex')">Codex</div>
  <div class="tab active" data-tab="limits" onclick="switchToLimits()">Usage</div>
  <button class="tab-settings" onclick="act('settings')" title="Settings">&#x2699;</button>
</div>

<div id="lim-body">
  <div class="lim-loading">Chargement des limites&#x2026;</div>
</div>

<div class="footer">
  <button class="btn" onclick="act('refreshLimits')">&#x21BA; Actualiser</button>
  <button class="btn btn-q" onclick="act('quit')">Quit</button>
</div>
</div>

</body></html>
"""

MAIN_JS = """\
let __data          = null;
let __tab           = 'all';
let __chartStyle    = 'bars';
let __chartPeriod   = '1m';
let __lastDaily     = [];
let __lastDailyCost = [];
let __chartHits     = [];
let __chartHits2    = [];
let __settings      = {};
let __dailyBreakdown = {};
const STYLES        = ['bars', 'line', 'area'];
const BD_COLORS = {
  cr: {hex:'#fbbf24', rgba:'rgba(251,191,36,'},
  i:  {hex:'#60a5fa', rgba:'rgba(96,165,250,'},
  r:  {hex:'#f472b6', rgba:'rgba(244,114,182,'},
  cw: {hex:'#a78bfa', rgba:'rgba(167,139,250,'},
  o:  {hex:'#34d399', rgba:'rgba(52,211,153,'},
};
const BD_ORDER = ['cr','i','r','cw','o'];

function fmt(n){
  if(!n)return'0';
  if(n>=1e9)return(n/1e9).toFixed(1)+'B';
  if(n>=1e6)return(n/1e6).toFixed(1)+'M';
  if(n>=1e3)return(n/1e3).toFixed(1)+'k';
  return''+n;
}
function fmtCost(c){
  if(!c||c<0.001)return'$0.00';
  if(c<0.01)return'$'+c.toFixed(3);
  return'$'+c.toFixed(2);
}
function fmtDate(s){
  return new Date(s+'T00:00:00').toLocaleDateString('fr-FR',{month:'short',day:'numeric'});
}

function switchTab(tab) {
  __onLimitsPage = false;
  document.getElementById('page-limits').style.display = 'none';
  document.getElementById('page-main').style.display = '';
  __tab = tab;
  document.querySelectorAll('.tab').forEach(t =>
    t.classList.toggle('active', t.dataset.tab === tab));
  if (__data) renderTab(tab);
  requestAnimationFrame(function(){
    try{window.webkit.messageHandlers.resize.postMessage(document.body.scrollHeight)}catch(e){}
  });
}

function renderTab(tab) {
  const s = __data[tab];
  if (!s) return;
  document.getElementById('v-today').textContent = fmt(s.today_tok);
  const _elH=(Date.now()-new Date().setHours(0,0,0,0))/3600000;
  const _todayLbl=_elH<23.5?'Today · '+(_elH<1?Math.round(_elH*60)+'m':(_elH<10?_elH.toFixed(1)+'h':Math.round(_elH)+'h')):'Today';
  document.getElementById('v-today').previousElementSibling.textContent=_todayLbl;
  document.getElementById('v-week').textContent  = fmt(s.week_tok);
  document.getElementById('v-all').textContent   = fmt(s.all_tok);
  const sessEl  = document.getElementById('stat-sess');
  const hasCost = s.cost_today != null && s.cost_today > 0;
  if (hasCost) {
    sessEl.style.display = '';
    document.getElementById('lbl-sess').textContent = s.cost_exact ? 'Cost today' : '~ Cost today';
    document.getElementById('v-sess').textContent  = fmtCost(s.cost_today);
  } else {
    sessEl.style.display = 'none';
  }
  const costStr = s.cost_all != null && s.cost_all > 0
    ? ' · ' + (s.cost_exact ? '' : '~') + fmtCost(s.cost_all)
    : '';
  document.getElementById('s-all').textContent   =
    'All time: ' + fmt(s.all_tok) + ' tokens' + costStr;
  document.getElementById('s-model').textContent =
    s.top_model && s.top_model !== '—' ? 'Top model: ' + s.top_model : '';
  __dailyBreakdown = s.daily_breakdown || {};
  drawChart(s.daily || []);
  drawCostChart(s.daily_cost || []);

  // Vitesse & efficacité
  const bd = s.breakdown_today;
  const perfSec = document.getElementById('perf-section');
  if (s.today_tok > 0) {
    perfSec.style.display = '';
    // Rythme
    document.getElementById('pf-tph').textContent = s.tok_per_hour ? fmt(s.tok_per_hour) : '—';
    // Cache hit
    const hitEl = document.getElementById('pf-hit');
    if (bd && (bd.input || bd.cache_read)) {
      const inputSide = (bd.input||0)+(bd.cache_read||0)+(bd.cache_write||0);
      const hitPct = inputSide>0 ? Math.round((bd.cache_read||0)/inputSide*100) : 0;
      hitEl.textContent = hitPct+'%';
      hitEl.className = 'perf-val '+(hitPct>=80?'perf-up':hitPct>=50?'perf-neu':'perf-dn');
    } else { hitEl.textContent='—'; hitEl.className='perf-val perf-neu'; }
    // Coût / 1M tokens
    const rateEl = document.getElementById('pf-rate');
    if (s.cost_today>0 && s.today_tok>0) {
      rateEl.textContent = '$'+(s.cost_today/s.today_tok*1e6).toFixed(2);
    } else { rateEl.textContent='—'; }
    // vs moy. 7j
    const vs7El = document.getElementById('pf-vs7');
    const daily7 = (s.daily||[]).slice(-7);
    if (daily7.length>=2) {
      const avg7 = daily7.slice(0,-1).reduce(function(a,d){return a+(d.tokens||0);},0)/(daily7.length-1||1);
      if (avg7>0) {
        const ratio = (s.today_tok/avg7-1)*100;
        const sign = ratio>=0?'+':'';
        vs7El.textContent = sign+Math.round(ratio)+'%';
        vs7El.className = 'perf-val '+(ratio>=10?'perf-up':ratio<=-10?'perf-dn':'perf-neu');
      } else { vs7El.textContent='—'; vs7El.className='perf-val perf-neu'; }
    } else { vs7El.textContent='—'; vs7El.className='perf-val perf-neu'; }
    // Projection fin de jour
    const projEl = document.getElementById('pf-proj');
    const elH=(Date.now()-new Date().setHours(0,0,0,0))/3600000;
    if (s.cost_today>0 && elH>0.08) {
      const proj = s.cost_today/elH*24;
      projEl.textContent = '$'+proj.toFixed(2);
    } else { projEl.textContent='—'; }
    // Top modèle
    const modelEl = document.getElementById('pf-model');
    const mName = (s.top_model_today||s.top_model||'—');
    const mShort = mName.replace(/^claude-/,'').replace(/-(202\d.*)$/,'').replace(/^[^\/]+\//,'').replace(/-/g,' ');
    modelEl.textContent = mShort.length>16 ? mShort.slice(0,15)+'…' : mShort;
  } else {
    perfSec.style.display = 'none';
  }

}

function filterByPeriod(daily) {
  if (!daily || !daily.length) return daily;
  if (__chartPeriod === '1m' || __chartPeriod === 'all') return daily;
  const n = __chartPeriod === '1d' ? 1 : 7;
  return daily.slice(-n);
}

function setChartPeriod(p) {
  __chartPeriod = p;
  document.querySelectorAll('.cp').forEach(b => b.classList.toggle('active', b.dataset.p === p));
  drawChart(__lastDaily);
  drawCostChart(__lastDailyCost);
}

function cycleStyle() {
  __chartStyle = STYLES[(STYLES.indexOf(__chartStyle)+1) % STYLES.length];
  document.getElementById('style-btn').textContent = __chartStyle;
  drawChart(__lastDaily);
  drawCostChart(__lastDailyCost);
}

function drawBar(ctx,x,y,w,h,r){
  r=Math.min(r,h/2,w/2);ctx.beginPath();
  ctx.moveTo(x+r,y);ctx.lineTo(x+w-r,y);ctx.arcTo(x+w,y,x+w,y+r,r);
  ctx.lineTo(x+w,y+h);ctx.lineTo(x,y+h);ctx.lineTo(x,y+r);ctx.arcTo(x,y,x+r,y,r);
  ctx.closePath();ctx.fill();
}

function drawChartWith(cvId, daily, valFn, hitsRef, showYAxis) {
  hitsRef.length=0;
  const cv=document.getElementById(cvId),ctx=cv.getContext('2d');
  const dpr=window.devicePixelRatio||2,cw=cv.offsetWidth||300,ch=90;
  cv.style.height=ch+'px';cv.width=cw*dpr;cv.height=ch*dpr;ctx.scale(dpr,dpr);
  ctx.clearRect(0,0,cw,ch);
  if(!daily||!daily.length)return;
  const vals=daily.map(valFn),max=Math.max(...vals,1),n=daily.length,gap=2;
  const leftPad=showYAxis?32:0,drawW=cw-leftPad;
  const bw=(drawW-gap)/n-gap,bMaxH=ch-18,bl=ch-10;
  if(showYAxis){
    const isCost=cvId==='cv2';
    const fmtAxis=isCost
      ? function(v){if(v>=1)return'$'+v.toFixed(2);if(v>=0.01)return'$'+v.toFixed(3);return'$'+v.toFixed(4);}
      : function(v){return fmt(v);};
    ctx.font='9px -apple-system, sans-serif';ctx.textAlign='right';ctx.textBaseline='middle';
    [0.25,0.5,0.75,1].forEach(function(lvl){
      const ly=bl-lvl*bMaxH;
      ctx.strokeStyle='rgba(255,255,255,.07)';ctx.lineWidth=1;
      ctx.beginPath();ctx.moveTo(leftPad,ly);ctx.lineTo(cw,ly);ctx.stroke();
      ctx.fillStyle='rgba(255,255,255,.22)';
      ctx.fillText(fmtAxis(lvl*max),leftPad-5,ly);
    });
  }
  ctx.strokeStyle='rgba(255,255,255,.22)';ctx.setLineDash([2,5]);ctx.lineWidth=1;
  ctx.beginPath();ctx.moveTo(leftPad,bl+2);ctx.lineTo(cw,bl+2);ctx.stroke();
  ctx.setLineDash([]);
  if(__chartStyle==='bars'){
    daily.forEach((d,i)=>{
      const r=vals[i]/max,bh=Math.max(2,r*bMaxH),x=i*(bw+gap)+gap+leftPad,y=bl-bh;
      ctx.fillStyle='rgba(255,255,255,'+(0.3+0.55*r).toFixed(2)+')';
      drawBar(ctx,x,y,bw,bh,2);
      hitsRef.push({x0:x,x1:x+bw,cx:x+bw/2,y:y,date:d.date,val:vals[i]});
    });
  } else {
    const pts=daily.map((d,i)=>({
      x:i*(bw+gap)+gap+bw/2+leftPad,
      y:bl-Math.max(2,vals[i]/max*bMaxH),
      r:vals[i]/max,date:d.date,val:vals[i]
    }));
    if(__chartStyle==='area'){
      const grad=ctx.createLinearGradient(0,0,0,bl);
      grad.addColorStop(0,'rgba(255,255,255,.28)');
      grad.addColorStop(1,'rgba(255,255,255,.02)');
      ctx.fillStyle=grad;ctx.beginPath();
      ctx.moveTo(pts[0].x,bl);
      pts.forEach(p=>ctx.lineTo(p.x,p.y));
      ctx.lineTo(pts[pts.length-1].x,bl);
      ctx.closePath();ctx.fill();
    }
    ctx.strokeStyle='rgba(255,255,255,.7)';ctx.lineWidth=1.5;
    ctx.beginPath();
    pts.forEach((p,i)=>i===0?ctx.moveTo(p.x,p.y):ctx.lineTo(p.x,p.y));
    ctx.stroke();
    pts.forEach(p=>{
      hitsRef.push({x0:p.x-bw/2,x1:p.x+bw/2,cx:p.x,y:p.y,date:p.date,val:p.val});
      ctx.beginPath();ctx.arc(p.x,p.y,2,0,Math.PI*2);
      ctx.fillStyle='rgba(255,255,255,'+(0.45+0.55*p.r).toFixed(2)+')';
      ctx.fill();
    });
  }
}

function drawStackedBars(daily) {
  __chartHits.length = 0;
  const cv = document.getElementById('cv'), ctx = cv.getContext('2d');
  const dpr = window.devicePixelRatio||2, cw = cv.offsetWidth||300, ch = 90;
  cv.style.height = ch+'px'; cv.width = cw*dpr; cv.height = ch*dpr; ctx.scale(dpr,dpr);
  ctx.clearRect(0,0,cw,ch);
  if (!daily||!daily.length) return;
  const vals = daily.map(d=>d.tokens), max = Math.max(...vals,1), n = daily.length, gap = 2;
  const leftPad = 32, drawW = cw-leftPad;
  const bw = (drawW-gap)/n-gap, bMaxH = ch-18, bl = ch-10;
  ctx.font = '9px -apple-system,sans-serif'; ctx.textAlign='right'; ctx.textBaseline='middle';
  [0.25,0.5,0.75,1].forEach(function(lvl){
    const ly = bl-lvl*bMaxH;
    ctx.strokeStyle='rgba(255,255,255,.07)'; ctx.lineWidth=1;
    ctx.beginPath(); ctx.moveTo(leftPad,ly); ctx.lineTo(cw,ly); ctx.stroke();
    ctx.fillStyle='rgba(255,255,255,.22)'; ctx.fillText(fmt(lvl*max),leftPad-5,ly);
  });
  ctx.strokeStyle='rgba(255,255,255,.22)'; ctx.setLineDash([2,5]); ctx.lineWidth=1;
  ctx.beginPath(); ctx.moveTo(leftPad,bl+2); ctx.lineTo(cw,bl+2); ctx.stroke();
  ctx.setLineDash([]);
  daily.forEach(function(d,i){
    const total = d.tokens||1;
    const bh = Math.max(2, total/max*bMaxH);
    const x = i*(bw+gap)+gap+leftPad;
    const bd = __dailyBreakdown[d.date];
    if (bd && (bd.cr||bd.i||bd.cw||bd.o)) {
      let yOff = 0;
      BD_ORDER.forEach(function(key){
        const v = bd[key]||0; if (!v) return;
        const segH = Math.max(0, (v/total)*bh);
        const col = BD_COLORS[key];
        ctx.fillStyle = col.rgba + '0.82)';
        ctx.fillRect(x, bl-bh+yOff, bw, segH);
        yOff += segH;
      });
    } else {
      const r = total/max;
      ctx.fillStyle='rgba(255,255,255,'+(0.3+0.55*r).toFixed(2)+')';
      drawBar(ctx,x,bl-bh,bw,bh,2);
    }
    __chartHits.push({x0:x,x1:x+bw,cx:x+bw/2,y:bl-bh,date:d.date,val:total});
  });
}

function buildTipHtml(hit) {
  const bd = __dailyBreakdown[hit.date];
  const header = '<div style="font-weight:600;margin-bottom:5px;font-size:12px">'+fmtDate(hit.date)+'&nbsp;&nbsp;'+fmt(hit.val)+'</div>';
  if (!bd) return header;
  const total = hit.val||1;
  const rows = BD_ORDER.map(function(key){
    const v = bd[key]||0; if (!v) return '';
    const pct = Math.round(v/total*100);
    const col = BD_COLORS[key].hex;
    const label = {cr:'Cache R',i:'Input',r:'Reasoning',cw:'Cache W',o:'Output'}[key];
    return '<div style="display:flex;justify-content:space-between;gap:10px;font-size:11px">'
      +'<span><span style="color:'+col+'">●</span>&nbsp;'+label+'</span>'
      +'<span style="color:rgba(255,255,255,.7)">'+fmt(v)+'&nbsp;<span style="opacity:.45">'+pct+'%</span></span>'
      +'</div>';
  }).join('');
  return header+rows;
}

function drawChart(daily) {
  __lastDaily = daily || [];
  const filtered = filterByPeriod(__lastDaily);
  if (__chartStyle==='bars' && Object.keys(__dailyBreakdown).length>0) {
    drawStackedBars(filtered);
  } else {
    drawChartWith('cv', filtered, d=>d.tokens, __chartHits, true);
  }
}

function drawCostChart(daily) {
  __lastDailyCost = daily || [];
  drawChartWith('cv2', filterByPeriod(__lastDailyCost), d=>d.cost, __chartHits2, true);
}

(function(){
  function makeTip(cvId,tipId,hitsRef,fmtFn,isMain){
    const cv=document.getElementById(cvId);
    cv.addEventListener('mousemove',function(e){
      if(!hitsRef.length)return;
      const mx=e.offsetX;let hit=null;
      for(const h of hitsRef){if(mx>=h.x0&&mx<=h.x1){hit=h;break;}}
      const tip=document.getElementById(tipId);
      if(hit){
        if(isMain && Object.keys(__dailyBreakdown).length>0){
          tip.innerHTML=buildTipHtml(hit);
        }else{
          tip.textContent=fmtDate(hit.date)+'  '+fmtFn(hit.val);
        }
        tip.style.display='block';
        const th=tip.offsetHeight||22,tipW=tip.offsetWidth||160,winW=360;
        const left=Math.max(4,Math.min(e.clientX-tipW/2,winW-tipW-4));
        tip.style.left=left+'px';
        tip.style.top=Math.max(4,e.clientY-th-10)+'px';
      }else{tip.style.display='none';}
    });
    cv.addEventListener('mouseleave',function(){
      document.getElementById(tipId).style.display='none';
    });
  }
  function fmtC(c){if(!c||c<0.001)return'$0.000';if(c<0.01)return'$'+c.toFixed(3);return'$'+c.toFixed(2);}
  makeTip('cv','tip',__chartHits,fmt,true);
  makeTip('cv2','tip2',__chartHits2,fmtC,false);
})();

function renderQuota(d, settings) {
  const limit = parseFloat(settings.monthly_limit_usd || 0);
  const row = document.getElementById('quota-row');
  if (!limit || limit <= 0) { row.style.display = 'none'; return; }
  const now = new Date();
  const monthPfx = now.getFullYear() + '-' + String(now.getMonth()+1).padStart(2,'0');
  const allDaily = (d.all || {}).daily_cost || [];
  const costMonth = allDaily.reduce(function(s, e) {
    return e.date && e.date.startsWith(monthPfx) ? s + (e.cost || 0) : s;
  }, 0);
  const daysInMonth = new Date(now.getFullYear(), now.getMonth()+1, 0).getDate();
  const dayOfMonth = now.getDate();
  const proj = dayOfMonth > 0 ? costMonth / dayOfMonth * daysInMonth : 0;
  const pct = Math.min(100, Math.round(costMonth / limit * 100));
  document.getElementById('q-spent').textContent = '$' + costMonth.toFixed(2);
  document.getElementById('q-limit').textContent = '$' + limit.toFixed(2);
  document.getElementById('q-pct').textContent   = pct + '%';
  document.getElementById('q-proj').textContent  = '~$' + proj.toFixed(2);
  const bar = document.getElementById('q-bar');
  bar.style.width = pct + '%';
  bar.style.background = pct >= 90 ? '#f87171' : pct >= 70 ? '#fb923c' : '#4ade80';
  row.style.display = '';
}

function injectData(d) {
  __data = d;
  if(d.settings){__settings=d.settings;applySettings(d.settings)}
  if(d.limits != null){__limitsData = d.limits;}
  if(d.git_heatmap != null){__gitHeatmap = d.git_heatmap;}
  if(__onLimitsPage){
    renderLimits();
  } else {
    renderTab(__tab);
    renderQuota(d, __settings);
  }
  requestAnimationFrame(function(){
    try{window.webkit.messageHandlers.resize.postMessage(document.body.scrollHeight)}catch(e){}
  });
}

function applySettings(s){
  if(s.chart_style){__chartStyle=s.chart_style;
    document.getElementById('style-btn').textContent=s.chart_style}
  if(s.chart_period){__chartPeriod=s.chart_period;
    document.querySelectorAll('.cp').forEach(function(b){
      b.classList.toggle('active',b.getAttribute('data-p')===s.chart_period)
    })}
  if(s.accent_color){
    document.body.style.background=s.accent_color;
    document.documentElement.style.background=s.accent_color;
  }
}


function act(n,p){try{window.webkit.messageHandlers[n].postMessage(p||null)}catch(e){}}

function setColor(hex){
  __settings.accent_color=hex;
  act('saveSettings',JSON.stringify(__settings));
}

// ── Heatmap GitHub ───────────────────────────────────────────────────────────

let __limitsData = null;
let __onLimitsPage = false;
let __gitHeatmap = {};

function drawContribHeatmap() {
  var canvas = document.getElementById('contrib-canvas');
  if (!canvas) return;
  var heatmap = __gitHeatmap || {};
  var dpr = window.devicePixelRatio || 1;

  // Dernier mois = 5 semaines (lun→dim), grosses cellules
  var weeks = 5;
  var cell = 30;
  var gap = 4;
  var step = cell + gap;
  var leftPad = 34;
  var topPad = 22;
  var legH = 22;
  var gridW = weeks * step - gap;
  var totalW = leftPad + gridW + 4;
  var H = topPad + 7 * step - gap + legH + 8;

  canvas.width = totalW * dpr;
  canvas.height = H * dpr;
  canvas.style.width = totalW + 'px';
  canvas.style.height = H + 'px';
  var ctx = canvas.getContext('2d');
  ctx.scale(dpr, dpr);

  var colors = ['#21262d','#0e4429','#006d32','#26a641','#39d353'];
  function lvl(c) { return !c?0:c<=2?1:c<=5?2:c<=9?3:4; }

  // Démarrer au lundi de la semaine d'il y a (weeks-1) semaines
  var today = new Date(); today.setHours(0,0,0,0);
  var dayOfWeek = (today.getDay() + 6) % 7; // 0=lun … 6=dim
  var start = new Date(today);
  start.setDate(start.getDate() - dayOfWeek - (weeks - 1) * 7);

  var mois = ['Janvier','Février','Mars','Avril','Mai','Juin',
              'Juillet','Août','Septembre','Octobre','Novembre','Décembre'];
  ctx.font = '11px -apple-system,BlinkMacSystemFont,sans-serif';
  ctx.fillStyle = 'rgba(255,255,255,.45)';
  ctx.fillText(mois[today.getMonth()] + ' ' + today.getFullYear(), leftPad, 14);

  var d = new Date(start);
  for (var w = 0; w < weeks; w++) {
    for (var row = 0; row < 7; row++) {
      if (d <= today) {
        var ds = d.getFullYear() + '-'
          + String(d.getMonth()+1).padStart(2,'0') + '-'
          + String(d.getDate()).padStart(2,'0');
        var c = heatmap[ds] || 0;
        ctx.fillStyle = colors[lvl(c)];
        var x = leftPad + w * step;
        var y = topPad + row * step;
        ctx.beginPath();
        if (ctx.roundRect) { ctx.roundRect(x, y, cell, cell, 4); }
        else { ctx.rect(x, y, cell, cell); }
        ctx.fill();
      }
      d.setDate(d.getDate() + 1);
    }
  }

  // Labels jours : Lun / Mer / Ven
  ctx.font = '10px -apple-system,BlinkMacSystemFont,sans-serif';
  ctx.fillStyle = 'rgba(255,255,255,.38)';
  ['Lun','','Mer','','Ven','',''].forEach(function(lbl, i) {
    if (lbl) ctx.fillText(lbl, 0, topPad + i * step + cell * 0.72);
  });

  // Légende Less / More (petites cases 12px)
  var legY = topPad + 7 * step - gap + 16;
  var legX = leftPad + gridW - (5 * 16 - 2);
  ctx.font = '10px -apple-system,BlinkMacSystemFont,sans-serif';
  ctx.fillStyle = 'rgba(255,255,255,.35)';
  ctx.fillText('Less', legX - 34, legY);
  colors.forEach(function(col, i) {
    ctx.fillStyle = col;
    ctx.beginPath();
    if (ctx.roundRect) { ctx.roundRect(legX + i*16, legY - 11, 12, 12, 2); }
    else { ctx.rect(legX + i*16, legY - 11, 12, 12); }
    ctx.fill();
  });
  ctx.fillStyle = 'rgba(255,255,255,.35)';
  ctx.fillText('More', legX + 5*16 + 2, legY);
}

function switchToLimits() {
  __onLimitsPage = true;
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('[data-tab="limits"]').forEach(t => t.classList.add('active'));
  document.getElementById('page-main').style.display = 'none';
  document.getElementById('page-limits').style.display = 'block';
  renderLimits();
  requestAnimationFrame(function(){
    try{window.webkit.messageHandlers.resize.postMessage(document.body.scrollHeight)}catch(e){}
  });
}

function barColor(pct) {
  if (pct >= 90) return '#f87171';
  if (pct >= 70) return '#fb923c';
  return '#4ade80';
}

let __countdownTimers = [];

function clearCountdowns() {
  __countdownTimers.forEach(function(id) { clearInterval(id); });
  __countdownTimers = [];
}

function renderLimBar(name, usedPct, resetStr, resetTs) {
  const used = usedPct != null ? usedPct : 0;
  const color = barColor(used);
  const exhausted = used >= 100;
  const numId = 'lim-num-' + name.toLowerCase().replace(/\s+/g, '-');
  return '<div class="lim-bar">' +
    '<div class="lim-bar-top">' +
      '<span class="lim-bar-name">' + name + '</span>' +
      '<span class="lim-bar-num" id="' + numId + '" style="color:' + color + '">' +
        (exhausted && resetTs ? '&#x2026;' : used + '%') +
      '</span>' +
    '</div>' +
    '<div class="lim-track"><div class="lim-fill" style="width:' + used + '%;background:' + color + '"></div></div>' +
    '<div class="lim-bar-sub">' +
      '<span>' + (exhausted ? 'Limite atteinte' : used + '% utilis&#233;') + '</span>' +
      (resetStr ? '<span>Reset ' + resetStr + '</span>' : '') +
    '</div>' +
  '</div>';
}

function startCountdown(elId, usedPct, resetTs) {
  if (usedPct < 100 || !resetTs) return;
  const el = document.getElementById(elId);
  if (!el) return;
  function tick() {
    const diff = resetTs - Date.now() / 1000;
    if (diff <= 0) { el.textContent = '0s'; return true; }
    const h = Math.floor(diff / 3600);
    const m = Math.floor((diff % 3600) / 60);
    const s = Math.floor(diff % 60);
    if (h > 0) el.textContent = h + 'h ' + m + 'm ' + s + 's';
    else if (m > 0) el.textContent = m + 'm ' + s + 's';
    else el.textContent = s + 's';
    return false;
  }
  tick();
  var id = setInterval(function() { if (tick()) clearInterval(id); }, 1000);
  __countdownTimers.push(id);
}

function renderUsageSummary() {
  if (!__data) return '';
  var rows = [
    {label:'Claude Code', key:'claude_code'},
    {label:'OpenCode',    key:'opencode'},
    {label:'Codex',       key:'codex'},
  ];
  var html = '<div style="padding:12px 20px 10px;border-bottom:1px solid rgba(255,255,255,.07)">'
    + '<div style="font-size:10px;font-weight:600;letter-spacing:.05em;text-transform:uppercase;'
    + 'color:rgba(255,255,255,.28);margin-bottom:10px">Aujourd&#39;hui</div>';
  rows.forEach(function(r) {
    var s = __data[r.key];
    if (!s || !s.today_tok) return;
    var cost = s.cost_today > 0 ? ' · $' + s.cost_today.toFixed(3) : '';
    html += '<div style="display:flex;justify-content:space-between;align-items:baseline;'
      + 'margin-bottom:5px;font-size:11px">'
      + '<span style="color:rgba(255,255,255,.45)">' + r.label + '</span>'
      + '<span style="color:rgba(255,255,255,.85);font-variant-numeric:tabular-nums">'
      + fmt(s.today_tok) + ' tok' + cost + '</span></div>';
  });
  var all = __data.all;
  if (all && all.today_tok) {
    var totalCost = all.cost_today > 0 ? ' · $' + all.cost_today.toFixed(3) : '';
    html += '<div style="display:flex;justify-content:space-between;align-items:baseline;'
      + 'padding-top:6px;border-top:1px solid rgba(255,255,255,.06);font-size:12px;font-weight:600">'
      + '<span style="color:rgba(255,255,255,.7)">Total</span>'
      + '<span>' + fmt(all.today_tok) + ' tok' + totalCost + '</span></div>';
  }
  return html + '</div>';
}

function renderLimits() {
  clearCountdowns();
  const lim = __limitsData;
  const el = document.getElementById('lim-body');

  var heatmapHtml = '<div style="padding:14px 16px 6px">'
    + '<div style="font-size:10px;font-weight:600;letter-spacing:.05em;text-transform:uppercase;'
    + 'color:rgba(255,255,255,.28);margin-bottom:8px">Contributions</div>'
    + '<canvas id="contrib-canvas" style="display:block"></canvas>'
    + '</div>';

  if (!lim) {
    el.innerHTML = heatmapHtml + renderUsageSummary()
      + '<div class="lim-loading">Chargement&#x2026;<br><span style="font-size:10px;opacity:.5">~10s au premier lancement</span></div>';
    drawContribHeatmap(); return;
  }
  if (lim.error && lim.session_used == null && lim.week_used == null) {
    el.innerHTML = heatmapHtml + renderUsageSummary()
      + '<div class="lim-error">&#x26A0;&#xFE0F; ' + lim.error + '</div>';
    drawContribHeatmap(); return;
  }
  var html = heatmapHtml + renderUsageSummary() + '<div class="lim-body">';
  if (lim.plan) html += '<div class="lim-plan">' + lim.plan + '</div>';
  if (lim.session_used != null) html += renderLimBar('Session (5h)', lim.session_used, lim.session_reset, lim.session_reset_ts);
  if (lim.week_used != null)    html += renderLimBar('Semaine', lim.week_used, lim.week_reset, lim.week_reset_ts);
  if (lim.opus_used != null)    html += renderLimBar('Opus / Sonnet', lim.opus_used, lim.opus_reset, lim.opus_reset_ts);
  html += '</div>';
  el.innerHTML = html;
  drawContribHeatmap();
  startCountdown('lim-num-session-(5h)', lim.session_used, lim.session_reset_ts);
  startCountdown('lim-num-semaine', lim.week_used, lim.week_reset_ts);
  startCountdown('lim-num-opus-/-sonnet', lim.opus_used, lim.opus_reset_ts);
}

"""

MODELS_HTML_TMPL = """\
<!DOCTYPE html><html><head><meta charset="utf-8">
<style>
*{margin:0;padding:0;box-sizing:border-box}
html,body{width:100%;height:100vh;background:#1c1c1e;color:#fff;
  font-family:-apple-system,BlinkMacSystemFont,"SF Pro Text",sans-serif;
  display:flex;flex-direction:column;overflow:hidden;-webkit-font-smoothing:antialiased}
.search{padding:12px 14px;border-bottom:1px solid rgba(255,255,255,.1);flex-shrink:0}
input{width:100%;background:rgba(255,255,255,.1);border:1px solid rgba(255,255,255,.12);
  border-radius:8px;padding:7px 12px;color:#fff;font-size:13px;outline:none;font-family:inherit}
input::placeholder{color:rgba(255,255,255,.35)}
input:focus{background:rgba(255,255,255,.14);border-color:rgba(255,255,255,.25)}
.periods{display:flex;align-items:center;padding:0 14px;border-bottom:1px solid rgba(255,255,255,.08);flex-shrink:0}
.period{padding:8px 10px 7px;font-size:11px;font-weight:500;color:rgba(255,255,255,.32);
  cursor:pointer;border-bottom:2px solid transparent;margin-bottom:-1px;
  user-select:none;transition:color .15s}
.period:hover:not(.active){color:rgba(255,255,255,.6)}
.period.active{color:#fff;border-bottom-color:rgba(255,255,255,.55)}
.info-wrap{margin-left:auto;position:relative;display:flex;align-items:center}
.info-btn{background:none;border:1px solid rgba(255,255,255,.14);color:rgba(255,255,255,.3);
  font-size:10px;padding:1px 5px;border-radius:9px;cursor:default;user-select:none;line-height:1.5}
.info-wrap:hover .info-btn{color:rgba(255,255,255,.65);border-color:rgba(255,255,255,.35)}
.info-tip{display:none;position:absolute;right:0;top:calc(100% + 6px);
  background:rgba(20,20,22,.98);border:1px solid rgba(255,255,255,.12);
  border-radius:8px;padding:10px 12px;font-size:11px;line-height:1.7;
  color:rgba(255,255,255,.72);white-space:nowrap;z-index:100}
.info-wrap:hover .info-tip{display:block}
.tip-src{color:rgba(255,255,255,.32);font-size:9.5px;text-transform:uppercase;
  letter-spacing:.06em;margin-top:7px;margin-bottom:1px}
.tip-src:first-child{margin-top:0}
.tip-row{display:flex;justify-content:space-between;gap:18px}
.tip-price{color:rgba(255,255,255,.38);font-variant-numeric:tabular-nums}
.tip-sub{opacity:.55;font-size:10px}
.tip-note{margin-top:7px;padding-top:6px;border-top:1px solid rgba(255,255,255,.08);
  color:rgba(255,255,255,.2);font-size:9.5px}
.count{font-size:11px;color:rgba(255,255,255,.3);padding:7px 14px 3px}
.list{flex:1;overflow-y:auto;padding:4px 0 8px}
.list::-webkit-scrollbar{width:4px}
.list::-webkit-scrollbar-thumb{background:rgba(255,255,255,.2);border-radius:2px}
.row{display:flex;align-items:center;padding:9px 14px;gap:10px}
.row:hover{background:rgba(255,255,255,.05)}
.rank{font-size:11px;color:rgba(255,255,255,.25);width:20px;flex-shrink:0;text-align:right}
.info{flex:1;min-width:0}
.name{font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin-bottom:4px}
.bar-wrap{height:3px;background:rgba(255,255,255,.1);border-radius:2px}
.bar-fill{height:100%;background:rgba(255,255,255,.55);border-radius:2px}
.right{text-align:right;flex-shrink:0;width:68px}
.tok{font-size:12px;color:rgba(255,255,255,.8)}
.cost{font-size:10px;color:rgba(255,255,255,.35);margin-top:2px}
.badge{display:inline-block;font-size:9px;padding:1px 5px;border-radius:3px;
  border:1px solid rgba(255,255,255,.15);color:rgba(255,255,255,.42);
  margin-left:5px;vertical-align:middle}
.empty{padding:24px 16px;color:rgba(255,255,255,.3);font-size:13px;text-align:center}
</style></head><body>
<div class="search">
  <input id="q" type="text" placeholder="Search model or source&#x2026;" autofocus>
</div>
<div class="periods">
  <div class="period" data-p="all" onclick="setPeriod('all')">All</div>
  <div class="period" data-p="1m"  onclick="setPeriod('1m')">1m</div>
  <div class="period" data-p="7d"  onclick="setPeriod('7d')">7d</div>
  <div class="period" data-p="1d"  onclick="setPeriod('1d')">1d</div>
  <div class="info-wrap">
    <button class="info-btn">i</button>
    <div class="info-tip">
      <div class="tip-src">Anthropic</div>
      <div class="tip-row"><span>Opus 4.8</span><span class="tip-price">$5 / $25</span></div>
      <div class="tip-row"><span>Sonnet 4.6</span><span class="tip-price">$3 / $15</span></div>
      <div class="tip-row"><span>Haiku 4.5</span><span class="tip-price">$1 / $5</span></div>
      <div class="tip-src">OpenAI</div>
      <div class="tip-row"><span>GPT-5.5</span><span class="tip-price">$5 / $30</span></div>
      <div class="tip-row"><span>GPT-5.4-mini</span><span class="tip-price">$0.75 / $4.50</span></div>
      <div class="tip-src">DeepSeek</div>
      <div class="tip-row"><span>V4 Flash</span><span class="tip-price">$0.14 / $0.28</span></div>
      <div class="tip-row tip-sub"><span>&#x2514; cache hit</span><span class="tip-price">$0.0028</span></div>
      <div class="tip-src">Xiaomi</div>
      <div class="tip-row"><span>MiMo V2.5 Free</span><span class="tip-price">$0.14 / $0.28</span></div>
      <div class="tip-row tip-sub"><span>&#x2514; cache hit</span><span class="tip-price">$0.0028</span></div>
      <div class="tip-note">input / output &mdash; $/M tokens &middot; estimated</div>
    </div>
  </div>
</div>
<div class="count" id="count"></div>
<div class="list" id="list"></div>
<script>
const DATA=MODELS_PLACEHOLDER;
let __period='all';
let __q='';
function fmt(n){if(!n)return'0';if(n>=1e9)return(n/1e9).toFixed(1)+'B';if(n>=1e6)return(n/1e6).toFixed(1)+'M';if(n>=1e3)return(n/1e3).toFixed(1)+'k';return''+n}
function fmtCost(c){if(!c||c<0.001)return'';if(c<0.01)return'~$'+c.toFixed(3);return'~$'+c.toFixed(2)}
function setPeriod(p){
  __period=p;
  document.querySelectorAll('.period').forEach(el=>el.classList.toggle('active',el.dataset.p===p));
  render(filtered());
}
function filtered(){
  const items=DATA[__period]||[];
  return __q?items.filter(m=>m.name.toLowerCase().includes(__q)||m.source.toLowerCase().includes(__q)):items;
}
function render(items){
  document.getElementById('count').textContent=items.length+' model'+(items.length!==1?'s':'');
  const el=document.getElementById('list');
  if(!items.length){el.innerHTML='<div class="empty">No results</div>';return}
  const max=items[0]?.tokens||1;
  el.innerHTML=items.map((m,i)=>{
    const pct=Math.max(2,Math.round(m.tokens/max*100));
    const cs=fmtCost(m.cost);
    return'<div class="row">'+
      '<div class="rank">'+(i+1)+'</div>'+
      '<div class="info">'+
        '<div class="name">'+m.name+'<span class="badge">'+m.source+'</span></div>'+
        '<div class="bar-wrap"><div class="bar-fill" style="width:'+pct+'%"></div></div>'+
      '</div>'+
      '<div class="right"><div class="tok">'+fmt(m.tokens)+'</div>'+(cs?'<div class="cost">'+cs+'</div>':'')+
      '</div>'+
    '</div>';
  }).join('');
}
document.getElementById('q').addEventListener('input',function(){__q=this.value.toLowerCase();render(filtered());});
setPeriod('all');
</script></body></html>
"""

SETTINGS_HTML_TMPL = """\
<!DOCTYPE html><html><head><meta charset="utf-8">
<style>
*{margin:0;padding:0;box-sizing:border-box}
html,body{width:100%;height:100vh;background:#1c1c1e;color:#fff;
  font-family:-apple-system,BlinkMacSystemFont,"SF Pro Text",sans-serif;
  overflow:hidden;-webkit-font-smoothing:antialiased;display:flex;flex-direction:column}
.settings-header{display:flex;align-items:center;padding:10px 14px;border-bottom:1px solid rgba(255,255,255,.08);flex-shrink:0}
.settings-title{font-size:13px;font-weight:600;color:rgba(255,255,255,.7)}
.settings-scroll{padding:10px 16px 16px;overflow-y:auto;flex:1}
.settings-scroll::-webkit-scrollbar{width:4px}
.settings-scroll::-webkit-scrollbar-thumb{background:rgba(255,255,255,.2);border-radius:2px}
.settings-section{margin-bottom:18px}
.settings-label{font-size:10px;font-weight:600;letter-spacing:.06em;text-transform:uppercase;color:rgba(255,255,255,.35);margin-bottom:8px}
.settings-desc{font-size:10px;color:rgba(255,255,255,.22);margin-bottom:8px;line-height:1.5}
.settings-row{display:flex;gap:6px;align-items:center;margin-top:6px}
.sbtn{background:rgba(255,255,255,.08);border:1px solid rgba(255,255,255,.1);color:rgba(255,255,255,.7);border-radius:6px;padding:5px 12px;font-size:11px;cursor:pointer;font-family:inherit;white-space:nowrap}
.sbtn:hover{background:rgba(255,255,255,.14);color:#fff}
.sbtn.danger{border-color:rgba(255,80,80,.2);color:rgba(255,100,100,.6)}
.sbtn.danger:hover{background:rgba(255,60,60,.12);color:rgba(255,90,90,.9)}
.settings-input{background:rgba(255,255,255,.07);border:1px solid rgba(255,255,255,.1);border-radius:6px;padding:5px 8px;color:#fff;font-size:12px;outline:none;font-family:inherit;width:100%}
.settings-input:focus{border-color:rgba(255,255,255,.28)}
.settings-input.narrow{width:72px}
.time-input{background:rgba(255,255,255,.07);border:1px solid rgba(255,255,255,.1);border-radius:6px;padding:5px 4px;color:#fff;font-size:13px;outline:none;font-family:inherit;width:40px;text-align:center}
.time-input:focus{border-color:rgba(255,255,255,.28)}
.time-input::-webkit-inner-spin-button,.time-input::-webkit-outer-spin-button{-webkit-appearance:none;margin:0}
.settings-hint{font-size:10px;color:rgba(255,255,255,.2);margin-top:4px}
.toggle-wrap{position:relative;display:inline-block;width:36px;height:20px;flex-shrink:0}
.toggle-wrap input{opacity:0;width:0;height:0}
.toggle-track{position:absolute;cursor:pointer;top:0;left:0;right:0;bottom:0;background:rgba(255,255,255,.15);border-radius:10px;transition:background .2s}
.toggle-track::before{content:'';position:absolute;width:16px;height:16px;left:2px;bottom:2px;background:#fff;border-radius:50%;transition:transform .2s}
.toggle-wrap input:checked+.toggle-track{background:#5b9cf6}
.toggle-wrap input:checked+.toggle-track::before{transform:translateX(16px)}
.settings-divider{border:none;border-top:1px solid rgba(255,255,255,.06);margin:10px 0}
.tag{display:inline-flex;align-items:center;gap:3px;background:rgba(255,255,255,.08);border-radius:4px;padding:2px 7px;font-size:11px;color:rgba(255,255,255,.55);margin:2px 4px 2px 0}
.tag .tag-del{background:none;border:none;color:rgba(255,100,100,.4);cursor:pointer;font-size:12px;padding:0;line-height:1;margin-left:2px}
.tag .tag-del:hover{color:rgba(255,80,80,.9)}
.tags-wrap{display:flex;flex-wrap:wrap;margin-top:6px}
.swatches{display:flex;gap:8px;flex-wrap:wrap;margin-top:6px}
.swatch{width:26px;height:26px;border-radius:50%;border:2px solid transparent;cursor:pointer;padding:0;outline:none;transition:transform .1s,border-color .1s;position:relative}
.swatch:hover{transform:scale(1.12)}
.swatch.active{border-color:rgba(255,255,255,.75)}
.swatch.active::after{content:'';position:absolute;inset:-4px;border-radius:50%;border:1px solid rgba(255,255,255,.2)}
</style></head><body>
<div class="settings-header">
  <span class="settings-title">Settings</span>
</div>
<div class="settings-scroll">

<div class="settings-section">
  <div class="settings-label">Color</div>
  <div class="swatches" id="color-swatches">
    <button class="swatch" data-color="#1c1c1e" style="background:#636366" onclick="setColor('#1c1c1e')" title="Gray"></button>
    <button class="swatch" data-color="#09090b" style="background:#27272a" onclick="setColor('#09090b')" title="Black"></button>
    <button class="swatch" data-color="#0c1829" style="background:#2563eb" onclick="setColor('#0c1829')" title="Blue"></button>
    <button class="swatch" data-color="#0d1a0f" style="background:#16a34a" onclick="setColor('#0d1a0f')" title="Green"></button>
    <button class="swatch" data-color="#160d26" style="background:#7c3aed" onclick="setColor('#160d26')" title="Purple"></button>
    <button class="swatch" data-color="#220d0d" style="background:#dc2626" onclick="setColor('#220d0d')" title="Red"></button>
    <button class="swatch" data-color="#0d1a1c" style="background:#0891b2" onclick="setColor('#0d1a1c')" title="Cyan"></button>
    <button class="swatch" data-color="#1a1400" style="background:#d97706" onclick="setColor('#1a1400')" title="Amber"></button>
  </div>
</div>

<hr class="settings-divider">

<div class="settings-section">
  <div class="settings-label">Excluded models</div>
  <div class="settings-desc">Tokens from these models are ignored in all calculations.</div>
  <div class="tags-wrap" id="excl-list"></div>
  <div class="settings-row">
    <input class="settings-input" id="new-excl" placeholder="model name&#x2026;">
    <button class="sbtn" onclick="addExcl()">Add</button>
  </div>
</div>

<hr class="settings-divider">

<div class="settings-section">
  <div class="settings-label">Refresh rate</div>
  <div class="settings-row">
    <input class="settings-input narrow" id="refresh-interval" type="number" min="5" max="300" onchange="saveSettings()">
    <span style="font-size:11px;color:rgba(255,255,255,.35)">seconds</span>
  </div>
</div>

<hr class="settings-divider">

<div class="settings-section">
  <div class="settings-label">Charts</div>
  <div class="settings-row" style="gap:10px">
    <span style="font-size:11px;color:rgba(255,255,255,.45);min-width:36px">Style</span>
    <select class="settings-input narrow" id="chart-style" onchange="saveSettings()">
      <option value="bars">Bars</option>
      <option value="line">Line</option>
      <option value="area">Area</option>
    </select>
    <span style="font-size:11px;color:rgba(255,255,255,.45);min-width:42px;margin-left:4px">Period</span>
    <select class="settings-input narrow" id="chart-period" onchange="saveSettings()">
      <option value="1d">1d</option>
      <option value="7d">7d</option>
      <option value="1m">1 month</option>
      <option value="all">All</option>
    </select>
  </div>
</div>

<hr class="settings-divider">

<div class="settings-section">
  <div class="settings-label">Daily notification</div>
  <div class="settings-desc">Get a daily summary at a set time with a Flex button.</div>
  <div class="settings-row" style="gap:10px;flex-wrap:wrap">
    <label class="toggle-wrap">
      <input type="checkbox" id="notify-enabled" onchange="saveSettings()">
      <span class="toggle-track"></span>
    </label>
    <span style="font-size:12px;color:rgba(255,255,255,.55);margin-right:14px" id="notify-enabled-label">Off</span>
    <span style="font-size:11px;color:rgba(255,255,255,.45)">Time</span>
    <input class="time-input" id="notify-hour" type="number" min="0" max="23" value="20" onchange="saveSettings()">
    <span style="color:rgba(255,255,255,.35);font-size:14px">:</span>
    <input class="time-input" id="notify-min" type="number" min="0" max="59" value="0" onchange="saveSettings()">
  </div>
</div>

<hr class="settings-divider">

<div class="settings-section">
  <div class="settings-label">Launch at login</div>
  <div class="settings-desc">Start Tokenbar automatically when you log in.</div>
  <div class="settings-row" style="gap:10px">
    <label class="toggle-wrap">
      <input type="checkbox" id="login-start" onchange="saveSettings()">
      <span class="toggle-track"></span>
    </label>
    <span style="font-size:12px;color:rgba(255,255,255,.55)" id="login-start-label">Off</span>
  </div>
</div>

<hr class="settings-divider">

<div class="settings-section">
  <div class="settings-label">Alerts</div>
  <div class="tags-wrap" id="alert-list"></div>
  <div class="settings-row" style="gap:8px;flex-wrap:wrap;margin-top:10px">
    <select class="settings-input narrow" id="alert-type" style="width:64px">
      <option value="tokens">Tokens</option>
      <option value="cost">Cost</option>
    </select>
    <span style="color:rgba(255,255,255,.3);font-size:11px">exceeds</span>
    <input class="settings-input narrow" id="alert-value" type="number" min="0.1" step="1" value="10000" placeholder="value" style="width:76px;flex:none">
    <label style="display:flex;align-items:center;gap:5px;margin-left:4px;font-size:11px;color:rgba(255,255,255,.45);cursor:pointer;user-select:none;white-space:nowrap">
      <input type="checkbox" id="alert-step" style="accent-color:#7c6af7;width:13px;height:13px">
      repeat
    </label>
    <button class="sbtn" onclick="addAlert()" style="font-size:12px;padding:5px 14px;margin-left:auto">Add</button>
  </div>
</div>

<hr class="settings-divider">

<div class="settings-section">
  <div class="settings-label">Quota mensuel Claude ($)</div>
  <div class="settings-desc">Affiche une barre de progression dans le popover (dépense du mois vs limite). Laisser à 0 pour désactiver.</div>
  <div class="settings-row">
    <span style="font-size:11px;color:rgba(255,255,255,.45);margin-right:4px">$</span>
    <input class="settings-input narrow" id="monthly-limit" type="number" min="0" step="10" placeholder="0" onchange="saveSettings()" style="width:80px">
    <span style="font-size:10px;color:rgba(255,255,255,.25);margin-left:6px">/ mois</span>
  </div>
</div>

<hr class="settings-divider">

<div class="settings-section">
  <div class="settings-label">DeepSeek API key</div>
  <div class="settings-desc">Affiche ton solde restant dans le popover.</div>
  <div class="settings-row">
    <input class="settings-input" id="ds-key" type="password" placeholder="sk-…" onchange="saveSettings()">
  </div>
</div>

<hr class="settings-divider">

<div class="settings-section">
  <div class="settings-label">Time filter</div>
  <div class="settings-desc">Reset the start time to include all tokens.</div>
  <button class="sbtn danger" id="reset-btn" onclick="resetStart()">Reset</button>
  <div class="settings-hint" id="start-hint"></div>
</div>

</div>
<script>
const SETTINGS = SETTINGS_PLACEHOLDER;
function act(n,p){try{window.webkit.messageHandlers[n].postMessage(p||null)}catch(e){}}
function fmtNum(n){if(!n)return'0';if(n>=1e9)return(n/1e9).toFixed(1)+'B';if(n>=1e6)return(n/1e6).toFixed(1)+'M';if(n>=1e3)return(n/1e3).toFixed(1)+'k';return''+n}
function setColor(hex){SETTINGS.accent_color=hex;act('saveSettings',JSON.stringify(SETTINGS))}
function collectSettings(){
  var excl=[];document.querySelectorAll('#excl-list .tag').forEach(function(t){var v=t.getAttribute('data-val');if(v)excl.push(v)});
  var alerts=[];document.querySelectorAll('#alert-list .tag').forEach(function(t){try{alerts.push(JSON.parse(t.getAttribute('data-val')))}catch(e){}});
  return{excluded_models:excl,refresh_interval:parseInt(document.getElementById('refresh-interval').value)||15,chart_style:document.getElementById('chart-style').value,chart_period:document.getElementById('chart-period').value,accent_color:SETTINGS.accent_color||'#1c1c1e',notify_enabled:document.getElementById('notify-enabled').checked,notify_time:(document.getElementById('notify-hour').value.padStart(2,'0')+':'+document.getElementById('notify-min').value.padStart(2,'0')),login_start:document.getElementById('login-start').checked,alerts:alerts,deepseek_api_key:document.getElementById('ds-key').value,monthly_limit_usd:parseFloat(document.getElementById('monthly-limit').value)||0}
}
function saveSettings(){var s=collectSettings();Object.assign(SETTINGS,s);act('saveSettings',JSON.stringify(s))}
function addExcl(){
  var v=document.getElementById('new-excl').value.trim().toLowerCase();
  if(!v)return;
  for(var i=0;i<document.querySelectorAll('#excl-list .tag').length;i++){if(document.querySelectorAll('#excl-list .tag')[i].getAttribute('data-val')===v)return}
  var el=document.createElement('span');el.className='tag';el.setAttribute('data-val',v);
  el.innerHTML=v+'<button class="tag-del" onclick="delExcl(this)" title="Remove">&#x2715;</button>';
  document.getElementById('excl-list').appendChild(el);document.getElementById('new-excl').value='';saveSettings()
}
function delExcl(btn){btn.parentElement.remove();saveSettings()}
function renderAlerts(alerts){
  var el=document.getElementById('alert-list');if(!el)return;
  el.innerHTML='';(alerts||[]).forEach(function(a,i){
    var fval=a.type==='cost'?'$'+a.value:fmtNum(a.value);
    var label=fval+(a.step?' · repeat':'');
    var sp=document.createElement('span');sp.className='tag';sp.setAttribute('data-val',JSON.stringify(a));
    sp.innerHTML='<span style="opacity:.45;margin-right:2px">'+(a.type==='cost'?'cost':'tokens')+'</span>'+label+'<button class="tag-del" onclick="delAlert(this)" title="Remove">&#x2715;</button>';
    el.appendChild(sp)
  })
}
function addAlert(){
  var type=document.getElementById('alert-type').value;
  var val=parseFloat(document.getElementById('alert-value').value);
  if(!val||val<=0)return;
  var step=document.getElementById('alert-step').checked;
  document.getElementById('alert-value').value='';document.getElementById('alert-step').checked=false;
  var existing=SETTINGS.alerts||[];existing.push({type:type,value:val,period:'all',step:step});
  SETTINGS.alerts=existing;renderAlerts(existing);saveSettings()
}
function delAlert(btn){
  var tag=btn.parentElement;
  var idx=Array.from(tag.parentElement.children).indexOf(tag);
  var alerts=SETTINGS.alerts||[];alerts.splice(idx,1);
  SETTINGS.alerts=alerts;renderAlerts(alerts);saveSettings()
}
function resetStart(){
  var btn=document.getElementById('reset-btn');
  if(btn.getAttribute('data-confirm')!=='1'){btn.textContent='Confirm?';btn.setAttribute('data-confirm','1');setTimeout(function(){btn.textContent='Reset';btn.removeAttribute('data-confirm')},3000);return}
  act('saveSettings',JSON.stringify({reset_start:true}));
  document.getElementById('start-hint').textContent='Reset. Restart recommended.';
  btn.textContent='Reset';btn.removeAttribute('data-confirm')
}
function renderSettings(s){
  var el=document.getElementById('excl-list');el.innerHTML='';
  (s.excluded_models||[]).forEach(function(v){var sp=document.createElement('span');sp.className='tag';sp.setAttribute('data-val',v);sp.innerHTML=v+'<button class="tag-del" onclick="delExcl(this)" title="Remove">&#x2715;</button>';el.appendChild(sp)});
  document.getElementById('refresh-interval').value=s.refresh_interval||15;
  document.getElementById('chart-style').value=s.chart_style||'bars';
  document.getElementById('chart-period').value=s.chart_period||'1m';
  var color=s.accent_color||'#1c1c1e';
  document.body.style.background=color;document.documentElement.style.background=color;
  document.querySelectorAll('.swatch').forEach(function(b){b.classList.toggle('active',b.getAttribute('data-color')===color)});
  renderAlerts(s.alerts);
  document.getElementById('notify-enabled').checked=s.notify_enabled||false;
  document.getElementById('notify-enabled-label').textContent=s.notify_enabled?'On':'Off';
  if(s.notify_time){var p=s.notify_time.split(':');if(p.length==2){document.getElementById('notify-hour').value=p[0];document.getElementById('notify-min').value=p[1]}}
  document.getElementById('login-start').checked=s.login_start||false;
  document.getElementById('login-start-label').textContent=s.login_start?'On':'Off';
  document.getElementById('ds-key').value=s.deepseek_api_key||'';
  document.getElementById('monthly-limit').value=s.monthly_limit_usd||''
}
renderSettings(SETTINGS);
</script></body></html>
"""


# ── PyObjC ────────────────────────────────────────────────────────────────────

class MsgHandler(NSObject):
    _app = None
    def userContentController_didReceiveScriptMessage_(self, uc, msg):
        n = msg.name()
        if   n == "refresh" and self._app: self._app.inject_data()
        elif n == "quit":                  NSApp.terminate_(None)
        elif n == "resize"  and self._app: self._app.resize_popover(int(msg.body()))
        elif n == "models"  and self._app: self._app.show_models_window()
        elif n == "flex"    and self._app: self._app.flex()
        elif n == "saveSettings" and self._app: self._app.save_settings_(msg.body())
        elif n == "settings"  and self._app: self._app.show_settings_window()
        elif n == "refreshLimits" and self._app: self._app.refresh_limits()


class NavDelegate(NSObject):
    _app = None
    def webView_didFinishNavigation_(self, wv, nav):
        if self._app: self._app.bootstrap_and_inject()


class AppDelegate(NSObject):

    def applicationDidFinishLaunching_(self, _):
        NSApp.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
        self._models_win = None
        self._settings_win = None
        self._timer = None
        self._notified_date = None
        self._alerted_threshold = {}

        NSUserNotificationCenter.defaultUserNotificationCenter().setDelegate_(self)

        self._bar  = NSStatusBar.systemStatusBar()
        self._item = self._bar.statusItemWithLength_(NSVariableStatusItemLength)
        btn = self._item.button()
        btn.setTitle_("…")
        btn.setTarget_(self)
        btn.setAction_("toggle:")

        self._msg = MsgHandler.alloc().init(); self._msg._app = self
        self._nav = NavDelegate.alloc().init(); self._nav._app = self

        cfg = WKWebViewConfiguration.alloc().init()
        uc  = cfg.userContentController()
        for n in ("refresh", "quit", "resize", "models", "saveSettings", "flex", "settings", "refreshLimits"):
            uc.addScriptMessageHandler_name_(self._msg, n)


        frame    = NSMakeRect(0, 0, W, H)
        self._wv = WKWebView.alloc().initWithFrame_configuration_(frame, cfg)
        self._wv.setNavigationDelegate_(self._nav)
        self._wv.setOpaque_(False)
        self._wv.setBackgroundColor_(NSColor.clearColor())
        _base_url = NSURL.fileURLWithPath_(str(Path.home()) + "/")
        self._wv.loadHTMLString_baseURL_(MAIN_HTML, _base_url)

        vd = NSAppearance.appearanceNamed_("NSAppearanceNameVibrantDark")
        ef = NSVisualEffectView.alloc().initWithFrame_(frame)
        ef.setMaterial_(2); ef.setBlendingMode_(0); ef.setState_(1); ef.setAppearance_(vd)

        view = NSView.alloc().initWithFrame_(frame)
        view.addSubview_(ef); view.addSubview_(self._wv)
        vc = NSViewController.alloc().init(); vc.setView_(view)

        self._pop = NSPopover.alloc().init()
        self._pop.setContentSize_(NSSize(W, H))
        self._pop.setContentViewController_(vc)
        self._pop.setBehavior_(NSPopoverBehaviorTransient)
        self._pop.setAnimates_(True)
        self._pop.setAppearance_(vd)

        threading.Thread(target=_refresh_limits_bg, daemon=True).start()
        self._start_timer()

    @objc.python_method
    def _start_timer(self):
        if self._timer:
            self._timer.invalidate()
        self._timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            get_refresh(), self, "tick:", None, True
        )

    def toggle_(self, sender):
        if self._pop.isShown():
            self._pop.performClose_(sender)
        else:
            btn = self._item.button()
            self._pop.showRelativeToRect_ofView_preferredEdge_(btn.bounds(), btn, 1)
            app_ref = self
            def _check_and_inject(result, error):
                if result:
                    app_ref.inject_data()
                else:
                    app_ref.bootstrap_and_inject()
            self._wv.evaluateJavaScript_completionHandler_(
                "typeof injectData !== 'undefined'", _check_and_inject)

    def tick_(self, _):
        data = fetch()
        if data:
            self._item.button().setTitle_(_navbar_title(data['all']['today_tok']))
        if self._pop.isShown():
            self._inject_js(data)
        if not hasattr(self, '_login_start_synced'):
            self._ensure_login_start()
        self.check_daily_notification()
        self._check_alerts(data)

    @objc.python_method
    def _ensure_login_start(self):
        enabled = _SETTINGS.get("login_start", False)
        exists = LAUNCH_AGENT_PATH.exists()
        if enabled and not exists:
            enable_login_start()
        elif not enabled and exists:
            disable_login_start()

    @objc.python_method
    def _check_alerts(self, data):
        if not data:
            return
        alerts = _SETTINGS.get("alerts", [])
        if not alerts:
            return
        s = data["all"]
        today_str = datetime.now().strftime("%Y-%m-%d")
        for a in alerts:
            try:
                typ = a.get("type", "cost")
                val = float(a.get("value", 10))
                period = a.get("period", "today")
                step = a.get("step", False)
                is_cost = typ == "cost"
                if is_cost:
                    current = s["cost_today"] if period == "today" else s["cost_all"]
                else:
                    current = s["today_tok"] if period == "today" else s["all_tok"]
                if current is None or current < val:
                    continue
                today_str = datetime.now().strftime("%Y-%m-%d")
                aid = f"{typ}_{period}_{val}_{step}"
                if period == "today":
                    aid += f"_{today_str}"
                if step:
                    n = int(current // val)
                    last = self._alerted_threshold.get(aid, 0)
                    if n <= last:
                        continue
                    self._alerted_threshold[aid] = n
                else:
                    if self._alerted_threshold.get(aid):
                        continue
                    self._alerted_threshold[aid] = True
                label = "Cost" if is_cost else "Tokens"
                unit = f"${current:.2f}" if is_cost else fmt(int(current))
                limit = f"${val:.2f}" if is_cost else fmt(int(val))
                notif = NSUserNotification.alloc().init()
                notif.setTitle_("Tokenbar — Alert")
                notif.setInformativeText_(f"{label}: {unit} ({limit} threshold)")
                notif.setActionButtonTitle_("Flex on X")
                notif.setUserInfo_({"action": "flex"})
                NSUserNotificationCenter.defaultUserNotificationCenter().deliverNotification_(notif)
            except:
                pass

    @objc.python_method
    def check_daily_notification(self):
        if not _SETTINGS.get("notify_enabled", False):
            return
        notify_time = _SETTINGS.get("notify_time", "20:00")
        try:
            hour, minute = map(int, notify_time.split(":"))
        except:
            return
        now = datetime.now()
        if now.hour != hour or now.minute != minute:
            return
        today_key = now.strftime("%Y-%m-%d")
        if self._notified_date == today_key:
            return
        self._notified_date = today_key
        data = fetch()
        if not data:
            return
        s = data["all"]
        today = s["today_tok"]
        total = s["all_tok"]
        cost  = s["cost_today"]
        model_today = s.get("top_model_today") or ""
        def f(n):
            if n >= 1_000_000: return f"{n/1_000_000:.1f}M"
            if n >= 1_000:    return f"{n/1_000:.1f}k"
            return str(n)
        cost_s = f"${cost:.2f}" if cost and cost >= 0.01 else f"${cost:.3f}" if cost and cost >= 0.001 else "$0.00"
        text = f"Today: {f(today)} · All time: {f(total)}"
        if model_today:
            text += f" · Top: {model_today}"
        if cost and cost > 0:
            text += f" · Cost: {cost_s}"
        notification = NSUserNotification.alloc().init()
        notification.setTitle_("Tokenbar — Daily Summary")
        notification.setInformativeText_(text)
        notification.setActionButtonTitle_("Flex on X")
        notification.setUserInfo_({"action": "flex"})
        NSUserNotificationCenter.defaultUserNotificationCenter().deliverNotification_(notification)

    def userNotificationCenter_didActivateNotification_(self, center, notification):
        if notification.userInfo() and notification.userInfo().get("action") == "flex":
            self.flex()

    def userNotificationCenter_shouldPresentNotification_(self, center, notification):
        return True

    @objc.python_method
    def resize_popover(self, h):
        self._pop.setContentSize_(NSSize(W, h))
        self._wv.setFrame_(NSMakeRect(0, 0, W, h))


    @objc.python_method
    def bootstrap_and_inject(self):
        """Injecte MAIN_JS dans le monde page, puis les données."""
        data = fetch()
        if data:
            self._item.button().setTitle_(_navbar_title(data['all']['today_tok']))
        def on_bootstrap(result, error):
            if error:
                print(f"[tokenbar] JS bootstrap error: {error}", flush=True)
            if not error and data:
                self._inject_js(data)
        self._wv.evaluateJavaScript_completionHandler_(MAIN_JS + "\n'bootstrapped'", on_bootstrap)

    @objc.python_method
    def inject_data(self):
        data = fetch()
        if not data:
            self._item.button().setTitle_("⚠"); return
        self._item.button().setTitle_(_navbar_title(data['all']['today_tok']))
        self._inject_js(data)

    @objc.python_method
    def _inject_js(self, data):
        if not data: return
        payload = dict(data, settings=_SETTINGS,
                       builtin_rates=[{"key": k, "rate": r} for k, r in BLENDED_RATES],
                       git_heatmap=_git_heatmap())
        js = "typeof injectData!=='undefined'&&injectData(" + json.dumps(payload) + ")"
        self._wv.evaluateJavaScript_completionHandler_(js, None)

    @objc.python_method
    def refresh_limits(self):
        _limits_cache["ts"] = 0.0
        threading.Thread(target=_refresh_limits_bg, daemon=True).start()
        def _reinject():
            time.sleep(2)
            self.inject_data()
        threading.Thread(target=_reinject, daemon=True).start()

    @objc.python_method
    def save_settings_(self, body):
        try:
            d = json.loads(body) if isinstance(body, str) else body
            if d.pop("reset_start", False):
                stamp = int(time.time())
                Path.home().joinpath(".tokenbar_start").write_text(str(stamp))
                global START_S
                START_S = float(stamp)
                _cc_cache["ts"] = 0.0
            save_settings(d)
            if "login_start" in d:
                self._ensure_login_start()
            self._start_timer()
            self.inject_data()
        except: pass

    @objc.python_method
    def flex(self):
        data = fetch()
        if not data:
            return
        s = data["all"]
        today = s["today_tok"]
        total = s["all_tok"]
        cost  = s["cost_today"]
        model_today = s.get("top_model_today") or ""
        sources = [label for key, label in (("opencode", "OpenCode"), ("claude_code", "Claude Code")) if data.get(key, {}).get("today_tok", 0) > 0]
        def fmt(n):
            if n >= 1_000_000: return f"{n/1_000_000:.1f}M"
            if n >= 1_000:    return f"{n/1_000:.1f}k"
            return str(n)
        cost_s = f"${cost:.2f}" if cost and cost >= 0.01 else f"${cost:.3f}" if cost and cost >= 0.001 else None
        date_str = datetime.now().strftime("%B %d, %Y")
        site = "https://azerdsq131.github.io/tokenbar/"
        text = f"""📊 Stats of the day — {date_str}
Today: {fmt(today)} tokens
All time: {fmt(total)} tokens""" + (f"""
🔥 Top model today: {model_today}""" if model_today else "") + (f"""
💸 Cost today: {cost_s}""" if cost_s else "") + (f"""
📱 Via: {", ".join(sources)}""" if sources else "") + f"""

👇 Get yours:
{site}"""
        url = "https://x.com/intent/tweet?text=" + urllib.parse.quote(text)
        webbrowser.open(url)

    def onWindowClose_(self, notification):
        win = notification.object()
        if win is self._settings_win:
            self._settings_win = None
            self._settings_wv  = None
        if win is self._models_win:
            self._models_win = None
            self._models_wv  = None

    @objc.python_method
    def show_models_window(self):
        models = fetch_all_models()
        html   = MODELS_HTML_TMPL.replace("MODELS_PLACEHOLDER", json.dumps(models))
        dark   = NSAppearance.appearanceNamed_("NSAppearanceNameDarkAqua")

        if self._models_win is not None:
            try:
                self._models_wv.loadHTMLString_baseURL_(html, NSURL.fileURLWithPath_(str(Path.home()) + "/"))
                self._models_win.makeKeyAndOrderFront_(None)
                NSApp.activateIgnoringOtherApps_(True)
                return
            except Exception:
                self._models_win = None

        win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(300, 200, 400, 540), 15, NSBackingStoreBuffered, False)
        win.setTitle_("Models used")
        win.setAppearance_(dark)
        win.setBackgroundColor_(
            NSColor.colorWithCalibratedRed_green_blue_alpha_(0.11, 0.11, 0.11, 1.0))

        NSNotificationCenter.defaultCenter().addObserver_selector_name_object_(
            self, "onWindowClose:", NSWindowWillCloseNotification, win)

        wv = WKWebView.alloc().initWithFrame_configuration_(
            NSMakeRect(0, 0, 400, 540), WKWebViewConfiguration.alloc().init())
        wv.setOpaque_(False)
        wv.setBackgroundColor_(NSColor.clearColor())
        wv.setAutoresizingMask_(18)
        wv.loadHTMLString_baseURL_(html, NSURL.fileURLWithPath_(str(Path.home()) + "/"))

        win.contentView().addSubview_(wv)
        win.makeKeyAndOrderFront_(None)
        NSApp.activateIgnoringOtherApps_(True)
        self._models_win = win
        self._models_wv  = wv

    def show_settings_window(self):
        try:
            self._show_settings_window_impl()
        except Exception as e:
            import traceback
            with open("/tmp/tokenbar_crash.log", "a") as f:
                f.write(f"settings crash: {e}\n")
                traceback.print_exc(file=f)

    @objc.python_method
    def _show_settings_window_impl(self):
        html     = SETTINGS_HTML_TMPL.replace("SETTINGS_PLACEHOLDER", json.dumps(_SETTINGS))
        dark     = NSAppearance.appearanceNamed_("NSAppearanceNameDarkAqua")

        if self._settings_win is not None:
            try:
                self._settings_wv.loadHTMLString_baseURL_(html, NSURL.fileURLWithPath_(str(Path.home()) + "/"))
                self._settings_win.makeKeyAndOrderFront_(None)
                NSApp.activateIgnoringOtherApps_(True)
                return
            except Exception:
                self._settings_win = None

        win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(360, 200, 400, 540), 15, NSBackingStoreBuffered, False)
        win.setTitle_("Settings")
        win.setAppearance_(dark)
        win.setBackgroundColor_(
            NSColor.colorWithCalibratedRed_green_blue_alpha_(0.11, 0.11, 0.11, 1.0))

        cfg = WKWebViewConfiguration.alloc().init()
        uc  = cfg.userContentController()
        for n in ("saveSettings",):
            uc.addScriptMessageHandler_name_(self._msg, n)

        NSNotificationCenter.defaultCenter().addObserver_selector_name_object_(
            self, "onWindowClose:", NSWindowWillCloseNotification, win)

        wv = WKWebView.alloc().initWithFrame_configuration_(
            NSMakeRect(0, 0, 400, 540), cfg)
        wv.setOpaque_(False)
        wv.setBackgroundColor_(NSColor.clearColor())
        wv.setAutoresizingMask_(18)
        wv.loadHTMLString_baseURL_(html, NSURL.fileURLWithPath_(str(Path.home()) + "/"))

        win.contentView().addSubview_(wv)
        win.makeKeyAndOrderFront_(None)
        NSApp.activateIgnoringOtherApps_(True)
        self._settings_win = win
        self._settings_wv  = wv


if __name__ == "__main__":
    app = NSApplication.sharedApplication()
    delegate = AppDelegate.alloc().init()
    app.setDelegate_(delegate)
    app.run()
