# TokenBar

**Live AI token usage and cost tracking in your macOS menu bar.**

TokenBar reads local files written by Claude Code and Codex — no login, no API keys, no internet access required.

![macOS only](https://img.shields.io/badge/macOS-only-black?logo=apple)
![Python 3](https://img.shields.io/badge/Python-3-blue)
![License MIT](https://img.shields.io/badge/license-MIT-green)

---

## What it does

- Shows total token count in your menu bar, updated every 15 seconds
- Breaks down usage by source (Claude Code, Codex, OpenCode), by model, and by day
- Calculates exact costs using per-model pricing tables (Anthropic, OpenAI, DeepSeek, Xiaomi)
- Displays 30-day token and cost charts with hover tooltips
- Lets you exclude specific models from all calculations

## Requirements

- macOS
- Python 3
- Claude Code and/or Codex installed (at least one)

## Installation

```bash
git clone https://github.com/AZERDSQ131/tokenbar
cd tokenbar
pip install pyobjc-framework-Cocoa pyobjc-framework-WebKit
```

## Usage

```bash
# foreground
python3 tokenbar.py

# background (logs → /tmp/tokenbar.log)
./start_tokenbar.sh

# restart
pkill -f tokenbar.py && python3 tokenbar.py
```

Click **◆** in the menu bar to open the popover.

On first launch, `~/.tokenbar_start` is created automatically to track only usage from that point forward.

## Data sources

| Source | File | Method |
|---|---|---|
| Claude Code | `~/.claude/projects/**/*.jsonl` | Scans JSONL logs, extracts all 4 token types per message |
| Codex | `~/.codex/state_5.sqlite` | Reads `threads` table, estimates cost from blended rates |
| OpenCode | `~/.local/share/opencode/opencode.db` | Reads `session` table, uses cost column when available |

All sources are filtered from `~/.tokenbar_start` (Unix timestamp).

## Cost calculation

Claude costs are computed exactly using `(input, output, cache_write, cache_read)` token counts and `CLAUDE_PRICING` tables per model family.

Other models use `BLENDED_RATES` ($/M blended). Fallback: $5/M.

Models in `EXCLUDED_MODELS` (`qwen122b`, `qwen3.5` by default) are filtered from all calculations.

## License

MIT
