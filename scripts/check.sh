#!/usr/bin/env bash
# The one-command gate: tests + lint + types. CI runs the same three steps.
set -euo pipefail
cd "$(dirname "$0")/.."

echo "=== pytest ==="
python -m pytest -q

echo "=== ruff ==="
python -m ruff check src tests

echo "=== mypy ==="
python -m mypy src/webwire

echo "=== check: ALL GREEN ==="
