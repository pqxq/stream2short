#!/usr/bin/env bash
# Stream2Short launcher — https://github.com/pqxq/stream2short
# Creates the virtual environment if needed, installs dependencies, then starts
# the Streamlit studio.  Run from the repository root:
#   ./start.sh

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$REPO_DIR/.venv"
PYTHON="${PYTHON:-python3}"

# ---------------------------------------------------------------------------
# 1. Create virtual environment
# ---------------------------------------------------------------------------
if [ ! -f "$VENV_DIR/bin/activate" ]; then
    echo "[stream2short] Creating virtual environment..."
    "$PYTHON" -m venv "$VENV_DIR"
fi

# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"

# ---------------------------------------------------------------------------
# 2. Install / update dependencies
# ---------------------------------------------------------------------------
echo "[stream2short] Installing dependencies..."
pip install --quiet --upgrade pip
pip install --quiet -r "$REPO_DIR/requirements.txt"

# ---------------------------------------------------------------------------
# 3. Launch Streamlit
# ---------------------------------------------------------------------------
echo "[stream2short] Starting Streamlit studio..."
exec streamlit run "$REPO_DIR/app.py" "$@"
