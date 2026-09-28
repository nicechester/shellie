#!/usr/bin/env bash
# Probe Gemini 3.8 Flash rate limits and capture 429 response bodies.
# Fires requests in a tight loop until 429, saves response, cools 65s, repeats until RPD hit.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
API_KEY=$(grep -E '^GEMINI_API_KEY=' "${REPO_ROOT}/.env" | cut -d'=' -f2-)
MODEL="gemini-3-flash-preview"
URL="https://generativelanguage.googleapis.com/v1beta/models/${MODEL}:generateContent"
OUT_DIR="/tmp/probe_429"
mkdir -p "$OUT_DIR"

PAYLOAD='{"contents":[{"role":"user","parts":[{"text":"Say hi."}]}]}'

burst=0
req=0

while true; do
  req=$((req + 1))
  out_file="${OUT_DIR}/req${req}.json"

  http_status=$(curl -s -o "$out_file" -w "%{http_code}" \
    -X POST "$URL" \
    -H "x-goog-api-key: ${API_KEY}" \
    -H "Content-Type: application/json" \
    -d "$PAYLOAD")

  echo "req ${req}: HTTP ${http_status}"

  if [[ "$http_status" == "429" ]]; then
    burst=$((burst + 1))
    echo ">>> 429 hit (burst #${burst}) — saved to ${out_file}"
    cat "$out_file"
    echo ""

    if grep -qi "PerDay" "$out_file" 2>/dev/null; then
      echo "=== RPD limit reached. Done. ==="
      break
    fi

    echo "--- Cooling down 65s ---"
    sleep 65
    echo "--- Resuming ---"
  fi
done

echo ""
echo "Captured responses in: ${OUT_DIR}"
