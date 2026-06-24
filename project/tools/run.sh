#!/bin/bash
# Run a Python tool inside the project venv
set -e
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck disable=SC1091
source "$PROJECT_ROOT/tools/venv/bin/activate"
python3 "$@"
