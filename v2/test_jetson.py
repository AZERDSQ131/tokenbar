#!/usr/bin/env python3
"""Tests jetson_export.py + jetson_source.py. Stdlib, sans PyObjC.

Lance : python3 v2/test_jetson.py   (depuis la racine du repo)
"""

import json
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

V2 = Path(__file__).resolve().parent
sys.path.insert(0, str(V2))

from jetson_export import export, load_cursor
from jetson_source import _parse_record, jetson_all_messages, reset_cache


def fake_cost(model, i, o, cw=0, cr=0):
    return (i * 1.0 + o * 5.0 + cw * 1.25 + cr * 0.1) / 1_000_000


def src_line(rid, day="2026-10-04", model="muse-spark",
             i=1000, o=100, cr=400, r=0, extra=True, surface="test"):
    ts = int(datetime.strptime(day, "%Y-%m-%d").timestamp() * 1000)
    e = {"requestId": rid, "timestamp": ts, "model": model, "surface": surface,
         "usage": {"inputTokens": i, "outputTokens": o,
                   "cachedInputTokens": cr, "reasoningOutputTokens": r},
         "totalTokens": i + o}
    if extra:  # bruit jete a l'export
        e["attempts"] = [{"ordinal": 1}]
        e["routeDecision"] = {"version": 1}
        e["accountLogLabel"] = "secret-a-supprimer"
    return json.dumps(e)


PASS, FAIL = 0, 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok  {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {detail}")


def read_exports(out):
    recs = []
    for f in sorted(Path(out).glob("jetson_usage_*.jsonl")):
        recs += [json.loads(l) for l in f.read_text().splitlines() if l.strip()]
    return recs


print("== export ==")
with tempfile.TemporaryDirectory() as t:
    src, out, cur = Path(t) / "u.jsonl", Path(t) / "data", Path(t) / "c.json"
    src.write_text("\n".join([
        src_line("a"), src_line("b"), "PAS-DU-JSON{{{",
        json.dumps({"noid": True}), "", src_line("c"),
    ]) + "\n")
    s = export(src, out, cur)
    recs = read_exports(out)
    check("incremental: 3 exportes, 2 malformees", s["exported"] == 3 and s["malformed"] == 2, s)
    check("3 records, champs restreints",
          len(recs) == 3 and all(set(r) <= {"requestId", "timestamp", "model", "surface", "usage", "totalTokens"}
                                 for r in recs)
          and all("accountLogLabel" not in r for r in recs))
    # Idempotence : rerun sans ecriture -> 0 export, curseur stable
    s2 = export(src, out, cur)
    check("rerun idempotent", s2["exported"] == 0 and s2["dupes"] == 0, s2)
    # Crash entre append et save : on recule le curseur (simule), seen dedupe
    c = json.loads(cur.read_text())
    c["offset"] = 0
    cur.write_text(json.dumps(c))
    s3 = export(src, out, cur)
    check("relecture apres crash: 0 doublon exporte",
          s3["exported"] == 0 and s3["dupes"] == 3, s3)
    # Append reel : seule la nouveaute sort
    with open(src, "a") as f:
        f.write(src_line("d") + "\n")
    s4 = export(src, out, cur)
    check("append: +1 seul", s4["exported"] == 1 and len(read_exports(out)) == 4, s4)
    # Rotation : nouveau fichier meme path -> repart a 0, seen dedupe anciens
    src.write_text(src_line("e") + "\n" + src_line("a") + "\n")
    os.utime(src, (0, 0))
    s5 = export(src, out, cur)
    recs5 = read_exports(out)
    rids = [r["requestId"] for r in recs5]
    check("rotation: seul le nouveau rid exporte",
          s5["exported"] == 1 and rids.count("a") == 1 and "e" in rids, s5)
    # Bucket mensuel
    with tempfile.TemporaryDirectory() as t2:
        s2p, o2, c2 = Path(t2) / "u.jsonl", Path(t2) / "d", Path(t2) / "c.json"
        s2p.write_text(src_line("m1", day="2026-09-30") + "\n" + src_line("m2", day="2026-10-01") + "\n")
        export(s2p, o2, c2)
        months = sorted(f.name for f in Path(o2).glob("*.jsonl"))
        check("bucket YYYY-MM", months == ["jetson_usage_2026-09.jsonl", "jetson_usage_2026-10.jsonl"], months)
    # --reset via CLI
    import subprocess
    r = subprocess.run([sys.executable, str(V2 / "jetson_export.py"),
                        "--src", str(src), "--out", str(out), "--cursor", str(cur), "--reset"],
                       capture_output=True, text=True)
    check("CLI --reset exit 0 + re-export complet",
          r.returncode == 0 and len(read_exports(out)) == 2, r.stderr[-200:])

print("== source ==")
seen = set()
row = _parse_record(json.loads(src_line("x", i=1000, o=100, cr=400)),
                    seen, fake_cost)
check("row shape provider jetson", row[1] == "muse-spark" and row[2] == "jetson")
check("input net (1000-400)", row[3] == 600 and row[4] == 100 and row[6] == 400, row)
check("jour local", row[0] == "2026-10-04", row)
check("cout via cost_fn", abs(row[8] - (600 * 1.0 + 100 * 5.0 + 400 * 0.1) / 1e6) < 1e-12, row)
check("dedupe requestId", _parse_record(json.loads(src_line("x")), seen, fake_cost) is None)
check("malforme -> None",
      _parse_record("nope", set(), fake_cost) is None
      and _parse_record({}, set(), fake_cost) is None
      and _parse_record({"requestId": "z"}, set(), fake_cost) is None
      # usage=null (requete en erreur HTTP 400, cas reel du vrai usage.jsonl)
      and _parse_record({"requestId": "e400", "timestamp": 1791140540912,
                         "model": "m", "usage": None}, set(), fake_cost) is None
      # timestamp absent/invalide -> jour inconnu -> ignore
      and _parse_record({"requestId": "nots", "usage": {"inputTokens": 5}},
                        set(), fake_cost) is None
      # usage="..." (string) : pas un dict -> ignore, sans crasher sur .get
      and _parse_record({"requestId": "str", "timestamp": 1791140540912,
                         "model": "m", "usage": "boom"}, set(), fake_cost) is None)
# raisonnement conserve
rr = _parse_record(json.loads(src_line("y", r=55)), set(), fake_cost)
check("reasoning conserve", rr[5] == 55, rr)
# sous-providers par surface
rh = _parse_record(json.loads(src_line("h1", surface="hermes")), set(), fake_cost)
check("surface hermes -> provider jetson-hermes", rh[2] == "jetson-hermes", rh)
rc = _parse_record(json.loads(src_line("c1", surface="claude")), set(), fake_cost)
check("surface claude -> provider jetson-claude", rc[2] == "jetson-claude", rc)
rc2 = _parse_record(json.loads(src_line("c2", surface="  CLAUDE  ")), set(), fake_cost)
check("surface insensible casse/espaces", rc2[2] == "jetson-claude", rc2)
rn = _parse_record(json.loads(src_line("n1", surface=None)), set(), fake_cost)
check("surface absente -> provider jetson", rn[2] == "jetson", rn)

with tempfile.TemporaryDirectory() as t:
    d = Path(t)
    (d / "jetson_usage_2026-10.jsonl").write_text(
        src_line("j1", i=1000, o=100, cr=400) + "\n"
        + src_line("j1", i=1000, o=100, cr=400) + "\n"  # doublon intra-fichier
        + "BOOM\n")
    (d / "jetson_usage_2026-11.jsonl").write_text(
        src_line("j1", i=1000, o=100, cr=400, day="2026-11-01") + "\n"  # overlap inter-fichiers
        + src_line("j2", i=500, o=50, cr=0, day="2026-11-01") + "\n")
    reset_cache()
    rows = jetson_all_messages(data_dir=d, cost_fn=fake_cost)
    check("dedupe globale inter-fichiers: 2 rows uniques", len(rows) == 2, rows)
    check("provider + somme additive",
          all(r[2] == "jetson" for r in rows)
          and sum(r[3] + r[4] + r[6] + r[7] for r in rows) == (600 + 100 + 400) + (500 + 50), rows)
    # Fichier supprime -> evince du cache, rows recalculees (pas de fantome)
    (d / "jetson_usage_2026-11.jsonl").unlink()
    rows2 = jetson_all_messages(data_dir=d, cost_fn=fake_cost)
    check("fichier supprime: 1 row restante", len(rows2) == 1 and rows2[0][1] == "muse-spark", rows2)
    # Re-ajout -> relu, dedupe OK
    (d / "jetson_usage_2026-11.jsonl").write_text(src_line("j2", i=500, o=50, cr=0, day="2026-11-01") + "\n")
    rows3 = jetson_all_messages(data_dir=d, cost_fn=fake_cost)
    check("fichier re-ajoute: 2 rows", len(rows3) == 2, rows3)
    # Cache hit : rerun sans modif ne reparse pas (meme contenu)
    rows4 = jetson_all_messages(data_dir=d, cost_fn=fake_cost)
    check("rerun sans modif: stable", rows4 == rows3, rows4)
    # Fichier cache sous CWD contenant '#rows' : ne doit ni crasher ni polluer
    (d / "jetson_usage_#rows.jsonl").write_text("PAS-DU-JSON{{{\n")
    rows5 = jetson_all_messages(data_dir=d, cost_fn=fake_cost)
    check("fichier '#rows' malforme: ignore sans crash", len(rows5) == 2, rows5)
    (d / "jetson_usage_#rows.jsonl").unlink()
    reset_cache()

print(f"\n{len(sys.argv) and ''}RESULT: {PASS} ok, {FAIL} fail")
sys.exit(1 if FAIL else 0)
