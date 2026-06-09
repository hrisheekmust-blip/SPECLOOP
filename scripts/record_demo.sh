#!/usr/bin/env bash
# Demo driver for the hero GIF. Run it once normally to rehearse, then record:
#
#   asciinema rec --cols 100 --rows 30 -c "bash scripts/record_demo.sh" demo.cast
#   mkdir -p docs && agg --font-size 16 --speed 1.0 demo.cast docs/demo.gif
#
# (agg: https://github.com/asciinema/agg — `cargo install --git` or release binary.)
# Total runtime ~60s: one real planner call + two sby proof runs per request.
set -euo pipefail
cd "$(dirname "$0")/.."
[ -f .venv/bin/activate ] && source .venv/bin/activate
export PATH="$PWD/oss-cad-suite/bin:$PATH"

if [ -z "${ANTHROPIC_API_KEY:-}" ]; then
  echo "ANTHROPIC_API_KEY not set — the planner stage needs it." >&2
  exit 1
fi

type_cmd() {   # echo a prompt + command, pause so it is readable on the GIF
  echo
  printf '\033[1;32m$\033[0m %s\n' "$*"
  sleep 2.5
}

clear
echo "# SpecLoop — natural-language request -> composition of formally-proven RTL blocks"
echo "# every block carries machine-checked SVA proofs; the composition itself is"
echo "# proven by assume-guarantee, with a mandatory anti-vacuity check."
sleep 4

type_cmd python -m specloop.compose.e2e '"register, buffer, normalize frame length, and rate-limit an 8-bit stream"'
python -m specloop.compose.e2e "register, buffer, normalize frame length, and rate-limit an 8-bit stream"
sleep 5

type_cmd head -28 work/composition.sv
head -28 work/composition.sv
sleep 5

echo
echo "# requests outside the proven library fail honestly — no hallucinated RTL:"
type_cmd python -m specloop.compose.e2e '"encrypt the stream with AES-256"'
python -m specloop.compose.e2e "encrypt the stream with AES-256" || true
sleep 5
