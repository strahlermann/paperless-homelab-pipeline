# Local LLM with Ollama

> 🤖 AI-generated (Claude), reviewed by the setup's owner.

The first version of this setup ran the tagger entirely at home: Paperless on a Raspberry Pi, and the model on a desktop that is normally switched off. The Pi woke the desktop at night, ran the tagger there and let it power itself off again. This page describes how that worked, what it cost in speed and accuracy, and when it's the better choice.

**Short version:** it works, and it's a good fit if you have a GPU box that's on anyway, or if no document text may leave the house. For us (CPU-only, box normally off, Paperless AI search already using a hosted embedding model) a cloud model won on accuracy, speed and convenience, so we switched. See [benchmark.md](benchmark.md).

## Hardware and model

| | Our setup |
|---|---|
| Machine | Ryzen 9 7900 (12 cores), 64 GB RAM, NVMe. GPU not used |
| OS | Manjaro (Arch-based) |
| Ollama model | `qwen3:30b-a3b-instruct-2507-q4_K_M` (30B mixture-of-experts, ~3B active per token, 4-bit) |
| Speed | ~160 tokens/s prompt processing, ~20 tokens/s generation, CPU only |
| Throughput | 22 documents in 31 min (first round), 20 fresh scans in 19 min |

Why this model: an MoE with ~3B active parameters is fast enough on CPU, and at 30B total it handles German letters, amounts and dates well. The q4 weights need roughly 18–20 GB of RAM plus context.

**Rough sizing guidance** (inferred, not measured here beyond our own box):
- **CPU only, 32 GB+ RAM:** a 30B-A3B MoE at q4 is usable for overnight batches. Expect minutes per long document.
- **GPU with 24 GB VRAM:** the same model fits and should be many times faster, which removes the speed argument for the cloud.
- **8–16 GB GPU or small RAM:** 7B–14B models fit. Expect noticeably worse field extraction and splitting than in our numbers; benchmark before trusting them.
- **On a Raspberry Pi:** not realistic for this job. Even a small local embedding model (e5-small) for Paperless's search index was too slow and hung.

## Ollama settings that mattered

- **Use the native API (`/api/chat`), not `/v1`.** The OpenAI-compatible endpoint doesn't let you raise `num_ctx`, and multi-page scans need a larger context than Ollama's default. With a too-small context the model silently sees only part of the document.
- Set `num_ctx` per request large enough for your longest documents (prompt + OCR text + JSON answer).
- Ask for JSON output and validate it. Qwen produced invalid page ranges for splits more often than the cloud models; the tagger discards those.
- Keep the model loaded for the duration of a batch (`keep_alive`), so it isn't reloaded per document.
- `ollama pull` the model in the maintenance run, not during tagging.

## Wake-on-demand design

```
always-on box (timer, nightly)               heavy box (normally off)
  ├─ any documents in Inbox?  ── no ──▶ exit
  ├─ wakeonlan <MAC>
  ├─ wait for SSH
  └─ ssh -T tagger@heavy  ───────────────▶  forced command: run-remote.sh
                                               flock
                                               tagger.py  ──▶ Ollama (localhost)
                                                          ──▶ Paperless API on the always-on box
                                               summary on stdout
                                               power off (guarded)
  ◀──────────── summary ─────────────────
  push notification
```

Pieces:
1. **Wake-on-LAN** enabled in the BIOS and on the NIC (`ethtool <nic> | grep Wake-on` should show `g`). Give both machines fixed DHCP reservations; the MAC and IP are hardcoded in a few places. WoL doesn't cross NAT (e.g. from WSL), so always send it from a box on the LAN.
2. **A dedicated `tagger` user** on the heavy box whose SSH key in `authorized_keys` is restricted:
   ```
   restrict,from="<driver-ip>",command="/opt/paperless-tagger/run-remote.sh" ssh-ed25519 AAAA… driver-tagger
   ```
   Every login with that key runs the tagger and may power the machine off. **Never use it for testing.** Create a second, normal admin key for maintenance and dry runs.
3. **Guarded power-off** in `run-remote.sh`. Only power off if:
   - nobody is logged in (`who` / `loginctl` show no session besides the tagger),
   - uptime is under 30 minutes (so the box was woken for this run),
   - no `/etc/paperless-tagger.nopoweroff` flag exists.

   Otherwise run the tagger and leave the machine on, and say so in the summary. With these guards the trigger can run even when someone is using the desktop.
4. **The timer on the always-on box** skips the wake-up when the inbox is empty, and can also start other jobs while the box is awake (we started the NAS backup copy just before the tagger login, because the tagger powers off seconds after it's done).
5. **Paperless URL by IP.** Calling Paperless by hostname from the heavy box resolved IPv6 first and was about 100× slower.
6. **Logs:** the tagger writes to its own log file; add logrotate.

## Why we moved away from it

From the [benchmark](benchmark.md) on 20 fresh scans:

| | Local Qwen3 30B (CPU) | Claude Haiku (cloud) |
|---|---|---|
| Field checks right | 88% | 95% |
| Wrong splits that would have been written | 1 (at confidence 0.95), plus 1 missed split | 0 |
| Time for 20 docs | 19 min | ~2 min |
| Cost | electricity | ~$1.80 per 1,000 docs |
| Latency for a new scan | next night | ≤ 15 min |

And the privacy argument turned out weaker than expected: Paperless's AI search index already sent the full OCR text of every document to a hosted embedding model. Tagging locally while the index goes to the cloud gives you the slower, riskier tagger without the privacy benefit. The honest choices are **all cloud** (with zero-retention providers) or **all local** (local embeddings too, or Paperless AI switched off).

Local still has real upsides: no running cost, no dependence on a provider being up, and no text leaving the house if you go all the way.

## If you want to go fully local

- Put the tagger and Ollama on a box that's always on, ideally with a GPU, so you don't need the wake/sleep dance.
- Point Paperless's embedding model at a local endpoint too, or turn Paperless AI off and rely on full-text search.
- Run the benchmark on your own documents first. Our numbers are for German household mail and one CPU-only box.
