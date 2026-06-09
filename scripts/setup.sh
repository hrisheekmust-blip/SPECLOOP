#!/usr/bin/env bash
# SpecLoop setup: clean clone -> running the formally-proven composition demo.
#
# Tested on Ubuntu 24.04 (WSL2), Python 3.12.3. Footprint after setup is large:
# ~3 GB Python deps (sentence-transformers pulls torch) + ~2.6 GB OSS CAD Suite.
set -euo pipefail
cd "$(dirname "$0")/.."

# ── 1. Toolchain pin ─────────────────────────────────────────────────────────
# The formal results in this repo were produced with the OSS CAD Suite build
# below (Yosys 0.48+77, git eac2294ca). Yosys versions are NOT interchangeable
# here: this project's central finding is front-end behavior (the read_verilog
# front end silently ignores SystemVerilog `bind`), and the test suite
# re-verifies that exact behavior on whatever build you install.
OSS_CAD_TAG="2025-01-14"
OSS_CAD_FILE="oss-cad-suite-linux-x64-20250114.tgz"
OSS_CAD_URL="https://github.com/YosysHQ/oss-cad-suite-build/releases/download/${OSS_CAD_TAG}/${OSS_CAD_FILE}"

# ── 2. Python environment ────────────────────────────────────────────────────
python3 -c 'import sys; assert sys.version_info >= (3, 11), "Python >= 3.11 required"'
if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
pip install --upgrade pip >/dev/null
pip install -e .

# ── 3. Proven-block RTL corpus (submodule) ───────────────────────────────────
git submodule update --init corpus/verilog-axis

# ── 4. Formal toolchain (yosys + SymbiYosys) ─────────────────────────────────
if [ ! -x oss-cad-suite/bin/sby ]; then
  echo "Downloading OSS CAD Suite ${OSS_CAD_TAG} (~640 MB)..."
  curl -L -o oss-cad-suite.tgz "${OSS_CAD_URL}"
  tar -xzf oss-cad-suite.tgz
fi
export PATH="$PWD/oss-cad-suite/bin:$PATH"
yosys --version
sby --help >/dev/null 2>&1 || true
echo "sby: $(command -v sby)"

# ── 5. Verify the install: deterministic test suites ────────────────────────
# No API key needed. Includes the toolchain-soundness guard (sby must check
# inlined assertions and is expected to silently ignore `bind` — the exact bug
# this project's library rebuild was about) and the Chain A assume-guarantee
# composition proof with its anti-vacuity check.
PYTHONPATH=src python3 tests/test_improvements.py
PYTHONPATH=src python3 tests/test_retrieval.py
PYTHONPATH=src python3 tests/test_planner.py

cat <<'EOF'

Setup complete. Run the end-to-end demo (makes one Anthropic API call):

  source .venv/bin/activate
  export PATH="$PWD/oss-cad-suite/bin:$PATH"
  export ANTHROPIC_API_KEY=...
  python -m specloop.compose.e2e "register, buffer, normalize frame length, and rate-limit an 8-bit stream"

Optional — semantic search over a module library (not bootstrapped in this
repo): requires Docker (docker compose up -d for Qdrant on :6333) and an index
built by running `specloop spec <module.v>` + `specloop index <module>` on your
own RTL, which calls the configured LLM. See README.md.
EOF
