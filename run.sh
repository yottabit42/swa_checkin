#!/usr/bin/env bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Persist Playwright browser binaries inside the workspace so container cache
# resets or Playwright version updates never break scheduled check-ins.
export PLAYWRIGHT_BROWSERS_PATH="${PLAYWRIGHT_BROWSERS_PATH:-$SCRIPT_DIR/.playwright-browsers}"

# Ensure virtual environment exists
if [ ! -d ".venv" ]; then
    echo "Creating virtual environment..."
    export PATH="$HOME/.local/bin:$PATH"
    uv venv --python 3.12 .venv
    source .venv/bin/activate
    uv pip install -r requirements.txt
    python3 -m playwright install chromium
else
    source .venv/bin/activate
fi

exec python3 main.py "$@"
