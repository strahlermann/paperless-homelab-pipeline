# Paperless homelab: scan, mail, LLM tagging, backups

> 🤖 **AI-generated.** This repo was written by Claude (Anthropic) from the notes, runbook and work logs of a real setup, and reviewed by its owner. The setup itself was largely built by Claude Code agents working on the machines, see [docs/how-it-was-built.md](docs/how-it-was-built.md). Check commands against your own system before running them.

A household document pipeline built on [Paperless-ngx](https://docs.paperless-ngx.com/): every letter that arrives by post or by email ends up as a searchable, tagged PDF, with an LLM filling in the metadata and a human reviewing only the documents the model is unsure about.

This repo is a write-up of a setup that has been running for real, written as a plan so you can build something similar on whatever hardware you have. It covers the architecture, the decisions behind it, a benchmark of a **local LLM (Ollama + Qwen3 30B)** against several cloud models, the backup and maintenance setup, and the mistakes we made along the way.

> **Status:** live since October 2026. About 1,000 documents (an old archive imported and reviewed, plus ongoing scans and five mailboxes). Numbers in this repo come from that setup.

## Contents

| File | What it covers |
|---|---|
| this README | Goal, architecture, hardware roles, build plan, lessons learned |
| [docs/tagger.md](docs/tagger.md) | How the LLM tagger works, the review workflow, splitting multi-document scans |
| [docs/local-llm-ollama.md](docs/local-llm-ollama.md) | Running the tagger on a local Ollama box with Wake-on-LAN, and when that's worth it |
| [docs/benchmark.md](docs/benchmark.md) | Local Qwen vs Claude Haiku vs Mistral Small/Large: accuracy, splits, speed, cost |
| [docs/backups-and-maintenance.md](docs/backups-and-maintenance.md) | restic to an off-site VPS and a NAS, restore tests, the 2-monthly maintenance run |
| [docs/how-it-was-built.md](docs/how-it-was-built.md) | How AI agents (Claude Code Remote Control sessions on the Pi, driving the local-AI desktop over SSH) built and run this, and the safety rules |
| [scripts/](scripts/) | The backup scripts, cleaned up and parameterised |

---

## 1. What it does

```
 Scanner ──SFTP (key only)──┐
                            ▼
 Mailboxes ──IMAP──▶  Paperless-ngx on the always-on box  ──▶ LLM tagger (every 15 min)
                            │   OCR, search, workflows          title, correspondent, type,
                            │                                   date, amount, deadline, tags,
                            │                                   split multi-document scans
                            │
                            ├──▶ nightly restic backup ──▶ off-site VPS (encrypted)
                            └──▶ every 2 months: wake NAS box ──▶ second backup copy,
                                                                  updates, ZFS scrub, power off
```

1. **Input.** A document scanner pushes PDFs over SFTP straight into Paperless's consume folder, no PC involved. Paperless polls five IMAP mailboxes every 10 minutes and imports PDF attachments.
2. **OCR and storage.** Paperless OCRs (German + English here), stores the file under `Year/Correspondent/Date Title.pdf`, and adds the `Inbox` tag.
3. **Tagging.** Every 15 minutes the tagger picks up everything in `Inbox`, sends the OCR text to an LLM and writes back title, correspondent, document type, date, custom fields (amount, deadline, invoice number) and topic tags. It also detects scans that contain several documents and splits them.
4. **Review.** Documents the model is sure about leave the inbox. The rest get a `Prüfen` ("review") tag and a note that says why. You fix them in the normal Paperless UI.
5. **Sharing.** Every document is private by default. Deterministic Paperless workflows, not the AI, decide what another household member can see (based on tags like "kids" or "flat").
6. **Deadlines.** A detected deadline sets a `Frist` tag, and a Paperless workflow sends a push notification three days before it.
7. **Backups.** Nightly encrypted restic backup off-site, plus a second copy on a NAS during the maintenance run.

## 2. Hardware roles: pick yours

The setup separates three roles. You don't need three machines; you need to decide which box plays which role.

| Role | Job | Needs | Our example |
|---|---|---|---|
| **Driver** (always on) | Paperless, mail polling, scanner target, tagger, timers, notifications, backups | Low idle power, 4–8 GB RAM, SSD. Runs 24/7 | Raspberry Pi 5, 8 GB, 1 TB NVMe |
| **Heavy box** (optional, woken on demand) | Local LLM, NAS with the second backup copy | Lots of RAM or a GPU, big disks. Ideally supports Wake-on-LAN | Ryzen 9 7900 desktop, 64 GB RAM, no GPU used, ZFS pool 4×3.6 TB |
| **Off-site** | Encrypted backup target | Some storage, SSH/SFTP | Small rented VPS |

**How to choose:**

- **Only one small always-on box?** Run Paperless and the tagger there with a cloud model. That's what this setup ended up doing. It's cheap (about $2 per 1,000 documents, see [benchmark](docs/benchmark.md)) and fast.
- **A big box that's on anyway (a home server with a GPU)?** Run everything on it, including Ollama. Local LLMs become attractive when the hardware is already running and has a GPU.
- **A big box that's normally off?** Keep Paperless on the small box, and wake the big one only for batch work (local LLM tagging, backup copy, scrub). We started like this; [docs/local-llm-ollama.md](docs/local-llm-ollama.md) describes how, including the pitfalls.
- **Paperless on a Pi works fine** for a household. The limits we hit: local embeddings for Paperless's AI search were too slow on the Pi (we use a hosted embedding model instead), and bulk imports are slow and noisy (about 20 PDFs per 10 minutes on a Pi 5 with OCR).
- **Off-site:** anything you can reach over SFTP. restic encrypts on the client, so the target only ever sees ciphertext. A storage box, B2/S3 or a cheap VPS all work.

## 3. Software

| Part | Choice | Notes |
|---|---|---|
| DMS | Paperless-ngx 3.3.1 in Docker Compose (Postgres + Valkey) | **Pin the image tag.** Bind the port to the LAN IP, not `0.0.0.0` |
| Paperless AI | Built-in LLM suggestions + embedding search index | Embeddings via OpenRouter (`baai/bge-m3`); local e5-small hung on the Pi. Full rebuild cost 1–2 cents |
| LLM tagger | One Python script + systemd timer, talks to the Paperless REST API | Backend pluggable over the OpenAI-compatible API: Ollama or OpenRouter. See [docs/tagger.md](docs/tagger.md) |
| Cloud LLM access | OpenRouter with zero-data-retention providers and `data_collection: deny` | Also set the account-wide no-retention filter in OpenRouter's privacy settings |
| Local LLM | Ollama, `qwen3:30b-a3b-instruct-2507-q4_K_M` | CPU-only in our test. See [docs/local-llm-ollama.md](docs/local-llm-ollama.md) |
| Backups | restic over SFTP to chrooted, key-only users | [docs/backups-and-maintenance.md](docs/backups-and-maintenance.md) |
| Notifications | ntfy.sh topic | Make sure someone actually subscribes (see lessons learned) |
| Scanner | Brother ADS-series document scanner, scan-to-SFTP profiles | 300 dpi colour, duplex, blank-page skip; one normal and one "continuous" profile |

## 4. Build plan

Work through it in this order. Each step is useful on its own, and the order puts backups before anything clever.

**Phase 1: Paperless on the driver**
1. Install Docker and Paperless-ngx with Postgres. Pin the image version. Bind the web port to the LAN address only (Docker-published ports bypass `ufw`).
2. Set OCR languages, time zone, consumer polling.
3. Set `PAPERLESS_CONSUMER_DELETE_DUPLICATES=true` **before** connecting any mailbox (see lessons learned).
4. Create the storage path `{{created_year}}/{{correspondent}}/{{created}} {{title}}` and a workflow that assigns it, plus the owner, on every new document. That gives you a readable archive on disk if Paperless ever breaks.
5. Create the tags: workflow tags (`Inbox` as the inbox tag, a review tag, `mail`), 10–15 topic tags, sharing tags. Create custom fields for amount, deadline, invoice number.
6. Users and sharing: one admin, other household members as normal users. Make every document owned by admin by default and share via tag-triggered workflows.

**Phase 2: Backups (do this before you rely on it)**
7. Nightly `document_exporter` + `pg_dump` + restic to off-site. Test a restore. Save the restic password in your password manager. Scripts in [scripts/](scripts/).

**Phase 3: Inputs**
8. Scanner: SFTP user with a chroot and key-only login, target directory = consume folder.
9. Mailboxes: one mail rule per account, PDF attachments only, owner admin, tag `mail`. Preview what a rule would match before enabling it. For Gmail use an app password and a processed label.

**Phase 4: The tagger**
10. Write or adapt the tagger ([docs/tagger.md](docs/tagger.md)). Start with `--dry-run` only.
11. Build a small test set (20–50 documents, including a few mixed scans) and benchmark the backends you can run ([docs/benchmark.md](docs/benchmark.md)). Write the expected values **before** you look at any model output.
12. Run it on a timer (every 15 minutes). Notify a summary.

**Phase 5: Clean-up and old documents**
13. After the first few hundred documents: merge duplicate correspondents, trash duplicate documents, check who can see what (log in as the other user).
14. Import old archives in throttled batches (feed the consume folder only while Paperless has no backlog).

**Phase 6: Hardening and maintenance**
15. Separate SSH keys per purpose, sudo off by default, persistent journal, log rotation.
16. A scheduled maintenance run for the heavy box: wake, second backup copy + `restic check --read-data`, OS updates, ZFS scrub, power off.

## 5. Key decisions

| Decision | Why |
|---|---|
| Paperless on the always-on low-power box | Documents are searchable right away; AI and heavy work are batch jobs |
| Cloud LLM (Claude Haiku via OpenRouter) for tagging instead of local Qwen | Most accurate on fresh documents, never wrote a wrong split, 3 min instead of 31 min, ~$1.80/1,000 docs, no box to wake. Details in [benchmark](docs/benchmark.md) |
| Didn't keep the local LLM "for privacy" | Paperless's AI search index already sends the full OCR text of every document to a hosted embedding model. Local tagging only buys privacy if the index is local too, or Paperless AI is off |
| Sharing decided by workflows, not by the model | The AI only labels. A deterministic rule decides who sees what, so a model mistake can't leak a document by itself |
| Unsure → review tag + note, never silent | The tagger writes everything it's sure about and explains the rest. Wrong data is worse than missing data |
| Auto-split only at confidence ≥ 0.85 | A wrong split is the one model error that damages documents; below the threshold it only flags |
| Auto-create correspondents only at confidence ≥ 0.7 | In the benchmark every suggestion at ≥ 0.7 was right; before that, nearly every new sender landed in review |
| Scanner → SFTP with a key | No PC needed, no SMB share, no password |
| restic, encrypted client-side | Untrusted off-site storage is fine |
| Heavy box normally off, woken by timer | Saves power; it's only needed every two months |

## 6. Lessons learned

**Paperless**
- **Duplicate rejection was off.** Paperless only logged a warning and imported the same mail attachment again and again (one shop's terms and conditions arrived 34 times). Set `PAPERLESS_CONSUMER_DELETE_DUPLICATES=true` first. Note that rescans are never caught: a new scan is never byte-identical to an old one.
- **Documents without an owner are visible to every user.** Scanner documents had no owner, so another household member saw private documents. Fix: a workflow sets the owner on consumption; sharing happens only through tags.
- **Own-name correspondents are poison.** Correspondents named after yourself matched literally on almost every letter (your name is in the address field) and alone sent 34 of 51 benchmark documents to review. Delete them, or set their matching to "none".
- **Check the "inbox tag" flag on every tag.** One normal tag had it set by accident, so Paperless counted all its documents as "in the inbox".
- **Paperless's own classifier only learns what's set to "Auto".** It retrains from your corrections, but only for correspondents, types and tags whose matching is "Auto". Ours had that on 15 of 173 correspondents, so it learned almost nothing. Its suggestions are available for free from the API (`/api/documents/<id>/suggestions/`, no LLM involved; the separate AI-suggestions endpoint does call the LLM).
- **Make the LLM tagger learn from your filing.** Giving the model the 3 most similar already-filed documents as examples raised correct correspondents from 104 to 123 of 137 corrected documents, at the same cost. See [docs/tagger.md](docs/tagger.md#learning-from-your-corrections).
- **Bulk imports need throttling.** On a Pi 5, Paperless handles about 20 PDFs per 10 minutes (OCR + AI index). Feed batches only when the consume queue is empty, and not at night if the box sits in a bedroom (it gets loud).
- **Old documents get odd dates.** 17 of 482 imported archive documents ended up dated 1980. Review dates after a bulk import.

**The tagger and models**
- **The model wasn't always the problem.** A date heuristic in the tagger overrode the model's correct date with the first date next to a "Datum"-style label (e.g. an enclosed invoice). Fixing the code gained more than switching models would have.
- **Splitting multi-document scans is the hard part.** Field extraction was nearly a tie between models; splits separated them. Rules that helped: enclosures (terms, price lists, statements) stay with their cover letter; confidence high only when every part starts with its own address block and date; mailing sheet counters (`0005/0044`) are checked in code, not left to the model.
- **Benchmark honestly.** Our first round was tuned and scored on the same documents, with expected values written after seeing one model's output. The second round used fresh scans with expected values committed before any model ran. Only the second round is a fair comparison.
- **Shared provider pools rate-limit.** Mistral Small via OpenRouter's shared pool failed on 15 of 42 documents. For a nightly job, use a provider with dedicated capacity or your own key.
- **The review step must clear both tags.** If you only remove the review tag and leave `Inbox`, the next run treats the document as new and tags it again, and it doesn't count as a learning example. We re-ran 206 documents by accident this way.
- **A big review pile goes faster in batches with one confirmation.** After the archive import, 315 documents were flagged. An agent read them all and sorted them into four lists (delete: not paperwork; delete: near-copies; fix; fine), the owner confirmed once, and the pile was cleared: 117 trashed, 63 fixed, the rest just unflagged. Deleted documents stay in the Paperless trash for 30 days.

**Infrastructure**
- **A forced-command SSH key means every login is a real run.** The local-LLM box had a key that ran the tagger and powered off on login. Use a separate admin key for anything else, and never "just test" the forced-command key.
- **Power-off logic needs guards.** Only power off the heavy box if nobody is logged in, it was woken for this job (uptime < 30 min), and no "keep on" flag exists. Otherwise you shut down someone's desktop.
- **Docker-published ports bypass `ufw`.** Bind to the LAN IP explicitly. Also check that the port isn't reachable over global IPv6.
- **Use IPs or fix name resolution.** A hostname that resolved to IPv6 first made API calls about 100× slower.
- **sshd chroots need every parent directory root-owned and not group/world-writable.** The ZFS pool's mount point was owned by a stale UID and broke the chroot. And the ssh unit is `sshd` on Arch-based distros, `ssh` on Debian.
- **Some scanners want plain RSA `.pub` keys** for their SFTP client.
- **The Raspberry Pi OS journal is volatile by default.** Logs vanished on reboot until we made it persistent.
- **Notifications nobody receives are worthless.** Everything went to an ntfy topic for a day before we noticed nobody had the app. Treat the topic name like a password (anyone who knows it can read the messages).
- **Sudo for automation: off by default.** A small `on [duration] | off | status` switch writes a NOPASSWD sudoers drop-in (checked with `visudo -cf`) and removes it automatically after two hours. A human has to type their password to open it.

## 7. Costs

| Item | Cost |
|---|---|
| LLM tagging (Claude Haiku via OpenRouter) | ~$1.80 per 1,000 documents |
| Embeddings for Paperless AI search (bge-m3) | 1–2 cents for a full rebuild of a few hundred documents |
| Off-site backup | Whatever your VPS or storage costs; the data is small (~250 MiB for 300 documents) |
| Power | Pi 5 always on; heavy box a few hours every two months |

## 8. Ideas not done yet

- An Ollama backend on a GPU box: the CPU-only run was the main reason local lost on speed (see [docs/local-llm-ollama.md](docs/local-llm-ollama.md)).
- A fully local variant (local embeddings + local tagger) for people who want no document text to leave the house.
- Setting more correspondents and types to "Auto" matching so Paperless's free classifier learns more (risk: it then assigns them on consumption, and wrong guesses stick unless the tagger may override them).
- Deduplicating rescans by content (same month and run number for payslips, for example) instead of by checksum.

## About the tagger code

The tagger is a single Python script in a private repo. This write-up describes its design in enough detail to rebuild it ([docs/tagger.md](docs/tagger.md)); the code itself may be published separately.
