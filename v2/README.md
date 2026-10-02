# Tokenbar v2 — Pi + Codex CLI + Claude Code (sources locales)

Menu bar macOS qui affiche ta consommation fusionnée en direct depuis les
sessions locales : Pi (`~/.pi/agent/sessions/*/*.jsonl`, coûts exacts),
Codex CLI (`~/.codex/sessions/**/*.jsonl`, `token_usage_record` incrémental,
coûts estimés) et Claude Code (`~/.claude/projects/**/*.jsonl`, usage par
message assistant, coûts estimés).

![macOS only](https://img.shields.io/badge/macOS-only-black?logo=apple)
![Python 3](https://img.shields.io/badge/Python-3-blue)

## Interface

- **Header** `π Tokenbar` + sessions (total · aujourd'hui), heure de MAJ
- **Stats** : Today (avec temps écoulé) · 7 days · All time · Cost today
- **Par harness** : Pi (violet) · Codex (bleu) · Claude (orange) — today + coût
  du jour, barre de part relative, sous-ligne 7d / all / coût total
- **Graphiques** : tokens + coûts, filtre source All/Pi/Codex/Claude, périodes
  1d/7d/1m/All, styles bars/line/area, tooltips
- **Top models** : top 8 inline avec barres + fenêtre « All models »
  (1d/7d/1m/all, recherche, coûts exacts)
- **Quota mensuel** ($, barre de progression), **Flex** (tweet pré-rempli),
  alertes seuils, notification du soir, lancement au login

Séries calendaires continues : chaque jour sans activité vaut 0 explicite
(jamais de vieux jour présenté comme aujourd'hui).

## Données

`_pi_all_messages()` + `_codex_all_messages()` + `_claude_all_messages()` :
granularité message/jour/modèle, cache incrémental par fichier (mtime+taille).
Froid ~5 s (3092 fichiers Claude), tiède ~0 s. Fetch en tâche de fond toutes
les 15 s, jamais sur le thread UI. Tarifs estimés : `CLAUDE_PRICING` +
`BLENDED_RATES` (cf v1), calibrés sur les coûts exacts Pi
(`deepseek-v4.1-flash` $0.01/M, `muse-spark` $0.006/M, `stealth/ox-alpha` $0).
Sous-ligne du header : `Pi X · Cx Y · Cc Z` (today par source). Fenêtre Models :
badges `Pi` / `Codex` / `Claude`.

## Lancement

```bash
./start_tokenbar_v2.sh        # direct, menu bar π (pas via open : le bundle .app n'affiche pas l'icône)
/opt/homebrew/bin/python3.12 v2/tokenbar_v2.py   # premier plan (debug)
```

Prérequis : `pip install pyobjc-framework-Cocoa pyobjc-framework-WebKit`.

Réglages : `~/.tokenbar_v2_settings.json` (active le lancement au login pour la persistance). Logs : `/tmp/tokenbar_v2.log`.
Historique multi-source (proxy/CLI) : voir git (`/tmp/tokenbar_v2_multi_backup.py`
pour la version précédente).
