# The LLM tagger

> 🤖 AI-generated (Claude), reviewed by the setup's owner.

Paperless-ngx has its own AI suggestions, but they are manual (you click "suggest" per document). The tagger is a small external script that does it automatically and conservatively: it writes what it's sure about and flags the rest for a human.

## Where it runs

- A systemd timer on the always-on box, every 15 minutes (`paperless-tagger.timer`).
- When the inbox is empty it exits after one API call to Paperless, without starting Python's heavy imports or touching the LLM key.
- It talks to Paperless only through the REST API with its own API token (stored in a mode-600 file, not in the unit).
- `--dry-run` and `--json` print what it would do without writing anything. Use them for every test and every benchmark.

Useful flags: `--dry-run`, `--limit N`, `--doc ID`, `--backend ollama|openai`, `--model`, `--backfill` (re-run on already tagged documents), `-v`.

## What it does per document

Input: every document that has `Inbox` and doesn't have the review tag.

1. **Split check** for multi-page scans: does this PDF contain several separate documents? (See below.)
2. **Fields:** title, correspondent, document type, document date, and custom fields: amount (`Betrag`), deadline (`Frist`), invoice number (`Rechnungsnummer`).
3. **Topic tags** from a fixed list with a one-line definition each (`TAG_DEFINITIONS` in the script). The definitions matter more than the model: "`Wohnung` = the lease and its amendments (shared); `Wohnen` = everything else about the flat (private)" is the kind of rule that stops a model from guessing.
4. **Household tags:** per-child tags, and a tag for documents addressed to another household member (detected from the address block, and only when both first and last name appear; a mention as "mother of" doesn't count).
5. **Deadline:** a future deadline sets the `Frist` tag. A Paperless workflow sends a push three days before the date.
6. Write back via the API, then either remove `Inbox` (done) or add the review tag plus a note.

The prompt includes the 3 most similar filed documents as examples (see [Learning from your corrections](#learning-from-your-corrections)), the household's names, the existing correspondents, document types and tag definitions, so the model can reuse what exists instead of inventing near-duplicates. The model answers in a fixed JSON schema with a confidence per field.

## Learning from your corrections

An LLM doesn't learn from your edits by itself. The tagger now feeds them back as context:

1. **Similar filed documents as examples.** Before each document, the tagger looks up the 3 most similar documents that are already filed (no `Inbox`, no review tag) and puts them into the prompt with their final correspondent, document type and topic tags. When you correct a document, the next similar one benefits right away. If your filing clearly backs a correspondent, the tagger accepts it even when that exact name isn't in the text (for example after you renamed a correspondent).
2. **Paperless's own suggestions as a hint.** `GET /api/documents/<id>/suggestions/` returns what Paperless's built-in classifier would assign. It costs nothing (no LLM call; the separate AI-suggestions endpoint is the one that calls the LLM). The catch: the classifier only learns correspondents, types and tags whose matching is set to **Auto**.

Results on 137 documents the owner had corrected, dry run, old tagger vs new:

| | Before | Similar documents | + Paperless hint |
|---|---|---|---|
| Correspondent right | 104 | 123 | 123 |
| Document type right | 95 | 102–103 | 104 |
| Flagged for review | 98 | 77–78 | 74 |
| Cost per document | ~0.2 ct | ~4% more prompt text | same |

Almost all the gain came from the similar documents; the Paperless hint was within run-to-run noise here, because few of our correspondents use Auto matching. One new failure mode: a document got a correspondent copied from two similar examples that was wrong, though it was still flagged for review. Note that the test set overlaps with the examples the tagger can draw on, so treat the numbers as optimistic.

This only works if reviewed documents lose **both** the review tag and `Inbox`: documents that still carry either are not used as examples.

## Guards

The tagger doesn't trust the model blindly:

| Situation | What happens |
|---|---|
| Model proposes a correspondent that doesn't exist | Created automatically at confidence ≥ 0.7 (ALL-CAPS names tidied, reused for the next document). Below that: a suggestion in the note, review tag |
| Model's date vs. dates found in the text | The model's date wins if the text backs it up. (An earlier heuristic that preferred "the first date next to a *Datum* label" was a bug: it picked up enclosed invoices' dates) |
| Deadline not found in the text | Dropped. Local Qwen invented deadlines that weren't there |
| Split proposal with invalid page ranges | Ignored, document flagged |
| Split confidence < 0.85 | Not split, only flagged with the proposed ranges in the note |
| A paper mailing with sheet counters (`0005/0044`) that form one complete sequence | Kept as one document in code, without asking the model |
| Anything else uncertain | Review tag + a `[paperless-tagger]` note with the reasons |

## Splitting multi-document scans

When you scan a stack in one pass, Paperless gets one PDF with several letters. The tagger asks the model for page ranges, e.g. `1, 2, 3–4, 5, 6, 7–8, 9–10`, plus a confidence. Above 0.85 it uses Paperless's split operation; below it only flags.

Prompt rules that fixed most errors:
- **Enclosures stay with their letter.** Terms and conditions, price lists, third-party statements behind a cover letter are part of that letter, even if they have their own title or come from another company.
- **Confidence has a scale.** High only when every part starts with its own address block and date. Low when an enclosure is hard to tell from a separate document.
- **Blank pages** belong to the part before them.

Long single documents (an 88-page bank mailing) were the classic false positive until the sheet-counter check moved into code.

**Finishing a split.** The split runs inside Paperless as a background task, and on the Pi the parts needed 20 to 45 minutes to show up. The first version waited a fixed time (10 minutes plus 45 seconds per page), then gave up and kept the original with a note. That left 7 originals next to their parts before anyone noticed. Now the run doesn't wait at all: the request goes into a small state file, and every 15-minute run checks it. The original goes to the trash only when all parts exist; if parts are still missing after 24 hours, it gets `Prüfen` and a note.

**Threshold.** We considered lowering the split threshold from 0.85 to 0.80, because Haiku gave 0.80 on two benchmark scans whose ranges were right. The live history said no: of the 10 real proposals between 0.80 and 0.85, the human reviewer split none (8 kept whole, 2 deleted). Check your own review history before you move it.

## The review workflow (for the humans)

1. Open a saved view "Review" (documents with the review tag), shown on the dashboard and in the sidebar.
2. Open a document → **Notes** tab. The `[paperless-tagger]` note lists the reasons, e.g. *"No matching correspondent, suggestion: …"* or *"Split suggested: 1–2, 3–5 (0.70)"*.
3. Fix what's wrong in **Details**. For a suggested correspondent, type the name and let Paperless create it.
4. **Remove both the review tag and `Inbox`, then save.** If you only remove the review tag, the next run treats the document as new and overwrites your edits.

Bulk edit works for many documents whose suggestions are all right: select them in the view and remove both tags from the toolbar.

What drives the review rate in practice is mostly not the model: missing correspondents, own-name correspondents matching every letter, and duplicates. After fixing those, the estimated review rate on our benchmark set dropped from 47 of 51 to about 26.

## Swapping backends

Ollama and OpenRouter both speak the OpenAI-compatible chat API, so the backend is a base URL and a model name. One exception: for Ollama we use the **native** API (`/api/chat`), because the OpenAI-compatible `/v1` endpoint can't raise the context window (`num_ctx`), and long scans don't fit in the default. See [local-llm-ollama.md](local-llm-ollama.md).

For OpenRouter, send provider preferences with every request:

```json
{ "provider": { "zdr": true, "data_collection": "deny" } }
```
