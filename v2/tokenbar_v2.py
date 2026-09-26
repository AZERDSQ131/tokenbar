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


SETTINGS_FILE = Path.home() / ".tokenbar_v2_settings.json"
_SETTINGS = {}

DEFAULT_REFRESH = 15.0


def load_settings():
    global _SETTINGS
    _SETTINGS = {"refresh_interval": DEFAULT_REFRESH,
                  "chart_style": "bars", "chart_period": "1m",
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
    return "\u03c0 " + fmt(today_tok)

# ── Données : pi local uniquement ─────────────────────────────────────────────
# Sessions ~/.pi/agent/sessions/*/*.jsonl — coûts exacts calculés par pi.
# Granularité message (modèle + jour + provider exacts), cache incrémental.

_pi_cache = {"files": {}, "rows": []}
_pi_fetch = {"ts": 0.0, "data": None}
PI_TTL = 10.0
_models_cache = {"ts": 0.0, "data": None}
MODELS_TTL = 30.0


def _pi_day(ts):
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone().strftime("%Y-%m-%d")
    except Exception:
        return datetime.now().strftime("%Y-%m-%d")


def _pi_all_messages():
    """Liste (day, model, provider, i, o, r, cr, cw, cost), cache incrémental."""
    if not PI_DIR.exists():
        return []
    try:
        files = sorted(PI_DIR.glob("*/*.jsonl"))
    except Exception as e:
        print(f"[tokenbar-v2] pi scan: {e}", flush=True)
        return list(_pi_cache["rows"])
    live = set()
    changed = False
    for jf in files:
        p = str(jf)
        live.add(p)
        try:
            st = jf.stat()
            sig = (st.st_mtime, st.st_size)
        except Exception:
            continue
        if _pi_cache["files"].get(p) == sig:
            continue
        changed = True
        _pi_cache["files"][p] = sig
        rows = []
        try:
            with open(jf, encoding="utf-8", errors="ignore") as f:
                for line in f:
                    try:
                        entry = json.loads(line)
                    except Exception:
                        continue
                    if entry.get("type") != "message":
                        continue
                    msg = entry.get("message") or {}
                    if msg.get("role") != "assistant":
                        continue
                    usage = msg.get("usage")
                    if not usage:
                        continue
                    ts = entry.get("timestamp") or msg.get("timestamp")
                    cst = ((usage.get("cost") or {}).get("total")) or 0.0
                    rows.append((_pi_day(ts) if ts else datetime.now().strftime("%Y-%m-%d"),
                                 msg.get("model") or "pi",
                                 msg.get("provider") or "unknown",
                                 usage.get("input", 0), usage.get("output", 0),
                                 usage.get("reasoning", 0),
                                 usage.get("cacheRead", 0), usage.get("cacheWrite", 0),
                                 cst))
        except Exception:
            continue
        _pi_cache["files"][p + "#rows"] = rows
    for p in list(_pi_cache["files"]):
        if p.endswith("#rows"):
            continue
        if p not in live:
            _pi_cache["files"].pop(p, None)
            _pi_cache["files"].pop(p + "#rows", None)
            changed = True
    if changed:
        out = []
        for p, v in _pi_cache["files"].items():
            if p.endswith("#rows"):
                out.extend(v)
        _pi_cache["rows"] = out
    return list(_pi_cache["rows"])


def _top(models: dict) -> str:
    if not models:
        return "—"
    best = max(models, key=models.get)
    return best if models[best] > 0 else "—"


def fetch(use_cache=True):
    now = time.time()
    if use_cache and _pi_fetch["data"] is not None and now - _pi_fetch["ts"] < PI_TTL:
        return _pi_fetch["data"]
    data = fetch_sync()
    _pi_fetch["ts"] = now
    _pi_fetch["data"] = data
    return data


def fetch_sync():
    rows = _pi_all_messages()
    now_dt = datetime.now()
    today_str = now_dt.date().isoformat()
    week_cut = (now_dt.date() - timedelta(days=6)).isoformat()
    today_s = now_dt.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    elapsed_h = max(0.5, (time.time() - today_s) / 3600)

    per_day, mall, m1d, mcost = {}, {}, {}, {}
    pall, p1d, pcost = {}, {}, {}
    cost_all = cost_today = 0.0
    today_tok = week_tok = all_tok = 0
    bd_today = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "reasoning": 0}

    for (day, name, prov, i, o, r, cr, cw, cst) in rows:
        tok = i + o + cr + cw
        if not tok:
            continue
        all_tok += tok
        cost_all += cst
        mall[name] = mall.get(name, 0) + tok
        mcost[name] = mcost.get(name, 0.0) + cst
        pe = pall.setdefault(prov, {"tokens": 0, "cost": 0.0, "today": 0, "today_cost": 0.0})
        pe["tokens"] += tok
        pe["cost"] += cst
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
            bd_today["reasoning"] += r
            pe["today"] += tok
            pe["today_cost"] += cst
            p1 = p1d.setdefault(prov, {"tokens": 0, "cost": 0.0})
            p1["tokens"] += tok
            p1["cost"] += cst
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

    top_models = sorted([{"name": n, "tokens": t, "cost": round(mcost.get(n, 0.0), 4)}
                         for n, t in mall.items()], key=lambda x: -x["tokens"])[:8]

    nfiles = sum(1 for p in _pi_cache["files"] if not p.endswith("#rows"))
    tfiles = 0
    for p, v in _pi_cache["files"].items():
        if p.endswith("#rows") and any(r[0] == today_str for r in v):
            tfiles += 1

    return {
        "today_tok": today_tok,
        "week_tok": week_tok,
        "all_tok": all_tok,
        "cost_today": cost_today,
        "cost_all": cost_all,
        "sessions_today": tfiles,
        "sessions_all": nfiles,
        "daily": daily,
        "daily_cost": daily_cost,
        "breakdown_today": bd_today,
        "daily_breakdown": daily_bd,
        "providers": pall,
        "providers_today": p1d,
        "top_models": top_models,
        "tok_per_hour": int(today_tok / elapsed_h) if today_tok > 0 else 0,
        "fetched_at": time.time(),
    }


def fetch_all_models(use_cache=True):
    global _models_cache
    now = time.time()
    if use_cache and _models_cache["data"] is not None and now - _models_cache["ts"] < MODELS_TTL:
        return _models_cache["data"]
    rows = _pi_all_messages()
    now_d = datetime.now().date()
    cuts = {"1d": now_d.isoformat(),
            "7d": (now_d - timedelta(days=6)).isoformat(),
            "1m": (now_d - timedelta(days=29)).isoformat()}
    groups = {"1d": {}, "7d": {}, "1m": {}, "all": {}}
    for (day, name, prov, i, o, r, cr, cw, cst) in rows:
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
                        "cost": round(e["cost"], 4), "source": "Pi"}
                       for n, e in g.items()], key=lambda x: -x["tokens"])
            for w, g in groups.items()}
    _models_cache = {"ts": now, "data": data}
    return data


MAIN_HTML = """\
<!DOCTYPE html><html><head><meta charset="utf-8">
<style>
*{margin:0;padding:0;box-sizing:border-box}
html,body{width:360px;background:#1c1c1e;color:#fff;
  font-family:-apple-system,BlinkMacSystemFont,"SF Pro Text",sans-serif;
  overflow-x:hidden;overflow-y:auto;-webkit-font-smoothing:antialiased}
html::-webkit-scrollbar{width:4px}
html::-webkit-scrollbar-thumb{background:rgba(255,255,255,.15);border-radius:2px}
.head{display:flex;align-items:center;padding:12px 16px 2px}
.title{font-size:15px;font-weight:700;letter-spacing:-.2px}
.sub{font-size:10.5px;color:rgba(255,255,255,.4);margin-top:1px}
.gear{margin-left:auto;background:none;border:none;color:rgba(255,255,255,.3);
  font-size:17px;cursor:pointer;padding:6px}
.gear:hover{color:rgba(255,255,255,.7)}
.sync{padding:0 16px 6px;font-size:9.5px;color:rgba(255,255,255,.28);letter-spacing:.02em}
.stats{display:grid;grid-template-columns:1fr 1fr;padding:6px 16px 4px;row-gap:12px}
.lbl{font-size:11.5px;font-weight:500;color:rgba(255,255,255,.55);margin-bottom:2px}
.val{font-size:24px;font-weight:700;letter-spacing:-.7px;line-height:1}
.val-sm{font-size:20px}
.sec{padding:10px 16px 6px;font-size:10px;font-weight:600;color:rgba(255,255,255,.35);
  text-transform:uppercase;letter-spacing:.07em}
.sec .lnk{float:right;font-weight:400;text-transform:none;letter-spacing:0;
  color:rgba(255,255,255,.3);cursor:pointer;text-decoration:underline;
  text-decoration-color:rgba(255,255,255,.15);text-underline-offset:2px;font-size:11px}
.sec .lnk:hover{color:rgba(255,255,255,.6)}
.prow{display:flex;align-items:center;gap:8px;padding:6px 16px}
.prow .dot{width:7px;height:7px;border-radius:50%;flex-shrink:0}
.prow .pname{font-size:12.5px;font-weight:500}
.prow .pvals{margin-left:auto;text-align:right;font-size:12px}
.prow .pvals .c{color:rgba(255,255,255,.45);font-size:11px}
.prow .psub{font-size:10px;color:rgba(255,255,255,.3)}
.trow{padding:5px 16px}
.trow .tline{display:flex;align-items:baseline;gap:8px;font-size:12px}
.trow .trk{color:rgba(255,255,255,.25);width:12px;font-size:11px}
.trow .tname{font-weight:500;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.trow .tvals{margin-left:auto;color:rgba(255,255,255,.55);white-space:nowrap;font-size:11.5px}
.tbar{height:3px;border-radius:2px;background:rgba(255,255,255,.08);margin-top:4px}
.tbar div{height:100%;border-radius:2px;background:rgba(255,255,255,.55)}
.chart-wrap{padding:2px 16px 0;position:relative}
canvas{display:block;width:100%}
.chart-controls{display:flex;align-items:center;padding:2px 16px 2px}
.chart-periods{display:flex;gap:1px;flex:1}
.cp{background:none;border:none;color:rgba(255,255,255,.22);font-family:inherit;
  font-size:10px;padding:2px 7px;border-radius:4px;cursor:pointer}
.cp:hover{color:rgba(255,255,255,.55)}
.cp.active{color:rgba(255,255,255,.72);background:rgba(255,255,255,.08)}
.chart-style-btn{background:none;border:none;color:rgba(255,255,255,.22);
  font-family:inherit;font-size:10px;padding:2px 8px;cursor:pointer}
.chart-style-btn:hover{color:rgba(255,255,255,.55)}
#tip,#tip2{position:fixed;background:rgba(22,22,24,.97);border:1px solid rgba(255,255,255,.13);
  border-radius:6px;padding:5px 9px;font-size:11px;color:rgba(255,255,255,.88);
  pointer-events:none;display:none;white-space:nowrap;z-index:100}
.quota-row{padding:7px 16px 9px;border-top:1px solid rgba(255,255,255,.06)}
.quota-header{display:flex;justify-content:space-between;font-size:11px;
  color:rgba(255,255,255,.5);margin-bottom:5px}
.quota-track{height:5px;border-radius:3px;background:rgba(255,255,255,.08)}
.quota-fill{height:100%;border-radius:3px;background:#4ade80;transition:width .3s}
.quota-footer{display:flex;justify-content:space-between;font-size:10px;
  color:rgba(255,255,255,.35);margin-top:4px}
.footer{display:flex;gap:2px;padding:8px 12px 12px;border-top:1px solid rgba(255,255,255,.06);margin-top:6px}
.btn{flex:1;background:none;border:none;color:rgba(255,255,255,.4);font-family:inherit;
  font-size:11.5px;padding:7px 0;border-radius:6px;cursor:pointer}
.btn:hover{color:#fff;background:rgba(255,255,255,.07)}
</style></head><body>
<div class="head">
  <div><div class="title">\u03c0 Pi</div><div class="sub" id="sess-line">harness local</div></div>
  <button class="gear" onclick="act('settings')" title="Settings">\u2699</button>
</div>
<div id="sync-line" class="sync"></div>
<div class="stats">
  <div><div class="lbl" id="lbl-today">Today</div><div class="val" id="v-today">\u2014</div></div>
  <div><div class="lbl">7 days</div><div class="val" id="v-week">\u2014</div></div>
  <div><div class="lbl">All time</div><div class="val val-sm" id="v-all">\u2014</div></div>
  <div><div class="lbl">Cost today</div><div class="val val-sm" id="v-cost">\u2014</div></div>
</div>
<div class="sec">Providers</div>
<div id="prov-list"></div>
<div class="sec">Tokens</div>
<div class="chart-wrap"><canvas id="cv"></canvas></div>
<div class="chart-controls"><div class="chart-periods">
  <button class="cp" data-p="1d" onclick="setChartPeriod('1d')">1d</button>
  <button class="cp" data-p="7d" onclick="setChartPeriod('7d')">7d</button>
  <button class="cp" data-p="1m" onclick="setChartPeriod('1m')">1m</button>
  <button class="cp" data-p="all" onclick="setChartPeriod('all')">All</button>
</div><button class="chart-style-btn" id="style-btn" onclick="cycleStyle()">bars</button></div>
<div class="sec">Cost</div>
<div class="chart-wrap"><canvas id="cv2"></canvas></div>
<div class="sec">Top models <span class="lnk" onclick="act('models')">All models \u2192</span></div>
<div id="top-list" style="padding-bottom:4px"></div>
<div id="quota-row" class="quota-row" style="display:none">
  <div class="quota-header"><span>Quota mensuel</span><span id="q-pct">\u2014</span></div>
  <div class="quota-track"><div class="quota-fill" id="q-bar"></div></div>
  <div class="quota-footer">
    <span class="quota-spent"><span id="q-spent">\u2014</span> / <span id="q-limit">\u2014</span></span>
    <span class="quota-proj">proj. <span id="q-proj">\u2014</span></span>
  </div>
</div>
<div class="footer">
  <button class="btn" onclick="act('refresh')">\u21BA Refresh</button>
  <button class="btn" onclick="act('models')">Models</button>
  <button class="btn" onclick="act('flex')">Flex</button>
  <button class="btn" onclick="act('quit')">Quit</button>
</div>
<div id="tip"></div><div id="tip2"></div>
</body></html>

"""


MAIN_JS = """\
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
function renderQuota(d, settings) {
  const limit = parseFloat(settings.monthly_limit_usd || 0);
  const row = document.getElementById('quota-row');
  if (!limit || limit <= 0) { row.style.display = 'none'; return; }
  const now = new Date();
  const monthPfx = now.getFullYear() + '-' + String(now.getMonth()+1).padStart(2,'0');
  const allDaily = (d.daily_cost || []) || [];
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
var __data=null,__settings={};
var __manualAt=0;
var __chartPeriod='1m',__chartStyle='bars',__lastDaily=[],__lastDailyCost=[];
var __chartHits=[],__chartHits2=[];
var STYLES=['bars','line','area'];
var PROV_COLORS={'opencode-go':'#8b5cf6','mistral':'#fb923c','groq':'#f87171',
  'openrouter':'#3b82f6','deepseek':'#22d3ee','nim':'#4ade80','opencode':'#a78bfa',
  'nvidia':'#34d399'};
function provColor(n){
  if(PROV_COLORS[n])return PROV_COLORS[n];
  var h=0;for(var i=0;i<n.length;i++)h=(h*31+n.charCodeAt(i))>>>0;
  return 'hsl('+(h%360)+',60%,60%)';
}
function shortName(n){
  var s=String(n).replace(/^[^\/]+\//,'');
  return s.length>26?s.slice(0,25)+'\u2026':s;
}
function filterByPeriod(daily){
  if(!daily||!daily.length)return daily;
  if(__chartPeriod==='all')return daily;
  var n=__chartPeriod==='1d'?1:__chartPeriod==='7d'?7:30;
  return daily.slice(-n);
}
function setChartPeriod(p){
  __chartPeriod=p;__manualAt=Date.now();
  document.querySelectorAll('.cp').forEach(function(b){b.classList.toggle('active',b.dataset.p===p)});
  drawCharts();
}
function cycleStyle(){
  __chartStyle=STYLES[(STYLES.indexOf(__chartStyle)+1)%STYLES.length];__manualAt=Date.now();
  document.getElementById('style-btn').textContent=__chartStyle;
  drawCharts();
}
function fmtCostFull(c){
  if(c==null||isNaN(c))return'—';
  if(c<0.001)return'$'+c.toFixed(4);
  if(c<0.01)return'$'+c.toFixed(3);
  return'$'+c.toFixed(2);
}
function drawCharts(){
  __lastDaily=__data?(__data.daily||[]):[];
  __lastDailyCost=__data?(__data.daily_cost||[]):[];
  drawChartWith('cv',filterByPeriod(__lastDaily),function(d){return d.tokens},__chartHits,true);
  drawChartWith('cv2',filterByPeriod(__lastDailyCost),function(d){return d.cost},__chartHits2,true);
}
function render(d){
  __data=d;
  $('v-today').textContent=fmt(d.today_tok);
  var elH=(Date.now()-new Date().setHours(0,0,0,0))/3600000;
  $('lbl-today').textContent=elH<23.5?'Today \u00b7 '+(elH<1?Math.round(elH*60)+'m':(elH<10?elH.toFixed(1)+'h':Math.round(elH)+'h')):'Today';
  $('v-week').textContent=fmt(d.week_tok);
  $('v-all').textContent=fmt(d.all_tok);
  $('v-cost').textContent=fmtCostFull(d.cost_today);
  $('sess-line').textContent='harness local \u00b7 '+d.sessions_all+' sessions'+(d.sessions_today?' \u00b7 '+d.sessions_today+' today':'');
  var f=d.fetched_at?new Date(d.fetched_at*1000):null;
  $('sync-line').textContent=f?('MAJ '+String(f.getHours()).padStart(2,'0')+':'+String(f.getMinutes()).padStart(2,'0')):'';
  var provs=Object.keys(d.providers||{}).map(function(n){
    var v=d.providers[n];return {n:n,t:v.tokens,c:v.cost,tt:v.today||0,tc:v.today_cost||0};
  }).sort(function(a,b){return b.t-a.t});
  $('prov-list').innerHTML=provs.map(function(p){
    return '<div class="prow"><span class="dot" style="background:'+provColor(p.n)+'"></span>'
      +'<span class="pname">'+shortName(p.n)+'</span>'
      +'<span class="pvals">'+fmt(p.t)+' <span class="c">'+fmtCostFull(p.c)+'</span>'
      +(p.tt?'<div class="psub">today '+fmt(p.tt)+'</div>':'')+'</span></div>';
  }).join('')||'<div class="prow"><span class="psub">aucune donn\u00e9e</span></div>';
  var mx=Math.max.apply(null,[1].concat((d.top_models||[]).map(function(m){return m.tokens})));
  $('top-list').innerHTML=(d.top_models||[]).map(function(m,i){
    var pct=Math.max(2,Math.round(m.tokens/mx*100));
    return '<div class="trow"><div class="tline"><span class="trk">'+(i+1)+'</span>'
      +'<span class="tname">'+shortName(m.name)+'</span>'
      +'<span class="tvals">'+fmt(m.tokens)+' \u00b7 '+fmtCostFull(m.cost)+'</span></div>'
      +'<div class="tbar"><div style="width:'+pct+'%"></div></div></div>';
  }).join('');
  document.querySelectorAll('.cp').forEach(function(b){b.classList.toggle('active',b.dataset.p===__chartPeriod)});
  document.getElementById('style-btn').textContent=__chartStyle;
  drawCharts();
  renderQuota(d,__settings);
  requestAnimationFrame(function(){
    try{window.webkit.messageHandlers.resize.postMessage(document.body.scrollHeight)}catch(e){}
  });
}
function injectData(d){
  if(d.settings){
    __settings=d.settings;
    if(Date.now()-__manualAt>60000){
      if(d.settings.chart_period)__chartPeriod=d.settings.chart_period;
      if(d.settings.chart_style)__chartStyle=d.settings.chart_style;
    }
  }
  render(d);
}
function act(n,p){try{window.webkit.messageHandlers[n].postMessage(p||null)}catch(e){}}
function $(id){return document.getElementById(id)}
(function(){
  function makeTip(cvId,tipId,hitsRef,fmtFn){
    var cv=$(cvId);
    cv.addEventListener('mousemove',function(e){
      if(!hitsRef.length)return;
      var mx=e.offsetX,hit=null;
      for(var i=0;i<hitsRef.length;i++){var h=hitsRef[i];if(mx>=h.x0&&mx<=h.x1){hit=h;break}}
      var tip=$(tipId);
      if(hit){
        tip.textContent=fmtDate(hit.date)+'  '+fmtFn(hit.val);
        tip.style.display='block';
        var th=tip.offsetHeight||22,tipW=tip.offsetWidth||160,winW=360;
        tip.style.left=Math.max(4,Math.min(e.clientX-tipW/2,winW-tipW-4))+'px';
        tip.style.top=Math.max(4,e.clientY-th-10)+'px';
      }else{tip.style.display='none'}
    });
    cv.addEventListener('mouseleave',function(){$(tipId).style.display='none'});
  }
  function fmtC(c){if(!c||c<0.001)return'$0.000';if(c<0.01)return'$'+c.toFixed(3);return'$'+c.toFixed(2);}
  makeTip('cv','tip',__chartHits,fmt);
  makeTip('cv2','tip2',__chartHits2,fmtC);
})();

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
            title = _navbar_title(data['today_tok'])
            btn = self._item.button()
            btn.setTitle_(title)
            try:
                rb = btn.title()
                vis = self._item.isVisible() if hasattr(self._item, 'isVisible') else '?'
            except Exception as e2:
                rb, vis = 'ERR', repr(e2)[:80]
            self._log(f"menu: {title} | readback={rb} | visible={vis}")
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
        s = data
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
        s = data
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
        payload = dict(data, settings=_SETTINGS)
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
        s = data
        today = s["today_tok"]
        total = s["all_tok"]
        cost  = s["cost_today"]
        model_today = (s.get("top_models") or [{}])[0].get("name")
        provs = sorted(s.get("providers", {}).items(), key=lambda kv: -kv[1]["tokens"])
        sources = [k for k, v in provs if v.get("today", 0) > 0]
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
