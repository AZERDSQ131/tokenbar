# Tokenbar v2 — branchée sur ton API OpenCode

Menu bar macOS qui affiche ta consommation IA en direct depuis **ton proxy
OpenCode** (`http://127.0.0.1:8787`), source unique. Pas de lecture de fichiers
OpenCode, pas de scan : l'app interroge `GET /v1/usage`.

![macOS only](https://img.shields.io/badge/macOS-only-black?logo=apple)
![Python 3](https://img.shields.io/badge/Python-3-blue)

## Comment ça marche

```
tes apps (OpenClaw, CLI, …) → proxy 127.0.0.1:8787 → opencode.ai (via Tor)
                                    │ tap usage (modèle + tokens)
                                    ▼
                        ~/.local/share/tokenbar-v2/usage.jsonl
                                    │ GET /v1/usage
                                    ▼
                        tokenbar_v2.py (menu bar ⬢)
```

1. **Le proxy** (`~/projects/api-opencode/proxy/server.js`) « tape » chaque
   réponse upstream en write-through (le streaming n'est pas altéré) : modèle
   (depuis le body), tokens (dernier bloc `usage` SSE ou JSON), statut, durée.
   → journal `usage.jsonl` (source de vérité, rejoué au boot) + `usage_state.json`.
2. **`GET /v1/usage`** sert les agrégats : `today_tok`, `week_tok`, `all_tok`,
   `daily[]` (avec détail par modèle), `models` / `models_1d` / `models_7d` / `models_1m`.
3. **L'app** fetch toutes les 15 s en tâche de fond (jamais sur le thread UI),
   calcule les coûts via ses tables de tarifs, remplit les jours vides à zéro
   (séries calendaires continues) et affiche : menu `⬢ tokens`, popover
   (stats, graphiques 30 j, modèles, Flex).

Si l'API est injoignable : état « hors ligne », jamais de vieilles données
présentées comme fraîches.

## Lancement

```bash
./start_tokenbar_v2.sh        # via TokenbarV2.app (menu bar ⬢)
/opt/homebrew/bin/python3.12 v2/tokenbar_v2.py   # premier plan (debug)
```

Prérequis : `pip install pyobjc-framework-Cocoa pyobjc-framework-WebKit`,
proxy démarré (`launchctl list | grep com.opencode.proxy`).

Dépendances : proxy `com.opencode.proxy` (plist réparé : pointe vers
`~/projects/api-opencode/proxy/server.js`, qui existe — l'ancien chemin
`API OpenCode/…` avait été supprimé alors que le process tournait encore).

## Réglages

Settings (engrenage) : intervalle de refresh, style/période des graphiques,
quota mensuel ($, barre de progression), notification du soir, alertes
seuils, lancement au login. Stockés dans `~/.tokenbar_v2_settings.json`.
Logs : `/tmp/tokenbar_v2.log`.
