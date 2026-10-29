#!/usr/bin/env python3
"""Score tagger and Paperless built-in runs on correspondent, type, date and tags, against
(A) bench/reference-all.json (written before any run, 2026-10-18/19) and
(B) the documents' current, reviewed values in Paperless (includes types created after A).
   bench/compare_builtin.py name=run_dir ... [--out results.json]
Only the fields Paperless can suggest are scored. "keep" in A means the date the scan had in the
2026-10-19 round-2 run, for every contestant. Multi-page splits are not scored here."""
import json, os, re, sys, urllib.request
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import tagger  # noqa: E402  (only for the household's child tag names)

ROOT = Path(__file__).resolve().parent.parent
SHARED = {tagger.CHILD_TAG, *tagger.CHILDREN}
TOPICS = {"Beruf", "Fahrzeug", "Finanzen", "Gesundheit", "Mobilfunk & Internet", "Reise", "Schule",
          "Steuer", "Versicherung", "Wohnen", "Genossenschaft"}
FIELDS = ["corr", "type", "date", "shared", "topic"]
ROUND2 = ROOT / "runs/20261019-080036-haiku/result.json"
norm = lambda t: re.sub(r"\W", "", (t or "").casefold())

def env():
    for line in (ROOT / ".env").read_text().splitlines():
        k, _, v = line.partition("=")
        if k.strip():
            os.environ.setdefault(k.strip(), v.strip().strip('"'))

def get(path):
    req = urllib.request.Request(os.environ["PAPERLESS_URL"].rstrip("/") + "/api/" + path,
                                 headers={"Authorization": "Token " + os.environ["PAPERLESS_TOKEN"]})
    return json.load(urllib.request.urlopen(req, timeout=60))

def names(kind):
    d = get(f"{kind}/?page_size=1000")
    return {x["id"]: x["name"] for x in d["results"]}

def values(x):
    tags = set(x.get("tags") or [])
    return {"corr": x.get("korrespondent") or x.get("korrespondent_vorschlag"), "type": x.get("dokumenttyp"),
            "date": x.get("datum"), "shared": sorted(tags & SHARED), "topic": sorted(tags & TOPICS)}

def match(field, val, accepted, before):
    for a in accepted:
        if field == "corr" and a and val and (norm(a) in norm(val) or norm(val) in norm(a)):
            return True
        if field == "date" and a == "keep" and val == before:
            return True
        if isinstance(a, list) and sorted(a) == val:
            return True
        if a == val:
            return True
    return False

def main():
    args, out_path = sys.argv[1:], None
    if "--out" in args:
        i = args.index("--out"); out_path = args[i + 1]; args = args[:i] + args[i + 2:]
    runs = {a.split("=", 1)[0]: {str(x["id"]): x for x in json.loads((Path(a.split("=", 1)[1]) / "result.json").read_text())}
            for a in args}
    walls = {a.split("=", 1)[0]: int((Path(a.split("=", 1)[1]) / "wall_s").read_text()) for a in args}
    ref_a = json.loads(Path(os.environ.get("BENCH_REF") or ROOT / "bench/reference-all.json").read_text())
    before = ({str(x["id"]): x.get("datum_bisher") for x in json.loads(ROUND2.read_text())}
              if ROUND2.exists() else {})
    env()
    corr, types, tags = names("correspondents"), names("document_types"), names("tags")
    ids = sorted(set.intersection(*(set(r) for r in runs.values())), key=int)
    score = {n: {ref: {f: [0, 0] for f in FIELDS} for ref in "AB"} for n in runs}
    docs = []
    for i in ids:
        d = get(f"documents/{i}/")
        t = {tags[x] for x in d["tags"] if x in tags}
        ref_b = {"corr": [corr.get(d["correspondent"])], "type": [types.get(d["document_type"])],
                 "date": [d["created"][:10]], "shared": [sorted(t & SHARED)], "topic": [sorted(t & TOPICS)]}
        ra = ref_a.get(i, {})
        use_a = not (ra.get("skip") or ra.get("fields_skip"))
        entry = {"id": i, "title": d["title"], "A": {f: ra.get(f) for f in FIELDS} if use_a else None,
                 "B": ref_b, "runs": {}}
        for n, run in runs.items():
            v = values(run[i])
            ok = {}
            for ref, acc in (("A", ra if use_a else None), ("B", ref_b)):
                if acc is None:
                    continue
                for f in FIELDS:
                    if acc.get(f) is None:
                        continue
                    hit = match(f, v[f], acc[f], before.get(i))
                    ok[f"{ref}.{f}"] = hit
                    score[n][ref][f][0] += hit; score[n][ref][f][1] += 1
            entry["runs"][n] = {"v": v, "ok": ok}
        docs.append(entry)
    print(f"{len(ids)} documents")
    for ref in "AB":
        print(f"\nReference {ref}")
        print(f"{'field':8} " + " ".join(f"{n[:14]:>14}" for n in runs))
        for f in FIELDS:
            print(f"{f:8} " + " ".join(f"{score[n][ref][f][0]:>8}/{score[n][ref][f][1]:<5}" for n in runs))
    print("\nwall s  " + " ".join(f"{walls[n]:>14}" for n in runs))
    if out_path:
        Path(out_path).write_text(json.dumps({"score": score, "wall_s": walls, "docs": docs},
                                             ensure_ascii=False, indent=1))

if __name__ == "__main__":
    main()
