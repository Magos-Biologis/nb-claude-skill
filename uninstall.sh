#!/usr/bin/env bash
# uninstall.sh — thin POSIX wrapper around uninstall.py
#
# For backward compatibility with POSIX users who run: bash uninstall.sh
# Windows users: run  python uninstall.py  directly.
#
# Usage:
#   bash uninstall.sh
#   CLAUDE_CONFIG_DIR=/path/to/config bash uninstall.sh

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if command -v python3 &>/dev/null; then
    PYTHON=python3
elif command -v python &>/dev/null; then
    PYTHON=python
else
    echo "Error: python3 (or python) is required but not found." >&2
    exit 1
fi

exec "$PYTHON" "$REPO_DIR/uninstall.py" "$@"
