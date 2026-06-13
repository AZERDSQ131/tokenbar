# Tokenbar

**Live AI token usage and cost tracking in your macOS menu bar.**

Tokenbar reads local files written by Claude Code and Codex — no login, no API keys, no internet access required.

![macOS only](https://img.shields.io/badge/macOS-only-black?logo=apple)
![Python 3](https://img.shields.io/badge/Python-3-blue)
![License MIT](https://img.shields.io/badge/license-MIT-green)

---

## What it does

- Shows today's tokens and cost in your menu bar (◆ 1.2k / $0.04), updated every 15 seconds
- Breaks down usage by source (Claude Code, Codex, OpenCode), by model, and by day
- Calculates exact costs using per-model pricing tables (Anthropic, OpenAI, DeepSeek, Xiaomi)
- Displays 30-day token and cost charts with hover tooltips
- Lets you exclude specific models from all calculations
- Share your daily stats on X/Twitter with one click via the **Flex** button

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

Click **◆ tokens / cost** in the menu bar to open the popover.

On first launch, `~/.tokenbar_start` is created automatically to track only usage from that point forward.

### Flex on X/Twitter

Click the **Flex** button in the popover footer to open a pre-filled tweet with your daily stats: today's tokens, all-time usage, top model, cost, and the sources you used. Your browser opens the tweet — just review and post.

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
## License

MIT
