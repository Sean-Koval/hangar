#!/usr/bin/env bash
# Re-capture tests/fixtures/streams/*.jsonl from the real harness CLIs, offline.
#
# Each CLI runs exactly as hangar runs it (harnesses.py), in a clean environment, against a scripted local model:
# mock_model.py speaks the Anthropic Messages API (claude, pi) and the OpenAI Responses API (codex); agy reaches
# mock_gemini.py through HTTPS_PROXY with a throwaway CA. No real model API is called. The script writes a file,
# runs a shell command and answers, so every harness shows the same activity. *-error.jsonl captures a failing
# model request (HTTP 400; for agy, an unreachable API).
#
# Usage: capture.sh OUT_DIR   with claude, codex, pi and agy on PATH (or CLAUDE=… CODEX=… PI=… AGY=…).
# Captured 2026-10-13 with Claude Code 2.1.296, codex 0.162.1, pi 1.1.0 and agy 1.3.3.
set -u
OUT=$(cd "${1:?usage: capture.sh OUT_DIR}" && pwd)
HERE=$(cd "$(dirname "$0")" && pwd)
CLAUDE=${CLAUDE:-$(command -v claude)} CODEX=${CODEX:-$(command -v codex)} PI=${PI:-$(command -v pi)}
AGY=${AGY:-$(command -v agy)}
W=$(mktemp -d)
trap 'kill $(jobs -p) 2>/dev/null; rm -rf "$W"' EXIT
BASE_PATH=/usr/local/bin:/usr/bin:/bin:$(dirname "$(command -v node)")
PROMPT="Create hello.txt saying hello, then show it"

fresh() {  # fresh working dir and HOME for one run
  rm -rf "$W/repo" "$W/home"
  mkdir -p "$W/repo" "$W/home"
}

model() {  # start mock_model.py on port $1 (extra env before the call, e.g. CAPTURE_FAIL=1)
  CAPTURE_DIR="$W/repo" python3 "$HERE/mock_model.py" "$1" &
  sleep 1
}

run_claude() {
  fresh
  (cd "$W/repo" && env -i PATH="$BASE_PATH" TERM=dumb IS_SANDBOX=1 HOME="$W/home" \
    ANTHROPIC_BASE_URL=http://127.0.0.1:18801 ANTHROPIC_API_KEY=sk-ant-capture DISABLE_AUTOUPDATER=1 \
    CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 NO_PROXY=127.0.0.1 timeout 90 "$CLAUDE" -p "$PROMPT" \
    --dangerously-skip-permissions --output-format stream-json --verbose --model claude-sonnet-4-5 \
    < /dev/null > "$W/$1" 2>&1)
}

run_codex() {
  fresh
  mkdir -p "$W/home/.codex"
  printf 'model = "gpt-5.5"\nmodel_provider = "mock"\n[model_providers.mock]\nname = "mock"\n%s\n%s\n%s\n' \
    'base_url = "http://127.0.0.1:18802/v1"' 'env_key = "MOCK_API_KEY"' 'wire_api = "responses"' \
    > "$W/home/.codex/config.toml"
  (cd "$W/repo" && env -i PATH="$BASE_PATH" TERM=dumb HOME="$W/home" MOCK_API_KEY=capture NO_PROXY=127.0.0.1 \
    timeout 90 "$CODEX" exec --json --dangerously-bypass-approvals-and-sandbox --skip-git-repo-check \
    "Create hello.txt saying hello, then add notes.md" < /dev/null > "$W/$1" 2>&1)
}

run_pi() {
  fresh
  mkdir -p "$W/home/.pi/agent"
  echo '{"providers": {"anthropic": {"baseUrl": "http://127.0.0.1:18803"}}}' > "$W/home/.pi/agent/models.json"
  (cd "$W/repo" && env -i PATH="$BASE_PATH" TERM=dumb HOME="$W/home" ANTHROPIC_API_KEY=sk-ant-capture PI_OFFLINE=1 \
    NO_PROXY=127.0.0.1 timeout 90 "$PI" -p --mode json --approve --model anthropic/claude-sonnet-4-5 \
    --session-id 7d3f9a52-1c4e-4b8a-9f61-2a0c5e8d4b17 "$PROMPT" < /dev/null > "$W/$1" 2>&1)
}

run_agy() {  # $2: proxy port (a closed port for the error capture)
  fresh
  mkdir -p "$W/home/.gemini/antigravity-cli"
  echo '{"modelProvider": "gemini"}' > "$W/home/.gemini/antigravity-cli/settings.json"
  (cd "$W/repo" && env -i PATH="$BASE_PATH" TERM=dumb HOME="$W/home" GEMINI_API_KEY=capture \
    SSL_CERT_FILE="$W/ca.crt" HTTPS_PROXY="http://127.0.0.1:$2" HTTP_PROXY="http://127.0.0.1:$2" timeout 90 "$AGY" \
    -p "$PROMPT" --output-format stream-json --dangerously-skip-permissions --print-timeout 40s \
    < /dev/null > "$W/$1" 2>&1)
}

model 18801; model 18802; model 18803
run_claude claude-code.jsonl
run_codex codex.jsonl
run_pi pi.jsonl
kill $(jobs -p); sleep 1
CAPTURE_FAIL=1 model 18801; CAPTURE_FAIL=1 model 18802
run_claude claude-code-error.jsonl
run_codex codex-error.jsonl

openssl req -x509 -newkey rsa:2048 -nodes -keyout "$W/ca.key" -out "$W/ca.crt" -days 1 -subj "/CN=capture CA" \
  -addext "basicConstraints=critical,CA:TRUE" -addext "keyUsage=critical,keyCertSign" 2>/dev/null
openssl req -newkey rsa:2048 -nodes -keyout "$W/leaf.key" -out "$W/leaf.csr" -subj "/CN=generativelanguage.googleapis.com" \
  2>/dev/null
printf "subjectAltName=DNS:*.googleapis.com,DNS:generativelanguage.googleapis.com\nextendedKeyUsage=serverAuth\n" \
  > "$W/leaf.ext"
openssl x509 -req -in "$W/leaf.csr" -CA "$W/ca.crt" -CAkey "$W/ca.key" -CAcreateserial -out "$W/leaf.crt" -days 1 \
  -extfile "$W/leaf.ext" 2>/dev/null
CAPTURE_DIR="$W/repo" python3 "$HERE/mock_gemini.py" 18804 "$W/leaf.crt" "$W/leaf.key" &
sleep 1
run_agy antigravity.jsonl 18804
run_agy antigravity-error.jsonl 9

# This machine's paths become the sandbox's.
PI_PKG=$(dirname "$(readlink -f "$PI")")  # the npm package dir (pi's prompt names its docs there)
while [ ! -f "$PI_PKG/package.json" ] && [ "$PI_PKG" != / ]; do PI_PKG=$(dirname "$PI_PKG"); done
for f in "$W"/*.jsonl; do
  sed -e "s#$W/repo#/workspace/repo#g" -e "s#$W/home#/home/agent#g" \
      -e "s#$(echo "$W/repo" | tr -c 'A-Za-z0-9\n' '-')#-workspace-repo#g" \
      -e "s#$PI_PKG#/opt/hangar/harness/pi/lib/lib/node_modules/@earendil-works/pi-coding-agent#g" "$f" > "$OUT/$(basename "$f")"
  echo "$(basename "$f"): $(wc -l < "$f") lines"
done
