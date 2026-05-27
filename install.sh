#!/usr/bin/env bash
# install.sh — thin POSIX wrapper around install.py
#
# For backward compatibility with POSIX users who run: bash install.sh
# Windows users: run  python install.py  directly (bash is not available).
#
# Usage:
#   bash install.sh
#   CLAUDE_CONFIG_DIR=/path/to/config bash install.sh

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Prefer python3, fall back to python
if command -v python3 &>/dev/null; then
    PYTHON=python3
elif command -v python &>/dev/null; then
    PYTHON=python
else
    echo "Error: python3 (or python) is required but not found." >&2
    echo "Install Python 3.8+ and try again." >&2
    exit 1
fi

exec "$PYTHON" "$REPO_DIR/install.py" "$@"
