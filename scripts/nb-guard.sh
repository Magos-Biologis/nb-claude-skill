#!/usr/bin/env bash
# nb-guard.sh — PreToolUse hook that blocks direct Read/Edit/Write/MultiEdit
# on .ipynb files and redirects Claude to the nb skill scripts.
#
# Invoked for ALL Read|Edit|Write|MultiEdit operations (no `if` filter in
# settings.json — the script performs its own extension check so that
# subdirectory paths like notebooks/analysis.ipynb are not bypassed by a
# same-directory-only glob).
#
# Exit codes:
#   0 — non-.ipynb target; allow the operation
#   1 — .ipynb target; block and print a redirect message on stdout

set -uo pipefail

PAYLOAD=$(cat)

# Extract tool name — fall back to "unknown" on any jq failure
TOOL=$(printf '%s' "$PAYLOAD" | jq -r '.tool_name // "unknown"' 2>/dev/null) \
    || TOOL="unknown"

# Extract file path.
# MultiEdit uses tool_input.edits[].file_path (array), not tool_input.file_path.
# For MultiEdit we find the first .ipynb path in the edits array; if none exist
# FILE is empty and we exit 0 (allow).
if [ "$TOOL" = "MultiEdit" ]; then
    FILE=$(printf '%s' "$PAYLOAD" | jq -r \
        '(.tool_input.edits // [] | map(select(.file_path | strings | endswith(".ipynb"))) | first | .file_path) // ""' \
        2>/dev/null) || FILE=""
else
    FILE=$(printf '%s' "$PAYLOAD" | jq -r \
        '.tool_input.file_path // .tool_input.path // ""' \
        2>/dev/null) || FILE=""
fi

# Only block .ipynb files.  Exit 0 immediately for everything else.
# Bash [[ glob patterns match across newlines, so *.ipynb catches any path
# whose last characters are ".ipynb", regardless of directory depth.
if [[ "$FILE" != *.ipynb ]]; then
    exit 0
fi

NB_SCRIPTS="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/skills/nb/scripts"

# Sanitize FILE and TOOL before echoing to prevent ANSI/newline injection.
# tr -d '\000-\037\177' strips all C0 control characters (including ESC = \033
# which starts ANSI sequences, and LF/CR which would split lines) and DEL.
SAFE_FILE=$(printf '%s' "$FILE"  | tr -d '\000-\037\177')
SAFE_TOOL=$(printf '%s' "$TOOL"  | LC_ALL=C tr -cd 'A-Za-z')

case "$SAFE_TOOL" in
    Read)
        echo "Blocked: do not use Read on .ipynb files — raw JSON is ~15× more tokens than needed."
        echo "Use the nb skill instead:"
        echo "  python3 \"$NB_SCRIPTS/nb-read.py\" \"$SAFE_FILE\""
        ;;
    Edit|Write|MultiEdit)
        echo "Blocked: do not use $SAFE_TOOL on .ipynb files directly."
        echo "Use the nb skill instead:"
        echo "  python3 \"$NB_SCRIPTS/nb-write.py\" \"$SAFE_FILE\" patch <index> -f /tmp/source.txt"
        ;;
    *)
        echo "Blocked: direct file operations on .ipynb files are not permitted."
        echo "Use the nb skill scripts in: $NB_SCRIPTS"
        ;;
esac

exit 1
