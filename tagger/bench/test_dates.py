#!/usr/bin/env python3
"""Replay plan_created() on saved model outputs (no LLM calls) and score the letter date.
   .venv/bin/python bench/test_dates.py <run_dir> [reference.json]"""
import json, re, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import tagger

run = Path(sys.argv[1])
ref = json.loads(Path(sys.argv[2] if len(sys.argv) > 2 else "bench/reference-all.json").read_text())
outputs = {}
for m in re.finditer(r"Document (\d+) model output: (\{.*\})", (run / "stderr.log").read_text()):
    outputs[m.group(1)] = json.loads(m.group(2))
env = tagger.load_env(tagger.ENV_FILE)
pl = tagger.Paperless(env["PAPERLESS_URL"], env["PAPERLESS_TOKEN"])
docs = {str(d["id"]): d for d in tagger.fetch_by_ids(pl, [int(i) for i in outputs])}
good = n = 0
for i, out in sorted(outputs.items()):
    r = ref.get(i, {})
    if "date" not in r or r.get("skip") or r.get("fields_skip"):
        continue
    doc = docs[i]
    chosen = tagger.plan_created(doc, out, tagger.clean_text(doc.get("content")), [])
    final = str(chosen or doc.get("created_date") or doc["created"][:10])
    ok = final in r["date"] or ("keep" in r["date"] and chosen is None)
    good += ok; n += 1
    if not ok:
        print(f"#{i}: got {final}, model {out.get('created')!r}, expected {r['date']}")
print(f"{good}/{n} dates right")
