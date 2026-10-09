#!/usr/bin/env bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Ensure virtual environment exists
if [ ! -d ".venv" ]; then
    echo "Creating virtual environment..."
    export PATH="$HOME/.local/bin:$PATH"
    uv venv --python 3.12 .venv
    source .venv/bin/activate
    uv pip install curl_cffi rich prompt_toolkit pydantic ntplib tzdata
else
    source .venv/bin/activate
fi

exec python3 main.py "$@"
