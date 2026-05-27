#!/usr/bin/env bash
# install.sh — installs the nb Claude Code skill into ~/.claude (or $CLAUDE_CONFIG_DIR)
#
# Usage:
#   bash install.sh           # installs to ~/.claude/skills/nb/
#   CLAUDE_CONFIG_DIR=/path/to/config bash install.sh   # custom config dir
#
# What this does:
#   1. Copies SKILL.md and scripts/ to $CLAUDE_DIR/skills/nb/
#   2. Copies tests/ for post-install verification
#   3. Patches $CLAUDE_DIR/settings.json to add a PreToolUse hook for nb-guard.sh
#      (idempotent — safe to run multiple times / after updates)
#
# Requirements: bash, jq

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLAUDE_DIR="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
SKILL_DIR="$CLAUDE_DIR/skills/nb"
SETTINGS="$CLAUDE_DIR/settings.json"
GUARD_CMD="bash $SKILL_DIR/scripts/nb-guard.sh"

# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------

if ! command -v jq &>/dev/null; then
    echo "Error: 'jq' is required but not found. Install it first." >&2
    echo "  On Arch/Manjaro:  sudo pacman -S jq" >&2
    echo "  On Ubuntu/Debian: sudo apt install jq" >&2
    exit 1
fi

echo "Installing nb skill to $SKILL_DIR..."

# ---------------------------------------------------------------------------
# 1. Copy files
# ---------------------------------------------------------------------------

mkdir -p "$SKILL_DIR/scripts" "$SKILL_DIR/tests"

cp "$REPO_DIR/SKILL.md"                    "$SKILL_DIR/"
cp "$REPO_DIR/scripts/nb-read.py"          "$SKILL_DIR/scripts/"
cp "$REPO_DIR/scripts/nb-write.py"         "$SKILL_DIR/scripts/"
cp "$REPO_DIR/scripts/nb-guard.sh"         "$SKILL_DIR/scripts/"
chmod +x "$SKILL_DIR/scripts/nb-guard.sh"

# Copy tests for post-install verification
cp "$REPO_DIR/tests/"*.py "$SKILL_DIR/tests/" 2>/dev/null || true
cp "$REPO_DIR/tests/"*.md "$SKILL_DIR/tests/" 2>/dev/null || true

echo "  ✓ Files copied"

# ---------------------------------------------------------------------------
# 2. Patch settings.json
# ---------------------------------------------------------------------------

# Create settings.json if it doesn't exist
if [[ ! -f "$SETTINGS" ]]; then
    echo '{}' > "$SETTINGS"
    echo "  ✓ Created $SETTINGS"
fi

# Build the hook entry with the absolute path expanded at install time
HOOK_ENTRY=$(jq -n --arg cmd "$GUARD_CMD" \
    '{"type":"command","command":$cmd}')

# Check if our hook command is already registered (idempotent)
ALREADY_INSTALLED=$(jq --arg cmd "$GUARD_CMD" '
    [.hooks.PreToolUse // [] | .[].hooks // [] | .[] | select(.command == $cmd)] | length
' "$SETTINGS")

if [[ "$ALREADY_INSTALLED" -gt 0 ]]; then
    echo "  ✓ Hook already registered in $SETTINGS (no change needed)"
else
    # Check if there's already a Read|Edit|Write|MultiEdit PreToolUse entry
    HAS_MATCHER=$(jq '
        [.hooks.PreToolUse // [] | .[] | select(.matcher == "Read|Edit|Write|MultiEdit")] | length
    ' "$SETTINGS")

    if [[ "$HAS_MATCHER" -gt 0 ]]; then
        # Append our hook to the existing matcher entry
        jq --argjson hook "$HOOK_ENTRY" '
            .hooks.PreToolUse |= map(
                if .matcher == "Read|Edit|Write|MultiEdit"
                then .hooks += [$hook]
                else . end)
        ' "$SETTINGS" > "$SETTINGS.nb_tmp" && mv "$SETTINGS.nb_tmp" "$SETTINGS"
    else
        # Create a new PreToolUse entry
        jq --argjson hook "$HOOK_ENTRY" '
            .hooks //= {} |
            .hooks.PreToolUse //= [] |
            .hooks.PreToolUse += [{"matcher":"Read|Edit|Write|MultiEdit","hooks":[$hook]}]
        ' "$SETTINGS" > "$SETTINGS.nb_tmp" && mv "$SETTINGS.nb_tmp" "$SETTINGS"
    fi

    echo "  ✓ Hook registered in $SETTINGS"
fi

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------

echo ""
echo "✓ nb skill installed successfully."
echo ""
echo "Next steps:"
echo "  • Restart Claude Code (or reload the session) to pick up the hook."
echo "  • Verify the install:  pytest $SKILL_DIR/tests/ -q"
echo "  • To uninstall:        bash $REPO_DIR/uninstall.sh"
