# Tagger backend benchmark

> 🤖 AI-generated (Claude), reviewed by the setup's owner.

Four backends, the same tagger script and prompt, dry run only (nothing written to Paperless). October 2026, German household documents.

| Backend | Where | Notes |
|---|---|---|
| Qwen3 30B-A3B Instruct 2507, q4_K_M | Ollama, local, CPU only (Ryzen 9 7900, 64 GB) | Native Ollama API |
| Claude Haiku 5.5 | OpenRouter | Served by Google, zero data retention |
| Mistral Small 2603 | OpenRouter | Served by Mistral (EU) |
| Mistral Large 4.0 | OpenRouter | Served by Mistral (EU) |

## Method

- **Round 1 (tuning set):** 22 documents, including two real multi-document scans. The split prompt was improved on this set, and the expected values were written after seeing the first Haiku run. **This round favours Haiku and is only for reference.**
- **Round 2 (fresh set):** 20 documents scanned on the morning of the test, none used for tuning. The expected values were written from the OCR text and committed to git **before any model ran**. Two of them are mixed scans and count only for splits; one had no usable text.
- **Scoring:** per field. A correspondent counts as right if it matches the expected name ignoring case and punctuation, or one contains the other. Some fields accept more than one answer (e.g. "invoice or letter" for a utility bill).
- All OpenRouter calls used `{"zdr": true, "data_collection": "deny"}`.

## Results on the fresh set

| Field | Qwen3 30B (local) | Claude Haiku | Mistral Small | Mistral Large |
|---|---|---|---|---|
| Correspondent | 15/17 | **17/17** | 8/10 | 16/17 |
| Document type | 14/17 | **16/17** | 9/10 | 11/17 |
| Date | 12/15 | 12/15 | 7/9 | 12/15 |
| Amount | 14/14 | 14/14 | 7/7 | 13/14 |
| Deadline | 13/13 | 13/13 | 7/7 | 13/13 |
| Invoice number | 17/17 | 17/17 | 10/10 | 17/17 |
| Child tags | 15/17 | **17/17** | 9/10 | 16/17 |
| Topic tags | 12/17 | **15/17** | 8/10 | 14/17 |
| Split ranges | 10/12 | 11/12 | 6/7 | **12/12** |
| **All fields except splits** | **88%** (112/127) | **95%** (121/127) | 89% (65/73) | **88%** (112/127) |

Mistral Small only covers the 10 documents that survived rate limiting. Two of the three date misses for every model were a tagger bug (see below), not the models.

## Splits: where the models really differ

Field extraction was close. Splitting multi-document scans is where a model can do real damage, because a confident wrong split gets written without review.

| Scan | Qwen | Haiku | Mistral Small | Mistral Large |
|---|---|---|---|---|
| 10 pages, 7 tax certificates and letters (expected 7 parts) | wrong 6 parts at 0.99 (round 1) | right, 0.85 | failed (rate limit) | right, 0.95 |
| 88-page single bank mailing (should not split) | – | right, one doc | right | right |
| Landlord statement with enclosures (one doc) | – | right | failed | right |
| Deposit statement + utility statement (2 parts) | missed the split | right, 0.85 | failed | right, 0.95 |
| Fax with enclosed hospital invoices (one doc) | **wrong split at 0.95** | wrong proposal at 0.70, only flagged | **wrong split at 0.98** | right |

Mistral Large was the best splitter. Haiku never produced a wrong split above the 0.85 threshold, so its mistakes cost a review, not a broken document. Qwen and Mistral Small each would have written one wrong split.

Prompt improvements between rounds raised Haiku's splits on the tuning set from 12/14 to 14/14; the same prompt made Qwen's split answers worse (invalid page ranges).

## Time and cost

| Backend | Docs | Wall time | Cost | Per 1,000 docs |
|---|---|---|---|---|
| Qwen3 30B (local CPU) | 20 fresh | 19 min | electricity | – |
| Claude Haiku | 51 (incl. split parts) | 5 min | $0.09 | **$1.81** |
| Mistral Small | 31 | 78 min (mostly rate-limit waits) | $0.03 | $0.91 |
| Mistral Large | 51 | 48 min (~35 s per call) | $0.50 | $9.88 |

## Other observations

- **Qwen** put a street address into a correspondent name, picked a wrong correspondent once, and invented two deadlines that weren't in the text (the tagger's guard dropped them).
- **Mistral Large** often left the document type empty or invented a type outside the list.
- **Mistral Small** through OpenRouter's shared pool: 15 of 42 documents failed after six retries. Not usable for a scheduled job without your own key.
- **A tagger bug cost every model the same two dates.** A rule preferred the first date next to a "…datum" label over the model's answer, which picked up an enclosed invoice's date on one document and a billing-period end on another. After the fix, replaying all saved runs gave 34 of 35 dates right, up from 32.
- **The review rate didn't separate the models** (26/28 vs 19/22 in round 1). Most flags came from rules (missing correspondents, own-name correspondents, deadlines), not from model uncertainty.

## Decision

Claude Haiku via OpenRouter, running on the always-on box every 15 minutes. Most accurate on fresh documents, no harmful splits, fast, cheap, and the heavy box no longer has to be woken. First live run: 19 scans tagged in 2.5 minutes.

## Round 3: Paperless' built-in assignment vs the tagger

Paperless-ngx 3.3 has two built-in ways to fill in metadata, and both were run on the 37 benchmark documents that still existed (read only; [tagger/bench/paperless_builtin.py](../tagger/bench/paperless_builtin.py)):

- **Classic matching:** the classifier Paperless trains on your filed documents, plus match rules. It only knows correspondents, types and tags set to "Auto" matching.
- **Paperless AI:** the same Claude Haiku 5.5 through OpenRouter, with similar documents from the embedding index as context (the document itself excluded).

Two references: **A** = the expected values written for rounds 1 and 2, **B** = what is filed in Paperless today after human review (it has gaps, and includes document types created after A).

| Field (A · B) | Tagger (Haiku) | Paperless AI | Classic |
|---|---|---|---|
| Correspondent | 33/33 · 32/37 | 33/33 · 32/37 | 13/33 · 17/37 |
| Document type | **18/33 · 28/37** | 13/33 · 20/37 | 1/33 · 12/37 |
| Date | 29/31 · 34/37 | 29/31 · **36/37** | 11/31 · 10/37 |
| Child tags | **33/33 · 37/37** | 31/33 · 35/37 | 26/33 · 29/37 |
| Topic tags | **29/33** · 25/37 | 23/33 · 24/37 | 3/33 · 7/37 |
| Time for 37 documents | 5.4 min, $0.11 | 2.9 min | 6 s |

- **Paperless AI** matches the tagger on correspondent and date, but picks other document types more often (reminder, certificate, application where a plain letter is filed), leaves some types empty and over-tags topics. It has no amount, deadline, invoice number, split or review flag, and it only suggests: you click through the suggestions per document. Each document costs two calls when an output language is set (the second one translates).
- **Classic matching** loses clearly, even though it was trained on these very documents.
- **Verdict:** keep the tagger. Paperless AI adds nothing it doesn't already do.

## OpenRouter's Auto Router

`openrouter/auto` picks a model per request; a `cost_tier` (`low` to `max`) sets the price band. Same 37 documents, `provider.require_parameters` so only models with structured output qualify:

| | Haiku 5.5 (fixed) | Auto, medium | Auto, high |
|---|---|---|---|
| Models it routed to | – | DeepSeek V4.1 Flash, Gemini 3.8 Flash, GLM 5.3 | GPT-6.1 Sol, Gemini 3.8 Flash, Sonnet 5.5 |
| Document type (A · B) | 18/33 · **28/37** | 17/33 · 26/37 | **20/33** · 27/37 |
| Topic tags (A · B) | **29/33** · 25/37 | 29/33 · 25/37 | 24/33 · **28/37** |
| Other fields | equal within one document | | |
| Cost, time | **$0.11, 5.4 min** | $0.22, 8.4 min | $0.89, 8.7 min |

No gain beyond noise, at two to eight times the cost. The router also switched models between the calls for one document (split check on one model, tagging on another) and ranks models by what other OpenRouter users spent on a task type in the last seven days, so what a tier means can change from week to week. If you want a stronger model, pin one, for example only for documents the tagger already flags for review.

## Caveats

- Small sample: 20 fresh documents, only two real mixed scans in the tuning set. Treat percentages as ±5–10 points.
- One language (German), one kind of documents (household mail), one CPU-only local box. A GPU and a different local model could change the local result, especially on speed.
- Run your own benchmark before you pick. The method above (expected values first, dry run only, score per field, look at the disagreements side by side) takes an evening.
