# Offline test of the pending-split hand-off: .venv/bin/python -I bench/test_pending_splits.py "$PWD"
import datetime as dt, json, os, sys, tempfile
d = tempfile.mkdtemp()
os.environ["TAGGER_PENDING_FILE"] = os.path.join(d, "p.json")
os.environ["TAGGER_LOG_FILE"] = os.path.join(d, "log")
sys.path.insert(0, sys.argv[1])
import tagger

class FakePL:
    def __init__(self, docs): self.docs, self.deleted, self.notes, self.patched = docs, [], [], []
    def get(self, ep, params=None):
        if "id__in" in params:
            return {"results": [x for x in self.docs if str(x["id"]) == str(params["id__in"])]}
        n = params["original_filename__iexact"]
        return {"results": [x for x in self.docs if x.get("original_file_name") == n]}
    def delete(self, ep): self.deleted.append(ep)
    def post(self, ep, data): self.notes.append((ep, data))
    def patch(self, ep, data): self.patched.append((ep, data))

now = dt.datetime.now()
old = (now - dt.timedelta(hours=30)).isoformat(timespec="seconds")
new = (now - dt.timedelta(minutes=20)).isoformat(timespec="seconds")
def setup():
    json.dump({
        "10": {"parts": ["10_1-1.pdf", "10_2-3.pdf"], "lines": ["a"], "requested": new},   # complete
        "20": {"parts": ["20_1-1.pdf", "20_2-2.pdf"], "lines": ["b"], "requested": new},   # partial, young
        "30": {"parts": ["30_1-2.pdf", "30_3-3.pdf"], "lines": ["c"], "requested": old},   # partial, old
        "40": {"parts": ["40_1-1.pdf"], "lines": ["d"], "requested": new},                 # original gone
    }, open(tagger.PENDING_SPLITS_FILE, "w"))
    return FakePL([{"id": 10, "tags": [1]}, {"id": 20, "tags": [1]}, {"id": 30, "tags": [1]},
                   {"id": 101, "original_file_name": "10_1-1.pdf"}, {"id": 102, "original_file_name": "10_2-3.pdf"},
                   {"id": 201, "original_file_name": "20_1-1.pdf"}, {"id": 301, "original_file_name": "30_1-2.pdf"}])
tagger.ensure_review_tag = lambda pl, ctx, dry: 99

pl = setup(); stats = {"split": 0, "parts": 0, "review": 0, "failed": 0, "waiting": 0}
still, handled = tagger.finish_pending_splits(pl, {}, stats, dry_run=True)
assert handled == {'10','20','30','40'}
assert pl.deleted == [] and pl.notes == [] and len(json.load(open(tagger.PENDING_SPLITS_FILE))) == 4, "dry run changed something"
assert set(still) == {"20"}, still

pl = setup(); stats = {"split": 0, "parts": 0, "review": 0, "failed": 0, "waiting": 0}
still, handled = tagger.finish_pending_splits(pl, {}, stats, dry_run=False)
assert pl.deleted == ["documents/10/"], pl.deleted
assert pl.patched == [("documents/30/", {"tags": [1, 99]})], pl.patched
assert len(pl.notes) == 1 and "1 von 2 Teilen" in pl.notes[0][1]["note"] and "301" in pl.notes[0][1]["note"], pl.notes
assert set(still) == {"20"} and set(json.load(open(tagger.PENDING_SPLITS_FILE))) == {"20"}
assert stats == {"split": 1, "parts": 2, "review": 1, "failed": 0, "waiting": 1}, stats
s = tagger.summarize(0, 0, 0, [], False, 0, {"split": 0, "parts": 0, "review": 0, "failed": 0, "waiting": 1})
assert s == "Paperless-Tagger: Keine neuen Dokumente im Inbox.", s
print("ok", stats)
