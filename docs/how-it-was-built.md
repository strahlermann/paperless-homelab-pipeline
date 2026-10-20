# How it was built: with AI agents on the machines

> This page, like the rest of the repo, was written by Claude (Anthropic's AI). A human reviewed it.

None of this was typed by hand from a tutorial. The person who owns the setup described what they wanted, and AI agents (Claude Code) did most of the work on the machines themselves: reading configs, writing scripts, running benchmarks and reporting back. The human made the decisions, typed sudo passwords and approved anything risky. This page describes how that worked, because the way of working is half of what made it possible in a few evenings.

## Stage 1: a chat and a session on the desktop

The project started in a normal Claude chat and with a Claude Code session running directly on the desktop, the "local AI" box (Ollama, ZFS NAS, no GPU used). In that stage:

- Ollama and the Qwen3 30B model were set up on the desktop, and the first version of the tagger was written there.
- Paperless already ran on the Raspberry Pi; the desktop was woken by the Pi at night to tag documents.
- To let the agent work, the desktop user had passwordless `sudo` (`NOPASSWD: ALL`). That was convenient and became the first thing to fix later.
- The chat produced a **runbook**: one Markdown file with every machine, path, decision and open problem, but no secrets. That file became the source of truth for everything that followed.

Two lessons from this stage: the improved tagger only existed in the agent's temporary scratch folder for a while (copy agent work into git right away), and a machine that's normally off is a bad place for the agent that runs everything.

## Stage 2: the Pi as the single "driver"

The plan in the runbook was to make the always-on Pi the one place everything is driven from:

- **A persistent Claude Code Remote Control session on the Pi**, running as a systemd user service in its own working folder. Remote Control means the session runs on the Pi with its local tools, files and network access, but is controlled from the Claude app or website. You can start work from your phone and the agent acts on the machine at home.
- **The desktop got no agent of its own.** The Pi reaches it over SSH with a dedicated admin key (`ssh <desktop>`) and a small helper (`<desktop> wake | status | shell | off | tagger-dry-run | maintain`).
- **Separate keys per purpose.** The tagger's forced-command key (every login = a real tagger run + power-off) is never used by the agent; a rule in the project instructions says so explicitly.
- **Sudo switched off by default** on both machines. A small `claude-sudo on [duration] | off | status` command opens passwordless sudo for a limited time (default 2 hours, then it closes itself). Turning it on needs the human's password, so a human always opens the door.

## Stage 3: a Claude project with one thread per task

The day-to-day work then ran in a **Claude project**: one shared chat where the owner posts requests, plus one thread per task. Each task thread started its own Remote Control session on the Pi, in the same folder, with a written brief. Threads in this build:

| Thread | What the agent did on the machines |
|---|---|
| Backups | Wrote the restic scripts, the user ran them with sudo, the agent ran the first backups and restore tests |
| Security hardening | Admin key, sudo switch, guarded power-off, pinned Paperless, persistent journal, port binding |
| Tagger benchmark | Copied the tagger into git, made the backend pluggable, ran Qwen on the desktop and three cloud models, built a comparison page, then switched the live tagger to the winner |
| Data cleanup | Found duplicates and broken correspondents, showed a plan, applied it after a "yes" |
| Maintenance | Built the 2-monthly wake, update, scrub and power-off run |
| Mail accounts | Prepared mail rules, previewed what they would import, caught the missing duplicate rejection |
| Old archive | Surveyed the desktop's folders by file name only, then copied and fed ~860 PDFs in throttled batches |
| This write-up | Read the runbook and all threads and wrote this repo |

A coordinating agent watched the project chat, kept a shared memory file of facts and decisions (for example "the desktop is normally off", "never use the tagger key"), and handed each new thread what it needed to know.

## Rules that made this safe enough

These were written into the project instructions and repeated in every brief:

- **No secrets in chat, ever.** Passwords, API keys and tokens go into root-only files that the human creates; the agent only refers to the file path. App passwords for mail were entered by the human in the Paperless UI.
- **Show the plan before touching sshd, sudoers, the compose file or bulk data**, and wait for a "yes".
- **Validate before reloading:** `sshd -t`, `visudo -c`, `docker compose config`, with automatic rollback if a check fails.
- **Take a fresh export before any bulk change** to documents.
- **Dry run first.** Every tagger change and benchmark ran with `--dry-run` before anything was written.
- **When the agent needs root, it hands over a script** for the human to run with sudo, or asks for the sudo switch to be turned on for a limited time.
- Things only the human can do (router DHCP reservations, provider privacy settings, changing the scanner password) were collected into short checklists.

## What it was like

- **Fast.** Backups, hardening, the benchmark, the switch to a cloud model, the cleanup, the maintenance run and the archive import happened over about two days.
- **Lots of approvals.** The import thread alone needed many permission prompts in the Pi session. That's the price of keeping the agent on a short leash; it's worth it.
- **The agent made mistakes and said so.** It claimed Paperless rejected duplicates (it didn't), found out before importing, paused and fixed it. It guessed the wrong ssh unit name on Arch. It wrote the first benchmark's expected values after seeing one model's output and flagged that the round was biased. Ask it to show evidence (output, counts, file paths) for every claim.
- **The runbook and the shared memory were what made it work.** Each new session starts cold. Without a written source of truth, every thread would have had to rediscover the network, the keys and the rules.

## If you want to work the same way

1. Write a runbook first: machines, roles, paths, decisions, open problems, where secrets live (not the secrets).
2. Put one always-on machine in charge and run a persistent agent session there. Don't give agents on other machines broad access; reach them over SSH with narrow keys.
3. Keep sudo off by default with a timed switch.
4. Put your rules in the project instructions so every session starts with them.
5. One task per thread, and make each thread end in something you can check: a restore test, a dry run, a comparison table.
