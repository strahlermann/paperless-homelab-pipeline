# Tagger code

The LLM tagger described in [docs/tagger.md](../docs/tagger.md): one Python script that reads the Paperless `Inbox`, splits multi-document scans, and writes title, correspondent, type, date, amount, deadline, invoice number and tags back through the REST API. This is the code that runs in the setup, with the household's names moved into a config file.

> 🤖 **AI-generated**, like the rest of this repo. The prompts and tag definitions are German and tuned on one household's mail. Read them before you trust the output, and start with `--dry-run`.

## Files

| File | What it is |
|---|---|
| `tagger.py` | The tagger. `--help` lists all options |
| `household.example.json` | Who lives here: names (never a correspondent), children (each one is also a tag), optional partner tag. Copy to `household.json` |
| `.env.example` | Paperless URL and API token. Copy to `.env`, `chmod 600` |
| `deploy/paperless-tagger-run` | Wrapper for the timer: skips the run when the Inbox is empty, reads the OpenRouter key from the Paperless container's secret, sends an ntfy summary |
| `deploy/paperless-tagger.{service,timer}` | systemd user units, every 15 minutes |
| `bench/run.sh` | Benchmark run: dry run with JSON output on a fixed list of documents, per backend (`qwen`, `haiku`, `or:<any OpenRouter model>`) |
| `bench/compare.py` | Scores runs field by field against a reference file you write first (`reference.example.json` shows the format) |
| `bench/paperless_builtin.py`, `bench/compare_builtin.py` | Asks Paperless' own classifier and Paperless AI for the same documents and scores them next to a tagger run |
| `bench/test_pending_splits.py` | Offline test of the split hand-off (no Paperless, no LLM) |
| `bench/test_dates.py` | Replays saved model outputs of a run through the date rules, without LLM calls |

`.env`, `household.json` and `runs/` are in `.gitignore`.

## Setup

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env && chmod 600 .env                # Paperless URL + token
cp household.example.json household.json              # your names
```

Paperless needs:

- tags `Inbox` (the inbox tag), `Prüfen` (review), `Frist` (open deadline), `Kinder` plus one tag per child, and the partner tag if you use it. The tagger never creates these.
- custom fields `Betrag` (monetary), `Frist` (date), `Rechnungsnummer` (string). Missing ones are skipped.
- the topic tags from `TAG_DEFINITIONS` at the top of `tagger.py`. Only tags with a definition there are offered to the model. If a tag has word stems in `TAG_KEYWORDS`, one of them must occur in the text, or the tag is dropped. Adapt both to your own tags.

Test on a few documents first; a dry run writes nothing:

```bash
export LLM_API_KEY=...   # OpenRouter key
.venv/bin/python tagger.py --dry-run -v --backend openai --llm-url https://openrouter.ai/api/v1 \
  --model anthropic/claude-haiku-5.5 \
  --llm-extra '{"temperature": null, "provider": {"zdr": true, "data_collection": "deny"}}' --doc 123
```

For a local model: `--backend ollama --llm-url http://<host>:11434 --model qwen3:30b-a3b-instruct-2507-q4_K_M`.

## Running on a timer

```bash
ln -s "$PWD/deploy/paperless-tagger-run" ~/.local/bin/
cp deploy/paperless-tagger.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now paperless-tagger.timer
loginctl enable-linger "$USER"     # run without a login session
```

The wrapper assumes this repo in `~/homelab/tagger` (set `TAGGER_REPO` otherwise), a Paperless container named `paperless-webserver-1` with the key as compose secret `/run/secrets/openrouter_api_key`, and `NTFY_URL` in `~/.config/pi-tagger/config`. Change those lines to fit your setup. Logs and the split state go to `~/.local/state/paperless-tagger/`.

## Splits are finished by a later run

Paperless on a Raspberry Pi needed 20 to 45 minutes to consume the parts of a split. An earlier version waited a fixed 12 minutes, gave up and kept the original, so originals and their parts ended up side by side. Now a split request goes into `pending-splits.json`, the run carries on, and every later run checks it: once all parts exist, the original goes to the trash; if parts are still missing after 24 hours, the original gets `Prüfen` and a note.

## Tests and benchmarks

```bash
.venv/bin/python -I bench/test_pending_splits.py "$PWD"          # offline
BENCH_DOCS="101 102 103" bench/run.sh haiku                          # dry run, needs Paperless + key
BENCH_REF=bench/my-reference.json bench/compare.py haiku=runs/<dir> other=runs/<dir>
```

Write the reference values before the first run, or you end up grading the model against its own answers. The method and our results are in [docs/benchmark.md](../docs/benchmark.md).
