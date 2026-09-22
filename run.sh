#!/usr/bin/env bash
# nanoDoc - start with:  ./run.sh
# Needs Python 3.9+ and nothing else.
set -euo pipefail
cd "$(dirname "$0")"

if command -v python3 >/dev/null 2>&1; then
  exec python3 start.py "$@"
elif command -v python >/dev/null 2>&1; then
  exec python start.py "$@"
else
  printf '\n  Python was not found.\n\n  macOS:  brew install python3\n  Linux:  sudo apt install python3\n\n'
  exit 1
fi
