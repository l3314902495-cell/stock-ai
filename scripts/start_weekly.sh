#!/usr/bin/env bash
# Run from any directory; PYTHON may point to the project virtual environment.
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec "${PYTHON:-python}" "$SCRIPT_DIR/weekly.py" start "$@"
