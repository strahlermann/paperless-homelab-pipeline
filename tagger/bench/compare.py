#!/usr/bin/env python3
"""Score two benchmark runs (bench/run.sh output dirs) against bench/reference.json.
   bench/compare.py runs/<qwen-dir> runs/<haiku-dir> [out.json]"""
import json, os, re, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import tagger  # noqa: E402  (only for the household's child tag names)

SHARED = {tagger.CHILD_TAG, *tagger.CHILDREN, "Wohnung"}
TOPICS = {"Beruf", "Fahrzeug", "Finanzen", "Gesundheit", "Mobilfunk & Internet", "Reise", "Schule",
          "Steuer", "Versicherung", "Wohnen"}
FIELDS = ["corr", "type", "date", "betrag", "frist", "rechnr", "shared", "topic"]

def load_run(d):
    d = Path(d)
    res = {str(x["id"]): x for x in json.loads((d / "result.json").read_text())}
    calls = [json.loads(l) for l in (d / "llm.jsonl").read_text().splitlines() if l.strip()]
    # Multi-page scans the model kept as one document have no "aufteilung" entry; take them from the log.
    for m in re.finditer(r"Document (\d+): (\d+) pages, one document, not split", (d / "stderr.log").read_text()):
        if m.group(1) in res and not res[m.group(1)].get("aufteilung"):
            res[m.group(1)]["aufteilung"] = {"seiten": int(m.group(2)), "bereiche": [f"1-{m.group(2)}"],
                                             "wird_aufgeteilt": False, "status": "one document"}
    return {"docs": res, "calls": calls, "wall_s": int((d / "wall_s").read_text()),
            "cost": sum(c.get("cost") or 0 for c in calls),
            "prompt_tokens": sum(c["prompt_tokens"] for c in calls),
            "gen_tokens": sum(c["gen_tokens"] for c in calls)}

def ranges(bereiche):
    out = []
    for b in bereiche or []:
        a, _, z = str(b).partition("-")
        out.append([int(a), int(z or a)])
    return out

def values(x):
    tags = x.get("tags") or []
    return {"corr": x.get("korrespondent") or x.get("korrespondent_vorschlag"), "type": x.get("dokumenttyp"), "date": x.get("datum"),
            "betrag": x.get("betrag"), "frist": x.get("frist"), "rechnr": x.get("rechnungsnummer"),
            "shared": sorted(set(tags) & SHARED), "topic": sorted(set(tags) & TOPICS)}

def ok(field, val, ref, x):
    acc = ref.get(field)
    if acc is None:
        return None
    for a in acc:
        norm = lambda t: re.sub(r"\W", "", t.casefold())
        if field == "corr" and a and val and (norm(a) in norm(val) or norm(val) in norm(a)):
            return True
        if field == "date" and a == "keep" and val == x.get("datum_bisher"):
            return True
        if isinstance(a, list) and sorted(a) == val:
            return True
        if a == val:
            return True
    return False

def main():
    """Args: name=run_dir ... [--out results.json]; legacy form: qwen_dir haiku_dir [out]."""
    ref = json.loads(Path(os.environ.get("BENCH_REF") or Path(__file__).with_name("reference.json")).read_text())
    args, out_path = sys.argv[1:], None
    if "--out" in args:
        i = args.index("--out"); out_path = args[i + 1]; args = args[:i] + args[i + 2:]
    if all("=" in a for a in args):
        runs = {a.split("=", 1)[0]: load_run(a.split("=", 1)[1]) for a in args}
    else:
        runs = {"qwen": load_run(args[0]), "haiku": load_run(args[1])}
        out_path = out_path or (args[2] if len(args) > 2 else None)
    ids = sorted(set().union(*(r["docs"] for r in runs.values())), key=str)
    docs, score = [], {b: {f: [0, 0] for f in FIELDS + ["split"]} for b in runs}
    for i in ids:
        r = ref.get(i, {})
        entry = {"id": i, "note": r.get("note"), "ref": {k: r.get(k) for k in FIELDS + ["split"]}, "b": {}}
        for b, run in runs.items():
            x = run["docs"].get(i)
            if x is None:
                entry["b"][b] = None
                continue
            v = values(x)
            res = {}
            if not r.get("skip") and not r.get("fields_skip"):
                for f in FIELDS:
                    res[f] = ok(f, v[f], r, x)
                    if res[f] is not None:
                        score[b][f][0] += res[f]; score[b][f][1] += 1
            a = x.get("aufteilung")
            if a:
                pages = a.get("seiten")
                exp = r.get("split") or [[1, pages]]
                got = ranges(a.get("bereiche")) or [[1, pages]]
                v["split"] = {"ranges": got, "auto": a.get("wird_aufgeteilt"), "conf": a.get("sicherheit"),
                              "status": a.get("status")}
                res["split"] = got == exp or got in (r.get("split_alt") or [])
                score[b]["split"][0] += res["split"]; score[b]["split"][1] += 1
                entry["ref"]["split"] = exp
            v.update(pruefen=bool(x.get("pruefen")), gruende=x.get("gruende") or [],
                     vorschlag=x.get("korrespondent_vorschlag"), titel=x.get("titel"),
                     datum_bisher=x.get("datum_bisher"), sicherheit=x.get("sicherheit"))
            entry["b"][b] = {"v": v, "ok": res}
        docs.append(entry)
    summary = {}
    for b, run in runs.items():
        n = len(run["docs"])
        summary[b] = {"docs": n, "pruefen": sum(bool(x.get("pruefen")) for x in run["docs"].values()),
                      "wall_s": run["wall_s"], "cost_usd": round(run["cost"], 4), "calls": len(run["calls"]),
                      "prompt_tokens": run["prompt_tokens"], "gen_tokens": run["gen_tokens"],
                      "score": score[b]}
    if out_path:
        Path(out_path).write_text(json.dumps({"summary": summary, "docs": docs}, ensure_ascii=False, indent=1))
    names = list(runs)
    print(f"{'field':10} " + " ".join(f"{n[:12]:>12}" for n in names))
    for f in FIELDS + ["split"]:
        print(f"{f:10} " + " ".join(f"{score[n][f][0]:>6}/{score[n][f][1]:<5}" for n in names))
    for n in names:
        s = summary[n]
        print(n, f"Prüfen {s['pruefen']}/{s['docs']}, wall {s['wall_s']} s, cost ${s['cost_usd']}, calls {s['calls']}")

if __name__ == "__main__":
    main()
