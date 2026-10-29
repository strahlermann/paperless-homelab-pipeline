#!/usr/bin/env python3
"""Ask Paperless' own built-in assignment for a list of documents and write a bench run dir in the
tagger's result.json shape, so bench/compare.py can score it next to a tagger run. Read only:
the suggestion endpoints never change a document.
   bench/paperless_builtin.py ai|classifier <doc ids...>
ai:         GET /documents/<id>/ai_suggestions/  (Paperless AI: LLM + RAG over similar documents)
classifier: GET /documents/<id>/suggestions/     (classic matching: Auto classifier + match rules)
"datum_bisher" is taken from the 2026-10-19 round-2 run, so the reference's "keep" means the
date the scan had back then, the same for every contestant."""
import json, os, sys, time, urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ROUND2 = ROOT / "runs/20261019-080036-haiku/result.json"

def env():
    for line in (ROOT / ".env").read_text().splitlines():
        k, _, v = line.partition("=")
        if k.strip():
            os.environ.setdefault(k.strip(), v.strip().strip('"'))

def get(path, timeout=300):
    req = urllib.request.Request(os.environ["PAPERLESS_URL"].rstrip("/") + "/api/" + path,
                                 headers={"Authorization": "Token " + os.environ["PAPERLESS_TOKEN"],
                                          "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)

def names(kind):
    out, page = {}, 1
    while True:
        d = get(f"{kind}/?page_size=500&page={page}")
        out.update({x["id"]: x["name"] for x in d["results"]})
        if not d.get("next"):
            return out
        page += 1

def main():
    mode, ids = sys.argv[1], sys.argv[2:]
    endpoint = {"ai": "ai_suggestions", "classifier": "suggestions"}[mode]
    env()
    corr, types, tags = names("correspondents"), names("document_types"), names("tags")
    before = ({str(x["id"]): x.get("datum_bisher") for x in json.loads(ROUND2.read_text())}
              if ROUND2.exists() else {})
    out = ROOT / "runs" / f"{datetime.now():%Y%m%d-%H%M%S}-paperless-{mode}"
    out.mkdir(parents=True)
    results, log, start = [], [], time.time()
    for i in ids:
        t = time.time()
        try:
            s = get(f"documents/{i}/{endpoint}/")
        except Exception as e:
            log.append(f"Document {i}: {e}")
            continue
        sec = round(time.time() - t, 1)
        c = [corr[x] for x in s.get("correspondents") or [] if x in corr]
        ty = [types[x] for x in s.get("document_types") or [] if x in types]
        results.append({
            "id": int(i),
            "titel": s.get("title"),
            "korrespondent": c[0] if c else None,
            "korrespondent_vorschlag": None if c else ((s.get("suggested_correspondents") or [None])[0]),
            "korrespondent_alle": c + list(s.get("suggested_correspondents") or []),
            "dokumenttyp": ty[0] if ty else None,
            "dokumenttyp_alle": ty + list(s.get("suggested_document_types") or []),
            "datum": (s.get("dates") or [None])[0],
            "daten_alle": s.get("dates") or [],
            "datum_bisher": before.get(str(i)),
            "tags": [tags[x] for x in s.get("tags") or [] if x in tags],
            "tags_neu": s.get("suggested_tags") or [],
            "sekunden": sec,
        })
        log.append(f"Document {i}: {sec} s")
        print(i, sec, "s", file=sys.stderr)
    (out / "result.json").write_text(json.dumps(results, ensure_ascii=False, indent=1))
    (out / "llm.jsonl").write_text("")
    (out / "stderr.log").write_text("\n".join(log) + "\n")
    (out / "wall_s").write_text(str(round(time.time() - start)))
    print(out)

if __name__ == "__main__":
    main()
