# AGENTS.md

This file provides guidance to Codex (Codex.ai/code) when working with code in this repository.

## Layout (v1 + v2)

- `v1/` — original app, frozen. Multi-source (Claude Code, Codex, OpenCode DBs,
  Cursor, Pi) by reading local files. Runs as `Tokenbar.app` (`◆` in menu bar).
- `v2/` — rethought app, single source: **your OpenCode proxy API**
  (`http://127.0.0.1:8787`, menu bar `⬢`). See `v2/README.md`.
- `index.html` — landing page, hosted via GitHub Pages at https://azerdsq131.github.io/tokenbar/

## Main files (v1)

- `v1/tokenbar.py` — main application (macOS menu bar, ~1600 lines)
- `v1/keep_awake.sh` — standalone utility (prevents sleep via mouse movements)
- `v1/start_tokenbar.sh` — launches Tokenbar.app via LaunchServices

## Main files (v2)

- `v2/tokenbar_v2.py` — menu bar app, single source `GET /v1/usage` on the proxy
- `v2/start_tokenbar_v2.sh` — launches TokenbarV2.app via LaunchServices
- `v2/README.md` — v2 architecture & run instructions
- Proxy side (other repo): `~/projects/api-opencode/proxy/server.js`
  (usage tap + `GET /v1/usage`), data in `~/.local/share/tokenbar-v2/`
  (`usage.jsonl` = source of truth, `usage_state.json` = cache).

## Commands

```bash
# v1 — foreground / background / restart
python3 v1/tokenbar.py
./v1/start_tokenbar.sh
pkill -f "tokenbar.py" && python3 v1/tokenbar.py

# v2 — foreground / background
/opt/homebrew/bin/python3.12 v2/tokenbar_v2.py
./v2/start_tokenbar_v2.sh

# Python dependencies (PyObjC + WebKit bindings)
pip install pyobjc-framework-Cocoa pyobjc-framework-WebKit

# v1 sentinel file — auto-created on first launch, filters prior tokens
# To reset manually:
echo "$(date +%s)" > ~/.tokenbar_start

# Proxy usage API
curl -s http://127.0.0.1:8787/health
curl -s http://127.0.0.1:8787/v1/usage
```

## Architecture (v1)

`tokenbar.py` is a native macOS menu bar app built with **PyObjC** (not `rumps`). It uses `NSStatusBar` + `NSPopover` + `WKWebView` to display an HTML/CSS/Canvas interface in a popover.

### macOS 16 WebKit — critical constraint

macOS 16 WebKit blocks inline `<script>` tags when `baseURL` is `None`. All WebViews (popover, models window, settings window) must use `NSURL.fileURLWithPath_(str(Path.home()) + "/")` as base URL. The main JS is injected via `evaluateJavaScript_` after page load (not embedded in HTML), via `bootstrap_and_inject()` called from `webView_didFinishNavigation_`.

### Data sources

| Source | File | Method |
|---|---|---|
| **Codex** | `~/.Codex/projects/**/*.jsonl` | Scans JSONL, `message.usage` field (4 token types) |
| **Codex** | `~/.codex/state_5.sqlite` | `threads` table, `tokens_used` column |
| **OpenCode** | `~/.local/share/opencode/opencode.db` | `session` table, `tokens_input/output/cost` columns |

All sources are filtered from `~/.tokenbar_start` (Unix timestamp). Auto-created on first launch.

`EXCLUDED_MODELS = {"qwen122b", "qwen3.5"}` — filtered from all sources and calculations.

### Cost calculation

**`claude_cost(model, inp, out, cache_write, cache_read)`** — exact cost for Codex (4 token types) with fallback to `BLENDED_RATES` for other models (OpenAI, DeepSeek, Xiaomi). Ultimate fallback: $5/M.

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

**Tabs**: All / Codex / Codex / OpenCode

Each tab exposes: `today_tok`, `week_tok`, `all_tok`, `cost_today`, `cost_all`, `cost_exact`, `top_model`, `top_model_today`, `daily` (tokens/day), `daily_cost` (cost/day).

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

**Base URL fix**: all `loadHTMLString_baseURL_` calls use `NSURL.fileURLWithPath_(str(Path.home()) + "/")` — required for inline scripts to run on macOS 16.

### JS ↔ Python communication

Popover `WKWebView` exposes 7 message handlers: `resize`, `refresh`, `quit`, `models`, `saveSettings`, `flex`, `settings`. Settings window exposes 1: `saveSettings`. Python injects data via `evaluateJavaScript_` calling `injectData(d)` on the JS side.

JS injection flow: `webView_didFinishNavigation_` → `bootstrap_and_inject()` → evaluates `MAIN_JS` → then evaluates `injectData(payload)`.

### Refresh

- `NSTimer` every 15 seconds (`REFRESH = 15.0`) → `tick_` → `refresh_in_background()`
- `fetch()` never runs on the main thread from UI paths: `refresh_in_background()`
  runs `fetch()` on a daemon thread, then applies results on the main thread via
  `performSelectorOnMainThread` → `_applyFetched_:` (menubar title, popover inject
  if open, alerts, daily notification). Payloads travel via `self._pending_data`
  (never through ObjC args) to avoid NSDictionary bridging.
- `toggle_` (menu bar click) shows the popover instantly, paints `self._last_data`
  immediately (no fetch), then triggers `refresh_in_background()`.
- `fetch()` wrapper: 10 s TTL (`_fetch_cache`, `FETCH_TTL`). `fetch_sync()` runs the
  5 sources in parallel via `_FETCH_POOL` so slow Cursor network doesn't block others.
- Cursor API uses `pageSize=1000` (fallback 200 on error) to cut paginated requests.
- Models window: opens instantly with `_models_cache` (30 s TTL, `MODELS_TTL`),
  refreshes in background and reloads via `_applyModels_:`.
- Menu bar format: `◆ tokens / cost` (today's totals — e.g. `◆ 1.2k / $0.04`)
- 30s cache on `fetch_claude_code` (`_cc_cache`) to avoid rescanning all JSONL files

### Settings window

Separate `NSWindow` (400×540), opens on gear icon click via `settings` message handler. Uses `SETTINGS_HTML_TMPL` with `SETTINGS_PLACEHOLDER` (same pattern as models window).

Persisted to `~/.tokenbar_settings.json` via `_SETTINGS` global. Fields: `excluded_models`, `refresh_interval`, `chart_style`, `chart_period`, `accent_color`, `notify_enabled`, `notify_time`, `login_start`, `alerts`. Saved via `saveSettings` message handler (on settings window's own `userContentController`), applied immediately.

Alerts: configured per type (tokens/cost) with a value threshold and optional repeat. Always active until removed (no period selector). Checked on every tick, fires an `NSUserNotification`.

### Flex on X/Twitter

The **Flex** button in the popover footer calls `act('flex')` → `AppDelegate.flex()`, which builds a stats tweet and opens `x.com/intent/tweet` via `webbrowser.open()`.

### Daily notification

When enabled in settings, a macOS `NSUserNotification` is delivered at the configured time (default 20:00, 24h format). Checked every 15s in `tick_()` via `AppDelegate.check_daily_notification()`. Only fires once per day (`_notified_date` guard). Notification has a **Flex on X** action button that calls `AppDelegate.flex()`.

## Architecture (v2)

Single source: your OpenCode proxy API. v2 never reads OpenCode files.

### Proxy side (`~/projects/api-opencode/proxy/server.js`, other repo)

- Every upstream response is tapped write-through (streaming preserved):
  chunks forwarded immediately + buffered (cap 10 MB) for usage extraction.
- `extractModel(body)` — `"model"` from request JSON, last path segment.
- `extractUsage(text)` — last occurrence wins: `prompt/completion_tokens`
  (chat) or `input/output_tokens` (responses) + `cached_tokens` /
  `cache_read|creation_input_tokens` + `reasoning_tokens`.
- `recordUsage()` → append `~/.local/share/tokenbar-v2/usage.jsonl` (source of
  truth, replayed at boot via `loadUsageState()`) + `saveUsageState()`.
- `GET /v1/usage` (never forwarded) → `buildUsage()`: `today_tok`,
  `week_tok`, `all_tok`, `daily[]` (tokens/input/output/cache + per-model
  breakdown), `models` / `models_1d` / `models_7d` / `models_1m`.
- Errors (non-2xx, status 0 on upstream failure) are logged too (tokens 0).
- LaunchAgent `com.opencode.proxy` (fixed 2026-09-26: pointed to the deleted
  `API OpenCode/…` path while the process ran from memory — now points to
  `~/projects/api-opencode/proxy/server.js`, logs to `api-opencode/logs/`).

### App side (`v2/tokenbar_v2.py`, ~1800 lines, menu bar `⬢`)

- `fetch_sync()` → `GET /v1/usage` (5 s timeout) → costs computed locally via
  `_model_cost()` (substring match on `BLENDED_RATES` + custom rates, `free`
  in name → 0, fallback $5/M). Returns `None` when API unreachable (offline
  state — stale data never shown as fresh).
- Calendar continuity: 366 days padded with explicit zeros (same honesty rule
  as v1: charts always end on today).
- Same proven shell as v1 (popover + canvas charts + models window + Flex +
  settings + alerts + daily notification), trimmed: single `⬢ OpenCode API`
  tab, no awake mode, no excluded-models/DeepSeek-key/start-reset settings.
- Own identity: `~/.tokenbar_v2_settings.json`, `/tmp/tokenbar_v2.log`,
  login agent `com.tokenbarv2`, bundle `TokenbarV2.app` (runs the repo file
  directly — no copy step unlike v1).

### App side — source 2 : serveur OpenCode local (`CLI`, coûts exacts)

- LaunchAgent `com.opencode.server` : `opencode serve --port 4096 --hostname
  127.0.0.1` avec `WorkingDirectory=/` (projet `global` → voit les sessions
  des 21 projets ; sans ça, la vue est limitée au cwd du serveur).
- `GET /session` suffit (un seul appel) : chaque session porte déjà
  `cost` (exact, calculé par OpenCode) + `tokens{input,output,reasoning,
  cache{read,write}}` + `model.id` + `time.updated`.
- `GET /session/:id/message` donne le détail par message (même shape) —
  utilisé ponctuellement pour vérif, pas en polling (N appels).
- Attribution : une session est rangée sur son **dernier modèle** et son
  **jour de dernière activité** (sessions multi-modèles/jours approximées).
- Onglets v2 : `All` (proxy + CLI mergés, légende par source via
  `daily_by_source {proxy, cli}`) / `⬢ Proxy` / `CLI`. Fenêtre modèles :
  badges `Proxy` / `CLI`. Ligne `MAJ HH:MM · proxy/CLI hors ligne` si une
  source tombe (l'autre continue, zéros honnêtes pour la source absente).
