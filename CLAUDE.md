# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Fichiers principaux

- `tokenbar.py` — application principale (barre de menus macOS, 954 lignes)
- `keep_awake.sh` — utilitaire indépendant (empêche la mise en veille via mouvements souris)
- `start_tokenbar.sh` — lance tokenbar en arrière-plan via `nohup`, log dans `/tmp/tokenbar.log`

## Commandes

```bash
# Lancer tokenbar (au premier plan)
python3 tokenbar.py

# Lancer en arrière-plan
./start_tokenbar.sh

# Relancer (cycle complet)
pkill -f tokenbar.py && python3 tokenbar.py

# Prérequis Python (PyObjC + WebKit bindings)
pip install pyobjc-framework-Cocoa pyobjc-framework-WebKit

# Fichier sentinelle requis au démarrage — filtre les tokens antérieurs
echo "$(date +%s)" > ~/.tokenbar_start
```

## Architecture

`tokenbar.py` est une barre de menus macOS native construite avec **PyObjC** (pas `rumps`). Elle s'appuie sur `NSStatusBar` + `NSPopover` + `WKWebView` pour afficher une interface HTML/CSS/Canvas en popover.

### Sources de données

| Source | Fichier | Méthode |
|---|---|---|
| **OpenCode** | `~/.local/share/opencode/opencode.db` (SQLite) | Table `session`, colonnes `tokens_input/output/cost` |
| **Claude Code** | `~/.claude/projects/**/*.jsonl` | Scan JSONL, champ `message.usage` (4 types de tokens) |
| **Codex** | `~/.codex/state_5.sqlite` | Table `threads`, colonne `tokens_used` |

Toutes les sources sont filtrées à partir de `~/.tokenbar_start` (timestamp Unix).

`EXCLUDED_MODELS = {"qwen122b", "qwen3.5"}` — filtrés de toutes les sources.

### Calcul des coûts

**`claude_cost(model, inp, out, cache_write, cache_read)`** — coût exact pour Claude (4 types de tokens) avec fallback sur `BLENDED_RATES` pour les autres modèles (OpenAI, DeepSeek, Xiaomi). Fallback ultime : $5/M.

**`estimate_cost(model, tokens)`** — coût estimé quand on n'a que le total (Codex, OpenCode sans colonne `cost`). Utilise `claude_cost` avec split 50/50 input/output.

**Tableaux de prix :**
- `CLAUDE_PRICING` : `(key, $/M_in, $/M_out, $/M_cache_write, $/M_cache_read)` pour les familles opus-4, sonnet-4, haiku-4, opus, sonnet, haiku
- `BLENDED_RATES` : `(key, $/M_blended)` pour gpt-5.4-mini, gpt-5.5, o4-mini, o4, o3, gpt-4o-mini, gpt-4o, deepseek-v4-flash, mimo

OpenCode : si la colonne `cost` de la DB est 0 malgré des tokens, le coût est estimé depuis les modèles (`cost_exact = False`).

### Structure du popover

**Onglets** : All / Claude / Codex / OpenCode

Chaque onglet expose : `today_tok`, `week_tok`, `all_tok`, `cost_today`, `cost_all`, `cost_exact`, `top_model`, `daily` (tokens/jour), `daily_cost` (coût/jour).

**Grille stats** : Today · 7d tokens · All time · Coût du jour (masqué si 0)

**Graphiques** : deux canvas empilés — tokens (30j) puis coût estimé.
- `drawChartWith(cvId, daily, valFn, hitsRef)` — fonction générique pour les deux graphiques
- `filterByPeriod(daily)` — filtre selon `__chartPeriod` (`1d`/`7d`/`1m`/`all`) via `slice(-n)`
- Contrôles en dessous du 2e graphique : boutons période (`1d` `7d` `1m` `All`) + bouton style (`bars` → `line` → `area`)
- Tooltips sur les deux canvas via `makeTip()`

**Résumé** : All time tokens + coût · Top model · lien "Tous les modèles →"

### Fenêtre modèles

`NSWindow` séparé (400×540), s'ouvre sur clic "Tous les modèles →" via le message handler `models`.

- Recherche live par nom/source
- Onglets période : All / 1m / 7d / 1d (données chargées une fois au clic, switch instantané)
- Chaque ligne : rang, nom, badge source, barre de progression, tokens, coût estimé
- Bouton (i) avec tooltip de la grille tarifaire par fournisseur (hover)

Données injectées via `MODELS_HTML_TMPL.replace("MODELS_PLACEHOLDER", json.dumps(models))`.

### Communication JS ↔ Python

`WKWebView` expose 4 message handlers : `resize`, `refresh`, `quit`, `models`. Python injecte les données via `evaluateJavaScript_` en appelant `injectData(d)` côté JS.

### Rafraîchissement

- `NSTimer` toutes les 15 secondes (`REFRESH = 15.0`)
- La barre de menus se met à jour à chaque tick ; le popover seulement s'il est ouvert
- Cache 30 s sur `fetch_claude_code` (`_cc_cache`) pour éviter de rescanner tous les JSONL
