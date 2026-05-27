#!/usr/bin/env bash
# uninstall.sh — removes the nb Claude Code skill
#
# Usage:
#   bash uninstall.sh
#   CLAUDE_CONFIG_DIR=/path/to/config bash uninstall.sh

set -euo pipefail

CLAUDE_DIR="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
SKILL_DIR="$CLAUDE_DIR/skills/nb"
SETTINGS="$CLAUDE_DIR/settings.json"
GUARD_CMD="bash $SKILL_DIR/scripts/nb-guard.sh"

if ! command -v jq &>/dev/null; then
    echo "Error: 'jq' is required but not found." >&2
    exit 1
fi

echo "Uninstalling nb skill from $CLAUDE_DIR..."

# ---------------------------------------------------------------------------
# 1. Remove skill files
# ---------------------------------------------------------------------------

if [[ -d "$SKILL_DIR" ]]; then
    rm -rf "$SKILL_DIR"
    echo "  ✓ Removed $SKILL_DIR"
else
    echo "  (skill directory not found — already uninstalled?)"
fi

# ---------------------------------------------------------------------------
# 2. Remove hook from settings.json
# ---------------------------------------------------------------------------

if [[ -f "$SETTINGS" ]]; then
    HOOK_COUNT=$(jq --arg cmd "$GUARD_CMD" '
        [.hooks.PreToolUse // [] | .[].hooks // [] | .[] | select(.command == $cmd)] | length
    ' "$SETTINGS")

    if [[ "$HOOK_COUNT" -gt 0 ]]; then
        jq --arg cmd "$GUARD_CMD" '
            .hooks.PreToolUse //= [] |
            .hooks.PreToolUse |= map(
                .hooks |= map(select(.command != $cmd)) |
                select((.hooks | length) > 0)
            ) |
            # Remove PreToolUse key entirely if now empty
            if (.hooks.PreToolUse | length) == 0
            then del(.hooks.PreToolUse)
            else . end
        ' "$SETTINGS" > "$SETTINGS.nb_tmp" && mv "$SETTINGS.nb_tmp" "$SETTINGS"
        echo "  ✓ Hook removed from $SETTINGS"
    else
        echo "  (hook not found in $SETTINGS — already removed?)"
    fi
else
    echo "  (settings.json not found — skipping)"
fi

echo ""
echo "✓ nb skill uninstalled."
echo "  Restart Claude Code to pick up the change."
