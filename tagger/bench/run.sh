#!/usr/bin/env bash
# Benchmark run of tagger.py on pi5: dry run, JSON output only, nothing is written to Paperless.
#   bench/run.sh qwen|haiku [extra tagger args, e.g. --doc 295]
# qwen:  native Ollama API on the Ryzen through an SSH tunnel (admin key, `ssh ryzen`) on :11435
# haiku: OpenRouter, key read from the Paperless container secret at run time (never stored here)
set -euo pipefail
cd "$(dirname "$0")/.."
# Default: the tuning set from 2026-10-18. BENCH_DOCS="319 320 ..." picks another set.
TEST_DOCS=(282 283 284 286 290 293 295 296 297 298 299 300 301 302 303 304 313 314 315 316 317 318)
[ -n "${BENCH_DOCS:-}" ] && read -ra TEST_DOCS <<<"$BENCH_DOCS"
backend=${1:?qwen|haiku}; shift
out=runs/$(date +%Y%m%d-%H%M%S)-${backend//[:\/]/_}; mkdir -p "$out"
docs=(); for d in "${TEST_DOCS[@]}"; do docs+=(--doc "$d"); done
[ $# -gt 0 ] && docs=()
export TAGGER_ENV_FILE=$PWD/.env TAGGER_LOG_FILE=$PWD/$out/tagger.log TAGGER_LLM_LOG=$PWD/$out/llm.jsonl
case $backend in
  qwen)
    timeout 3 bash -c '</dev/tcp/127.0.0.1/11435' 2>/dev/null \
      || ssh -f -N -o ExitOnForwardFailure=yes -L 127.0.0.1:11435:127.0.0.1:11434 ryzen
    args=(--backend ollama --llm-url http://127.0.0.1:11435 --model qwen3:30b-a3b-instruct-2507-q4_K_M);;
  haiku)
    LLM_API_KEY=$(docker exec paperless-webserver-1 cat /run/secrets/openrouter_api_key); export LLM_API_KEY
    args=(--backend openai --llm-url https://openrouter.ai/api/v1 --model anthropic/claude-haiku-5.5
          --llm-extra '{"temperature": null, "provider": {"zdr": true, "data_collection": "deny"}}');;
  or:*)  # any OpenRouter model, e.g. or:mistralai/mistral-small-2603
    LLM_API_KEY=$(docker exec paperless-webserver-1 cat /run/secrets/openrouter_api_key); export LLM_API_KEY
    args=(--backend openai --llm-url https://openrouter.ai/api/v1 --model "${backend#or:}"
          --llm-extra '{"provider": {"zdr": true, "data_collection": "deny"}}');;
  *) echo "unknown backend $backend" >&2; exit 2;;
esac
start=$(date +%s)
.venv/bin/python tagger.py --dry-run --json --fresh -v "${args[@]}" "${docs[@]}" "$@" \
  >"$out/result.json" 2>"$out/stderr.log"
echo "$(( $(date +%s) - start ))" >"$out/wall_s"
echo "$out"
