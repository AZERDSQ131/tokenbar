#!/usr/bin/env python3
"""OpenCode Token Bar — OpenCode + Claude Code."""

import base64
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
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
from pathlib import Path

import objc
from AppKit import (
    NSApp, NSApplication, NSApplicationActivationPolicyAccessory,
    NSObject, NSPopover, NSPopoverBehaviorTransient, NSScreen,
    NSStatusBar, NSVariableStatusItemLength, NSViewController,
    NSView, NSMakeRect, NSSize, NSAppearance, NSVisualEffectView, NSColor,
    NSWindow, NSBackingStoreBuffered,
    NSUserNotificationCenter, NSUserNotification,
    NSWindowWillCloseNotification,
)
from WebKit import WKWebView, WKWebViewConfiguration, WKUserScript
from Foundation import NSTimer, NSURL, NSNotificationCenter
from Quartz import (
    CGEventCreate, CGEventGetLocation,
    CGWarpMouseCursorPosition, CGAssociateMouseAndMouseCursorPosition,
)

OC_DB       = Path.home() / ".local/share/opencode/opencode.db"
OC_DB_DEV   = Path.home() / ".local/share/opencode/opencode-dev.db"
CC_DIR      = Path.home() / ".claude/projects"
CODEX_DB    = Path.home() / ".codex/state_5.sqlite"
OC_WF_DIR   = Path.home() / ".config/opencode/workflows"
CURSOR_DB   = Path.home() / "Library/Application Support/Cursor/User/globalStorage/state.vscdb"
PI_DIR      = Path.home() / ".pi/agent/sessions"

W, H   = 360, 320
DEFAULT_REFRESH = 15.0

DEFAULT_EXCLUDED = {"qwen122b", "qwen3.5"}

SETTINGS_FILE = Path.home() / ".tokenbar_v2_settings.json"
_SETTINGS = {}

DEFAULT_REFRESH = 15.0


def load_settings():
    global _SETTINGS
    _SETTINGS = {"refresh_interval": DEFAULT_REFRESH,
                  "chart_style": "bars", "chart_period": "1m",
                  "custom_rates": {},
                  "notify_enabled": False,
                  "notify_time": "20:00",
                  "login_start": False,
                  "alerts": [],
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


def enable_login_start():
    LAUNCH_AGENT_DIR.mkdir(parents=True, exist_ok=True)
    plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.tokenbarv2</string>
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
  <string>/tmp/tokenbar_v2.log</string>
  <key>StandardErrorPath</key>
  <string>/tmp/tokenbar_v2.log</string>
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
    return "\u2b22 " + fmt(today_tok)


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


# (input $/M, output $/M, cache_write_5m $/M, cache_read $/M)
# Sources: platform.claude.com/docs/en/about-claude/pricing (vérifié 2026-08-18)
CLAUDE_PRICING = [
    ("opus-5",    5.00, 25.00, 6.25, 0.50),
    ("opus-4",    5.00, 25.00, 6.25, 0.50),
    ("sonnet-5",  2.00, 10.00, 2.50, 0.20),
    ("sonnet-4",  3.00, 15.00, 3.75, 0.30),
    ("haiku-4",   1.00,  5.00, 1.25, 0.10),
    ("opus",      5.00, 25.00, 6.25, 0.50),
    ("sonnet",    2.00, 10.00, 2.50, 0.20),
    ("haiku",     0.25,  1.25, 0.30, 0.03),
]

# $/M blended (70% input + 30% output), tarifs API officiels vérifiés le 2026-08-18
BLENDED_RATES = [
    # -- gratuit : OpenRouter free tier / OpenCode Zen free / local Ollama --
    ("big-pickle",             0.0),   # OpenCode Zen, gratuit (période de feedback)
    ("qwen3-next-80b-a3b",     0.0),   # OpenRouter, variante instruct gratuite
    ("nemotron-3-super-120b",  0.18),  # OpenRouter payant: $0.085 in / $0.40 out
    ("sakana",                 0.0),
    ("vibethinker",            0.0),   # modèle local (Ollama)
    ("qwen3:4b",               0.0),   # modèle local (Ollama)
    ("phi3",                   0.0),   # modèle local (Ollama)
    ("nemotron-nano",          0.0),   # modèle local (Ollama)
    ("owl-alpha",              0.0),   # OpenRouter, gratuit au 2026-08

    # -- OpenAI (developers.openai.com/api/docs/pricing) --
    ("gpt-5.6-luna",       0.5),    # $0.20 in / $1.20 out
    ("gpt-5.6-terra",      5.0),    # $2.00 in / $12.00 out
    ("gpt-5.6-sol",       12.5),    # $5.00 in / $30.00 out
    ("gpt-5.5",           12.5),    # $5.00 in / $30.00 out
    ("gpt-5.4-mini",     1.875),    # $0.75 in / $4.50 out
    ("gpt-5.4",           6.25),    # $2.50 in / $15.00 out
    ("gpt-5.3-codex",    5.425),    # $1.75 in / $14.00 out
    ("gpt-5.2-codex",    5.425),    # $1.75 in / $14.00 out
    ("gpt-5.1-codex-max", 3.875),   # $1.25 in / $10.00 out
    ("gpt-5.1-codex-mini",0.775),   # $0.25 in / $2.00 out
    ("codex-auto-review", 5.425),   # revue auto Codex CLI, tarif codex par défaut
    ("gpt-5.2",           5.425),   # $1.75 in / $14.00 out
    ("gpt-5.1",           3.875),   # $1.25 in / $10.00 out
    ("o4-mini",            2.0),
    ("o4",                12.0),
    ("o3",                20.0),
    ("gpt-4o-mini",        0.3),
    ("gpt-4o",             5.0),

    # -- autres fournisseurs --
    ("deepseek-v4-flash",  0.35),   # DeepSeek API, hors-pic: $0.22 in / $0.66 out
    ("deepseek-v4-pro",    1.06),   # OpenRouter (release 0813): $0.66 in / $1.98 out
    ("z-ai/glm-5.2",        2.3),   # Z.ai officiel: $1.40 in / $4.40 out
    ("glm-5.2",              2.3),
    ("kimi-k2.6",         1.865),   # Moonshot officiel: $0.95 in / $4.00 out
    ("mistral-medium-3.5",  3.3),   # Mistral officiel: $1.50 in / $7.50 out
    ("minimax-m3",          0.57),  # officiel: $0.30 in / $1.20 out
    ("mimo",               0.18),   # $0.14 in + $0.28 out (Xiaomi API, non-free)
]

def claude_cost(model: str, inp: int, out: int,
                cache_write: int = 0, cache_read: int = 0) -> float:
    m = model.lower()
    for key, ri, ro, rw, rr in CLAUDE_PRICING:
        if key in m:
            return (inp * ri + out * ro + cache_write * rw + cache_read * rr) / 1_000_000
    # variantes gratuites (OpenRouter ":free" / "-free", OpenCode Zen free tier)
    if "free" in m:
        return 0.0
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


# ── Source unique : ton API OpenCode (proxy local 127.0.0.1:8787) ─────────────
# Le proxy loggue chaque requête upstream (modèle + tokens) et expose les
# agrégats via GET /v1/usage. Aucune lecture de fichiers OpenCode ici.
# Si l'API est injoignable, fetch_sync() renvoie None (état "hors ligne",
# jamais de vieilles données présentées comme fraîches).

API_BASE = "http://127.0.0.1:8787"
API_TIMEOUT = 5.0
_api_cache = {"ts": 0.0, "data": None}
API_TTL = 10.0


def _api_get(path):
    req = urllib.request.Request(API_BASE + path, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=API_TIMEOUT) as r:
        return json.loads(r.read())


def api_online():
    try:
        return _api_get("/health").get("status") == "ok"
    except Exception:
        return False


def _model_cost(name, inp, out):
    m = name.lower()
    if "free" in m:
        return 0.0
    rates = dict(BLENDED_RATES)
    rates.update(_SETTINGS.get("custom_rates", {}))
    for key, rate in rates.items():
        if key in m:
            return (inp + out) * rate / 1_000_000
    return (inp + out) * 5.0 / 1_000_000


def _day_cost(day_entry):
    return sum(_model_cost(n, m.get("input", 0) + m.get("cache_read", 0) + m.get("cache_write", 0), m.get("output", 0))
               for n, m in (day_entry.get("models") or {}).items())


def _top(models: dict) -> str:
    return max(models, key=models.get) if models else "—"


def fetch(use_cache=True):
    now = time.time()
    if use_cache and _api_cache["data"] is not None and now - _api_cache["ts"] < API_TTL:
        return _api_cache["data"]
    data = fetch_sync()
    _api_cache["ts"] = now
    _api_cache["data"] = data
    return data


def fetch_proxy_tab():
    """Source 1 : ton API (proxy). Renvoie l'onglet ou None si injoignable."""
    try:
        raw = _api_get("/v1/usage")
    except Exception as e:
        print(f"[tokenbar-v2] API injoignable: {e}", flush=True)
        return None
    if not raw.get("ok"):
        return None

    now_dt = datetime.now()
    today_s = now_dt.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    elapsed_h = max(0.5, (time.time() - today_s) / 3600)

    # Continuité calendaire : chaque jour sans requête vaut 0 explicite.
    by_date = {e["date"]: e for e in raw.get("daily", [])}
    pad_start = now_dt.date() - timedelta(days=365)
    pad_days = (now_dt.date() - pad_start).days
    daily, daily_cost, daily_bd = [], [], {}
    for i in range(pad_days + 1):
        key = (pad_start + timedelta(days=i)).isoformat()
        e = by_date.get(key)
        tok = e.get("tokens", 0) if e else 0
        daily.append({"date": key, "tokens": tok})
        daily_cost.append({"date": key, "cost": _day_cost(e) if e else 0.0})
        if e:
            bd = {"i": 0, "o": 0, "r": 0, "cr": 0, "cw": 0}
            for m in (e.get("models") or {}).values():
                bd["i"] += m.get("input", 0); bd["o"] += m.get("output", 0)
                bd["cr"] += m.get("cache_read", 0); bd["cw"] += m.get("cache_write", 0)
            daily_bd[key] = bd
        else:
            daily_bd[key] = {"i": 0, "o": 0, "r": 0, "cr": 0, "cw": 0}

    def win_cost(models):
        return sum(_model_cost(n, m.get("input", 0) + m.get("cache_read", 0) + m.get("cache_write", 0), m.get("output", 0))
                   for n, m in models.items())

    m_all = raw.get("models", {})
    m_1d = raw.get("models_1d", {})
    cost_all = win_cost(m_all)
    cost_today = win_cost(m_1d)
    today_tok = raw.get("today_tok", 0)
    tok_per_hour = int(today_tok / elapsed_h) if today_tok > 0 else 0

    bd_today = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
    for m in m_1d.values():
        bd_today["input"] += m.get("input", 0)
        bd_today["output"] += m.get("output", 0)
        bd_today["cache_read"] += m.get("cache_read", 0)
        bd_today["cache_write"] += m.get("cache_write", 0)

    s = {
        "today_tok": today_tok,
        "week_tok": raw.get("week_tok", 0),
        "all_tok": raw.get("all_tok", 0),
        "today_req": raw.get("today_req", 0),
        "today_sess": None,
        "all_sess": None,
        "top_model": _top({n: m.get("tokens", 0) for n, m in m_all.items()}),
        "top_model_today": _top({n: m.get("tokens", 0) for n, m in m_1d.items()}),
        "daily": daily,
        "daily_cost": daily_cost,
        "cost_today": cost_today,
        "cost_all": cost_all,
        "cost_exact": False,
        "breakdown_today": bd_today,
        "daily_breakdown": daily_bd,
        "tok_per_hour": tok_per_hour,
        "api_updated_at": raw.get("updated_at"),
        "models_all": {n: m.get("tokens", 0) for n, m in m_all.items()},
        "models_1d": {n: m.get("tokens", 0) for n, m in m_1d.items()},
    }
    return s


def fetch_all_models(use_cache=True):
    global _models_cache
    now = time.time()
    if use_cache and _models_cache["data"] is not None and now - _models_cache["ts"] < MODELS_TTL:
        return _models_cache["data"]
    try:
        raw = _api_get("/v1/usage")
    except Exception:
        return _models_cache["data"]
    if not raw.get("ok"):
        return _models_cache["data"]

    def make_rows(models, source):
        rows = []
        for name, m in models.items():
            tok = m.get("tokens", 0)
            cost = _model_cost(name, m.get("input", 0) + m.get("cache_read", 0) + m.get("cache_write", 0), m.get("output", 0))
            rows.append({"name": name, "tokens": tok,
                         "cost": round(cost, 4), "source": source})
        return sorted(rows, key=lambda x: -x["tokens"])

    def cli_rows():
        rows = _cli_all_messages()
        if rows is None:
            return _cli_groups_cache["data"]
        now_d = datetime.now().date()
        cuts = {"1d": now_d.isoformat(),
                "7d": (now_d - timedelta(days=6)).isoformat(),
                "1m": (now_d - timedelta(days=29)).isoformat()}
        groups = {"1d": {}, "7d": {}, "1m": {}, "all": {}}
        for (day, name, i, o, r, cr, cw, cst) in rows:
            tok = i + o + cr + cw
            if not tok:
                continue
            wins = ["all"]
            if day >= cuts["1m"]:
                wins.append("1m")
            if day >= cuts["7d"]:
                wins.append("7d")
            if day == cuts["1d"]:
                wins.append("1d")
            for w in wins:
                e = groups[w].setdefault(name, {"tokens": 0, "cost": 0.0})
                e["tokens"] += tok
                e["cost"] += cst
        data = {w: sorted([{"name": n, "tokens": e["tokens"],
                            "cost": round(e["cost"], 4), "source": "CLI"}
                           for n, e in g.items()], key=lambda x: -x["tokens"])
                for w, g in groups.items()}
        _cli_groups_cache["data"] = data
        return data

    data = {}
    cli = cli_rows()
    for w in ("1d", "7d", "1m", "all"):
        key = {"1d": "models_1d", "7d": "models_7d",
               "1m": "models_1m", "all": "models"}[w]
        rows = make_rows(raw.get(key, {}), "Proxy") + cli[w]
        data[w] = sorted(rows, key=lambda x: -x["tokens"])
    _models_cache = {"ts": now, "data": data}
    return data


CLI_BASE = "http://127.0.0.1:4096"
CLI_TIMEOUT = 5.0


def _cli_get(path):
    req = urllib.request.Request(CLI_BASE + path, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=CLI_TIMEOUT) as r:
        return json.loads(r.read())


def _cli_day(ms):
    try:
        return datetime.fromtimestamp(ms / 1000.0).strftime("%Y-%m-%d")
    except Exception:
        return datetime.now().strftime("%Y-%m-%d")


_cli_msg_cache = {"by_session": {}}
_cli_groups_cache = {"data": {"1d": [], "7d": [], "1m": [], "all": []}}


def _cli_all_messages():
    """Précision niveau message (modèle + jour exacts), cache incrémental :
    seules les sessions modifiées depuis le dernier poll sont re-lues."""
    try:
        sessions = _cli_get("/session")
    except Exception as e:
        print(f"[tokenbar-v2] CLI injoignable: {e}", flush=True)
        return None
    live = set()
    for sn in sessions:
        sid = sn.get("id")
        if not sid:
            continue
        live.add(sid)
        upd = (sn.get("time") or {}).get("updated", 0)
        cached = _cli_msg_cache["by_session"].get(sid)
        if cached is not None and cached.get("updated") == upd:
            continue
        try:
            msgs = _cli_get(f"/session/{sid}/message?limit=500")
        except Exception as e:
            print(f"[tokenbar-v2] messages {sid[:12]}: {e}", flush=True)
            continue
        rows = []
        for m in msgs:
            info = m.get("info", {}) or {}
            if info.get("role") != "assistant":
                continue
            t = info.get("tokens") or {}
            ch = t.get("cache") or {}
            tm = (info.get("time") or {}).get("created", 0)
            rows.append(((_cli_day(tm) if tm else _cli_day(upd)),
                         info.get("modelID") or "unknown",
                         t.get("input", 0), t.get("output", 0),
                         t.get("reasoning", 0),
                         ch.get("read", 0), ch.get("write", 0),
                         info.get("cost") or 0.0))
        _cli_msg_cache["by_session"][sid] = {"updated": upd, "rows": rows}
    for sid in list(_cli_msg_cache["by_session"]):
        if sid not in live:
            del _cli_msg_cache["by_session"][sid]
    _cli_msg_cache["nsess"] = len(live)
    out = []
    for v in _cli_msg_cache["by_session"].values():
        out.extend(v["rows"])
    return out


def fetch_cli_tab():
    """Source 2 : serveur OpenCode local (coûts exacts). Onglet ou None."""
    rows = _cli_all_messages()
    if rows is None:
        return None
    now_dt = datetime.now()
    today_str = now_dt.date().isoformat()
    week_cut = (now_dt.date() - timedelta(days=6)).isoformat()
    today_s = now_dt.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    elapsed_h = max(0.5, (time.time() - today_s) / 3600)

    per_day, mall, m1d = {}, {}, {}
    cost_all = cost_today = 0.0
    today_tok = week_tok = all_tok = 0
    bd_today = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}

    for (day, name, i, o, r, cr, cw, cst) in rows:
        tok = i + o + cr + cw
        if not tok:
            continue
        all_tok += tok
        cost_all += cst
        mall[name] = mall.get(name, 0) + tok
        e = per_day.setdefault(day, {"tokens": 0, "cost": 0.0,
                                     "bd": {"i": 0, "o": 0, "r": 0, "cr": 0, "cw": 0}})
        e["tokens"] += tok
        e["cost"] += cst
        e["bd"]["i"] += i; e["bd"]["o"] += o; e["bd"]["r"] += r
        e["bd"]["cr"] += cr; e["bd"]["cw"] += cw
        if day == today_str:
            today_tok += tok
            cost_today += cst
            m1d[name] = m1d.get(name, 0) + tok
            bd_today["input"] += i; bd_today["output"] += o
            bd_today["cache_read"] += cr; bd_today["cache_write"] += cw
        if day >= week_cut:
            week_tok += tok

    pad_start = now_dt.date() - timedelta(days=365)
    pad_days = (now_dt.date() - pad_start).days
    daily, daily_cost, daily_bd = [], [], {}
    for i in range(pad_days + 1):
        key = (pad_start + timedelta(days=i)).isoformat()
        e = per_day.get(key)
        daily.append({"date": key, "tokens": e["tokens"] if e else 0})
        daily_cost.append({"date": key, "cost": e["cost"] if e else 0.0})
        daily_bd[key] = e["bd"] if e else {"i": 0, "o": 0, "r": 0, "cr": 0, "cw": 0}

    return {
        "today_tok": today_tok,
        "week_tok": week_tok,
        "all_tok": all_tok,
        "today_req": 0,
        "today_sess": None,
        "all_sess": _cli_msg_cache.get("nsess", 0),
        "top_model": _top(mall),
        "top_model_today": _top(m1d),
        "daily": daily,
        "daily_cost": daily_cost,
        "cost_today": cost_today,
        "cost_all": cost_all,
        "cost_exact": True,
        "breakdown_today": bd_today,
        "daily_breakdown": daily_bd,
        "tok_per_hour": int(today_tok / elapsed_h) if today_tok > 0 else 0,
        "models_all": dict(mall),
        "models_1d": dict(m1d),
    }


def empty_tab():
    now_dt = datetime.now()
    pad_start = now_dt.date() - timedelta(days=365)
    pad_days = (now_dt.date() - pad_start).days
    daily = [{"date": (pad_start + timedelta(days=i)).isoformat(), "tokens": 0}
             for i in range(pad_days + 1)]
    daily_cost = [{"date": d["date"], "cost": 0.0} for d in daily]
    return {
        "today_tok": 0, "week_tok": 0, "all_tok": 0, "today_req": 0,
        "today_sess": None, "all_sess": None,
        "top_model": "—", "top_model_today": "—",
        "daily": daily, "daily_cost": daily_cost,
        "cost_today": 0.0, "cost_all": 0.0, "cost_exact": True,
        "breakdown_today": {},
        "daily_breakdown": {d["date"]: {"i": 0, "o": 0, "r": 0, "cr": 0, "cw": 0} for d in daily},
        "tok_per_hour": 0, "models_all": {}, "models_1d": {},
    }


def merge_tabs(p, c):
    daily = [{"date": a["date"], "tokens": a["tokens"] + b["tokens"]}
             for a, b in zip(p["daily"], c["daily"])]
    daily_cost = [{"date": a["date"], "cost": a["cost"] + b["cost"]}
                  for a, b in zip(p["daily_cost"], c["daily_cost"])]
    keys = ("i", "o", "r", "cr", "cw")
    daily_bd = {d["date"]: {k: p["daily_breakdown"].get(d["date"], {}).get(k, 0)
                               + c["daily_breakdown"].get(d["date"], {}).get(k, 0)
                               for k in keys} for d in daily}
    by_source = {d["date"]: {"proxy": a["tokens"], "cli": b["tokens"]}
                 for d, a, b in [(e, x, y) for e, x, y in
                                  zip(daily, p["daily"], c["daily"])]}
    mall, m1d = {}, {}
    for src in (p.get("models_all", {}), c.get("models_all", {})):
        for k, v in src.items():
            mall[k] = mall.get(k, 0) + v
    for src in (p.get("models_1d", {}), c.get("models_1d", {})):
        for k, v in src.items():
            m1d[k] = m1d.get(k, 0) + v
    today_tok = p["today_tok"] + c["today_tok"]
    elapsed_h = max(0.5, (time.time() - datetime.now().replace(
        hour=0, minute=0, second=0, microsecond=0).timestamp()) / 3600)
    bd = {"input": p["breakdown_today"].get("input", 0) + c["breakdown_today"].get("input", 0),
          "output": p["breakdown_today"].get("output", 0) + c["breakdown_today"].get("output", 0),
          "cache_read": p["breakdown_today"].get("cache_read", 0) + c["breakdown_today"].get("cache_read", 0),
          "cache_write": p["breakdown_today"].get("cache_write", 0) + c["breakdown_today"].get("cache_write", 0)}
    return {
        "today_tok": today_tok,
        "week_tok": p["week_tok"] + c["week_tok"],
        "all_tok": p["all_tok"] + c["all_tok"],
        "today_req": p.get("today_req", 0),
        "today_sess": None,
        "all_sess": c.get("all_sess"),
        "top_model": _top(mall),
        "top_model_today": _top(m1d),
        "daily": daily,
        "daily_cost": daily_cost,
        "cost_today": p["cost_today"] + c["cost_today"],
        "cost_all": p["cost_all"] + c["cost_all"],
        "cost_exact": False,
        "breakdown_today": bd,
        "daily_breakdown": daily_bd,
        "daily_by_source": by_source,
        "tok_per_hour": int(today_tok / elapsed_h) if today_tok > 0 else 0,
    }


def fetch_sync():
    p = fetch_proxy_tab()
    c = fetch_cli_tab()
    if p is None and c is None:
        print("[tokenbar-v2] proxy + CLI injoignables", flush=True)
        return None
    return {"all": merge_tabs(p or empty_tab(), c or empty_tab()),
            "proxy": p or empty_tab(),
            "cli": c or empty_tab(),
            "fetched_at": time.time(),
            "proxy_online": p is not None,
            "cli_online": c is not None}


_models_cache = {"ts": 0.0, "data": None}
MODELS_TTL = 30.0


# ── HTML ──────────────────────────────────────────────────────────────────────

MAIN_HTML = """\
<!DOCTYPE html><html><head><meta charset="utf-8">
<style>
*{margin:0;padding:0;box-sizing:border-box}
html,body{width:360px;background:#1c1c1e;color:#fff;
  font-family:-apple-system,BlinkMacSystemFont,"SF Pro Text",sans-serif;
  overflow-x:hidden;overflow-y:auto;-webkit-font-smoothing:antialiased}
html::-webkit-scrollbar{width:4px}
html::-webkit-scrollbar-thumb{background:rgba(255,255,255,.15);border-radius:2px}

/* tabs */
.tabs{display:flex;padding:0 10px;border-bottom:1px solid rgba(255,255,255,.08)}
.tab{padding:10px 7px 9px;font-size:12px;font-weight:500;color:rgba(255,255,255,.38);
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

.btn-q{color:rgba(255,255,255,.3)}
</style></head><body>

<div id="page-main">
<div class="tabs">
  <div class="tab active" data-tab="all"   onclick="switchTab('all')">All</div>
  <div class="tab"        data-tab="proxy" onclick="switchTab('proxy')">⬢ Proxy</div>
  <div class="tab"        data-tab="cli"   onclick="switchTab('cli')">CLI</div>
  <button class="tab-settings" onclick="act('settings')" title="Settings">&#x2699;</button>
</div>
<div id="sync-line" style="padding:0 20px 6px;font-size:9.5px;color:rgba(255,255,255,.28);letter-spacing:.02em"></div>

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
<div id="provider-legend" style="display:none;padding:4px 20px 0;gap:12px;flex-wrap:wrap"></div>
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
let __dailyBySource = {};
const STYLES        = ['bars', 'line', 'area'];
const BD_COLORS = {
  cr: {hex:'#fbbf24', rgba:'rgba(251,191,36,'},
  i:  {hex:'#60a5fa', rgba:'rgba(96,165,250,'},
  r:  {hex:'#f472b6', rgba:'rgba(244,114,182,'},
  cw: {hex:'#a78bfa', rgba:'rgba(167,139,250,'},
  o:  {hex:'#34d399', rgba:'rgba(52,211,153,'},
};
const BD_ORDER = ['cr','i','r','cw','o'];

// Une couleur fixe par fournisseur, réutilisée partout (résumé, graphique "All", quotas)
const PROVIDER_COLORS = {
  proxy: {hex:'#8b5cf6', rgba:'rgba(139,92,246,'},
  cli:   {hex:'#34d399', rgba:'rgba(52,211,153,'},
};
const PROVIDER_ORDER  = ['proxy','cli'];
const PROVIDER_LABELS = {proxy:'Proxy API', cli:'CLI local'};

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
  __dailyBySource = (tab === 'all' && s.daily_by_source) ? s.daily_by_source : {};
  const legendEl = document.getElementById('provider-legend');
  if (tab === 'all' && Object.keys(__dailyBySource).length) {
    legendEl.style.display = 'flex';
    legendEl.innerHTML = PROVIDER_ORDER.map(function(k){
      return '<span style="display:inline-flex;align-items:center;gap:4px;font-size:9.5px;color:rgba(255,255,255,.4)">'
        + '<span style="width:6px;height:6px;border-radius:50%;background:' + PROVIDER_COLORS[k].hex + '"></span>'
        + PROVIDER_LABELS[k] + '</span>';
    }).join('');
  } else {
    legendEl.style.display = 'none';
  }
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
  if (__chartPeriod === 'all') return daily;
  const n = __chartPeriod === '1d' ? 1 : __chartPeriod === '7d' ? 7 : 30;
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
  const bySource = __tab === 'all' && Object.keys(__dailyBySource).length > 0;
  const ORDER  = bySource ? PROVIDER_ORDER  : BD_ORDER;
  const COLORS = bySource ? PROVIDER_COLORS : BD_COLORS;
  const SRC    = bySource ? __dailyBySource : __dailyBreakdown;
  daily.forEach(function(d,i){
    const total = d.tokens||1;
    const bh = Math.max(2, total/max*bMaxH);
    const x = i*(bw+gap)+gap+leftPad;
    const bd = SRC[d.date];
    if (bd && ORDER.some(function(k){return bd[k];})) {
      let yOff = 0;
      ORDER.forEach(function(key){
        const v = bd[key]||0; if (!v) return;
        const segH = Math.max(0, (v/total)*bh);
        const col = COLORS[key];
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
  const bySource = __tab === 'all' && Object.keys(__dailyBySource).length > 0;
  const bd = bySource ? __dailyBySource[hit.date] : __dailyBreakdown[hit.date];
  const header = '<div style="font-weight:600;margin-bottom:5px;font-size:12px">'+fmtDate(hit.date)+'&nbsp;&nbsp;'+fmt(hit.val)+'</div>';
  if (!bd) return header;
  const total = hit.val||1;
  const ORDER = bySource ? PROVIDER_ORDER : BD_ORDER;
  const rows = ORDER.map(function(key){
    const v = bd[key]||0; if (!v) return '';
    const pct = Math.round(v/total*100);
    const col = bySource ? PROVIDER_COLORS[key].hex : BD_COLORS[key].hex;
    const label = bySource ? PROVIDER_LABELS[key] : {cr:'Cache R',i:'Input',r:'Reasoning',cw:'Cache W',o:'Output'}[key];
    return '<div style="display:flex;justify-content:space-between;gap:10px;font-size:11px">'
      +'<span><span style="color:'+col+'">●</span>&nbsp;'+label+'</span>'
      +'<span style="color:rgba(255,255,255,.7)">'+fmt(v)+'&nbsp;<span style="opacity:.45">'+pct+'%</span></span>'
      +'</div>';
  }).join('');
  return header+rows;
}

function hasChartBreakdown() {
  return (__tab === 'all' && Object.keys(__dailyBySource).length > 0)
    || Object.keys(__dailyBreakdown).length > 0;
}

function drawChart(daily) {
  __lastDaily = daily || [];
  const filtered = filterByPeriod(__lastDaily);
  if (__chartStyle==='bars' && hasChartBreakdown()) {
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
        if(isMain && hasChartBreakdown()){
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
  var sl=document.getElementById('sync-line');
  if(sl){
    var f=d.fetched_at?new Date(d.fetched_at*1000):null;
    var txt=f?('MAJ '+String(f.getHours()).padStart(2,'0')+':'+String(f.getMinutes()).padStart(2,'0')):'';
    var off=[];
    if(d.proxy_online===false)off.push('proxy hors ligne');
    if(d.cli_online===false)off.push('CLI hors ligne');
    sl.textContent=txt+(off.length?' \u00b7 '+off.join(' \u00b7 '):'');
    sl.style.color=off.length?'#f87171':'rgba(255,255,255,.28)';
  }
  renderTab(__tab);
  renderQuota(d, __settings);
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


</div>
<script>
const SETTINGS = SETTINGS_PLACEHOLDER;
function act(n,p){try{window.webkit.messageHandlers[n].postMessage(p||null)}catch(e){}}
function fmtNum(n){if(!n)return'0';if(n>=1e9)return(n/1e9).toFixed(1)+'B';if(n>=1e6)return(n/1e6).toFixed(1)+'M';if(n>=1e3)return(n/1e3).toFixed(1)+'k';return''+n}
function setColor(hex){SETTINGS.accent_color=hex;act('saveSettings',JSON.stringify(SETTINGS))}
function collectSettings(){
  var alerts=[];document.querySelectorAll('#alert-list .tag').forEach(function(t){try{alerts.push(JSON.parse(t.getAttribute('data-val')))}catch(e){}});
  return{refresh_interval:parseInt(document.getElementById('refresh-interval').value)||15,chart_style:document.getElementById('chart-style').value,chart_period:document.getElementById('chart-period').value,accent_color:SETTINGS.accent_color||'#1c1c1e',notify_enabled:document.getElementById('notify-enabled').checked,notify_time:(document.getElementById('notify-hour').value.padStart(2,'0')+':'+document.getElementById('notify-min').value.padStart(2,'0')),login_start:document.getElementById('login-start').checked,alerts:alerts,monthly_limit_usd:parseFloat(document.getElementById('monthly-limit').value)||0}
}
function saveSettings(){var s=collectSettings();Object.assign(SETTINGS,s);act('saveSettings',JSON.stringify(s))}
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
function renderSettings(s){
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
        print(f"[tokenbar-v2] message reçu: {n}", flush=True)
        try:
            if   n == "refresh" and self._app: self._app.inject_data()
            elif n == "quit":
                NSApp.terminate_(None)
            elif n == "resize"  and self._app: self._app.resize_popover(int(msg.body()))
            elif n == "models"  and self._app: self._app.show_models_window()
            elif n == "flex"    and self._app: self._app.flex()
            elif n == "saveSettings" and self._app: self._app.save_settings_(msg.body())
            elif n == "settings"  and self._app: self._app.show_settings_window()
        except Exception:
            import traceback
            traceback.print_exc()


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
        self._last_data = None
        self._pending_data = None
        self._pending_models = None
        self._fetching = False

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
        for n in ("refresh", "quit", "resize", "models", "saveSettings", "flex", "settings"):
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

        self._start_timer()
        self.refresh_in_background()

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
            # Affichage instantané : on peint les dernières données connues
            # sans attendre le réseau, puis on rafraîchit en arrière-plan.
            app_ref = self
            def _check_and_inject(result, error):
                if result:
                    if app_ref._last_data is not None:
                        app_ref._inject_js(app_ref._last_data)
                else:
                    app_ref.bootstrap_and_inject()
            self._wv.evaluateJavaScript_completionHandler_(
                "typeof injectData !== 'undefined'", _check_and_inject)
            self.refresh_in_background()

    @objc.python_method
    def refresh_in_background(self):
        """Fetch hors du main thread, puis mise à jour UI sur le main thread."""
        if self._fetching:
            return
        self._fetching = True
        def work():
            try:
                data = fetch()
            except Exception:
                import traceback
                traceback.print_exc()
                data = None
            self._pending_data = data
            self.performSelectorOnMainThread_withObject_waitUntilDone_(
                "_applyFetched:", True, False)
        threading.Thread(target=work, daemon=True).start()

    def _applyFetched_(self, _):
        data = self._pending_data
        self._pending_data = None
        self._fetching = False
        if not data:
            self._log("fetch: aucune donnée")
            return
        first = self._last_data is None
        self._last_data = data
        try:
            title = _navbar_title(data['all']['today_tok'])
            self._item.button().setTitle_(title)
        except Exception:
            pass
        if first:
            self._log("premières données appliquées")
        if self._pop.isShown():
            self._inject_js(data)
        if not hasattr(self, '_login_start_synced'):
            self._ensure_login_start()
        self.check_daily_notification()
        self._check_alerts(data)

    def tick_(self, _):
        self.refresh_in_background()

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
    def _log(self, msg):
        try:
            with open("/tmp/tokenbar_v2.log", "a") as f:
                f.write(f"{time.strftime('%H:%M:%S')} {msg}\n")
        except Exception:
            pass

    @objc.python_method
    def resize_popover(self, h):
        max_h = 700
        try:
            screen = NSScreen.mainScreen()
            if screen:
                max_h = int(screen.visibleFrame().size.height - 60)
        except Exception:
            pass
        h = max(H, min(int(h), max_h))
        self._pop.setContentSize_(NSSize(W, h))
        self._wv.setFrame_(NSMakeRect(0, 0, W, h))


    @objc.python_method
    def bootstrap_and_inject(self):
        """Injecte MAIN_JS dans le monde page, puis les données (sans bloquer)."""
        last = self._last_data
        def on_bootstrap(result, error):
            if error:
                print(f"[tokenbar-v2] JS bootstrap error: {error}", flush=True)
            elif last is not None:
                self._inject_js(last)
        self._wv.evaluateJavaScript_completionHandler_(MAIN_JS + "\n'bootstrapped'", on_bootstrap)
        self.refresh_in_background()

    @objc.python_method
    def inject_data(self):
        if self._last_data is not None:
            self._inject_js(self._last_data)
        self.refresh_in_background()

    @objc.python_method
    def _inject_js(self, data):
        if not data: return
        payload = dict(data, settings=_SETTINGS,
                       builtin_rates=[{"key": k, "rate": r} for k, r in BLENDED_RATES])
        js = "typeof injectData!=='undefined'&&injectData(" + json.dumps(payload) + ")"
        self._wv.evaluateJavaScript_completionHandler_(js, None)

    @objc.python_method
    def save_settings_(self, body):
        try:
            d = json.loads(body) if isinstance(body, str) else body
            save_settings(d)
            if "login_start" in d:
                self._ensure_login_start()
            self._start_timer()
            self.inject_data()
        except: pass

    @objc.python_method
    def flex(self):
        data = self._last_data if self._last_data is not None else fetch()
        if not data:
            return
        s = data["all"]
        today = s["today_tok"]
        total = s["all_tok"]
        cost  = s["cost_today"]
        model_today = s.get("top_model_today") or ""
        sources = [label for key, label in (("proxy", "Proxy API"), ("cli", "CLI local")) if data.get(key, {}).get("today_tok", 0) > 0]
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
        # Ouverture instantanée avec le cache, données fraîches en arrière-plan.
        if (_models_cache.get("data") is None
                or time.time() - _models_cache.get("ts", 0.0) > MODELS_TTL):
            def work():
                try:
                    fresh = fetch_all_models()
                except Exception:
                    fresh = None
                if fresh is not None:
                    self._pending_models = fresh
                    self.performSelectorOnMainThread_withObject_waitUntilDone_(
                        "_applyModels:", True, False)
            threading.Thread(target=work, daemon=True).start()
        models = _models_cache.get("data") or {"1d": [], "7d": [], "1m": [], "all": []}
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

    def _applyModels_(self, _):
        models = self._pending_models
        self._pending_models = None
        if not models:
            return
        if self._models_win is None or self._models_wv is None:
            return
        try:
            html = MODELS_HTML_TMPL.replace("MODELS_PLACEHOLDER", json.dumps(models))
            self._models_wv.loadHTMLString_baseURL_(
                html, NSURL.fileURLWithPath_(str(Path.home()) + "/"))
        except Exception:
            pass

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
