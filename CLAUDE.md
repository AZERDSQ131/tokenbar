# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Main files

- `tokenbar.py` — main application (macOS menu bar, ~1350 lines)
- `keep_awake.sh` — standalone utility (prevents sleep via mouse movements)
- `start_tokenbar.sh` — launches tokenbar in background via `nohup`, logs to `/tmp/tokenbar.log`
- `index.html` — landing page, hosted via GitHub Pages at https://azerdsq131.github.io/tokenbar/

## Commands

```bash
# Run in foreground
python3 tokenbar.py

# Run in background (uses Tokenbar.app if available)
./start_tokenbar.sh

# Full restart
pkill -f tokenbar.py && python3 tokenbar.py

# Build .app bundle (required for notification icon)
./start_tokenbar.sh  # auto-creates on first run, or:
open Tokenbar.app

# Python dependencies (PyObjC + WebKit bindings)
pip install pyobjc-framework-Cocoa pyobjc-framework-WebKit

# Sentinel file — auto-created on first launch, filters prior tokens
# To reset manually:
echo "$(date +%s)" > ~/.tokenbar_start
```

## Architecture

`tokenbar.py` is a native macOS menu bar app built with **PyObjC** (not `rumps`). It uses `NSStatusBar` + `NSPopover` + `WKWebView` to display an HTML/CSS/Canvas interface in a popover.

### macOS 16 WebKit — critical constraint

macOS 16 WebKit blocks inline `<script>` tags when `baseURL` is `None`. All WebViews (popover and models window) must use `NSURL.fileURLWithPath_(str(Path.home()) + "/")` as base URL. The main JS is injected via `evaluateJavaScript_` after page load (not embedded in HTML), via `bootstrap_and_inject()` called from `webView_didFinishNavigation_`.

### Data sources

| Source | File | Method |
|---|---|---|
| **Claude Code** | `~/.claude/projects/**/*.jsonl` | Scans JSONL, `message.usage` field (4 token types) |
| **Codex** | `~/.codex/state_5.sqlite` | `threads` table, `tokens_used` column |
| **OpenCode** | `~/.local/share/opencode/opencode.db` | `session` table, `tokens_input/output/cost` columns |

All sources are filtered from `~/.tokenbar_start` (Unix timestamp). Auto-created on first launch.

`EXCLUDED_MODELS = {"qwen122b", "qwen3.5"}` — filtered from all sources and calculations.

### Cost calculation

**`claude_cost(model, inp, out, cache_write, cache_read)`** — exact cost for Claude (4 token types) with fallback to `BLENDED_RATES` for other models (OpenAI, DeepSeek, Xiaomi). Ultimate fallback: $5/M.

**`estimate_cost(model, tokens)`** — estimated cost when only total tokens are available (Codex). Uses `claude_cost` with 50/50 input/output split.

**Pricing tables:**
- `CLAUDE_PRICING`: `(key, $/M_in, $/M_out, $/M_cache_write, $/M_cache_read)` for opus-4, sonnet-4, haiku-4, opus, sonnet, haiku families
- `BLENDED_RATES`: `(key, $/M_blended)` for gpt-5.4-mini, gpt-5.5, o4-mini, o4, o3, gpt-4o-mini, gpt-4o, deepseek-v4-flash, mimo

OpenCode: if `cost` column is 0 despite tokens, cost is estimated from models (`cost_exact = False`).

### Per-model cost tracking

`fetch_claude_code()` tracks exact costs per model in `model_costs / model_costs_1d / model_costs_7d / model_costs_1m` dicts (populated alongside `models` dict using `claude_cost()`).

`fetch_opencode()` does the same via SQL `SUM(cost)` per model, with estimation fallback when `cost_exact = False`.

`fetch_all_models()` → `make_rows()` uses these exact costs instead of `estimate_cost()`, so the models window shows accurate figures matching the tab totals.

### Popover structure

**Tabs**: All / Claude / Codex / OpenCode

Each tab exposes: `today_tok`, `week_tok`, `all_tok`, `cost_today`, `cost_all`, `cost_exact`, `top_model`, `daily` (tokens/day), `daily_cost` (cost/day).

**Stats grid**: Today · 7d tokens · All time · Cost today (hidden if 0)

**Charts**: two stacked canvases — tokens (30d) then estimated cost.
- `drawChartWith(cvId, daily, valFn, hitsRef, showYAxis)` — generic function for both charts
- `filterByPeriod(daily)` — filters by `__chartPeriod` (`1d`/`7d`/`1m`/`all`) via `slice(-n)`
- Controls below 2nd chart: period buttons (`1d` `7d` `1m` `All`) + style button (`bars` → `line` → `area`)
- Tooltips on both canvases via `makeTip()`

**Summary**: All time tokens + cost · Top model · "All models →" link

### Models window

Separate `NSWindow` (400×540), opens on "All models →" click via `models` message handler.

- Live search by name/source
- Period tabs: All / 1m / 7d / 1d (data loaded once on click, instant switch)
- Each row: rank, name, source badge, progress bar, tokens, exact cost
- (i) button with pricing grid tooltip per provider (hover)

Data injected via `MODELS_HTML_TMPL.replace("MODELS_PLACEHOLDER", json.dumps(models))`.

**Base URL fix**: both `loadHTMLString_baseURL_` calls use `NSURL.fileURLWithPath_(str(Path.home()) + "/")` — required for inline scripts to run on macOS 16.

### JS ↔ Python communication

`WKWebView` exposes 4 message handlers: `resize`, `refresh`, `quit`, `models`. Python injects data via `evaluateJavaScript_` calling `injectData(d)` on the JS side.

JS injection flow: `webView_didFinishNavigation_` → `bootstrap_and_inject()` → evaluates `MAIN_JS` → then evaluates `injectData(payload)`.

### Refresh

- `NSTimer` every 15 seconds (`REFRESH = 15.0`)
- Menu bar updates on every tick; popover only if open
- 30s cache on `fetch_claude_code` (`_cc_cache`) to avoid rescanning all JSONL files

### Settings

Persisted to `~/.tokenbar_settings.json` via `_SETTINGS` global. Fields: `excluded_models`, `refresh_interval`, `chart_style`, `chart_period`, `accent_color`. Saved via `saveSettings` message handler, applied immediately via `applySettings()` in JS.
