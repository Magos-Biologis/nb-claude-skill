#!/usr/bin/env python3
"""
uninstall.py — remove the nb Claude Code skill.

Usage:
  python3 uninstall.py                  # Linux / macOS
  python   uninstall.py                 # Windows
  CLAUDE_CONFIG_DIR=/path python3 uninstall.py

What it does:
  1. Removes <claude_dir>/skills/nb/
  2. Removes any nb-guard hook entries (both nb-guard.py and nb-guard.sh)
     from settings.json
  3. Preserves all other hooks and settings
"""

import json
import os
import shutil
import sys
from pathlib import Path

if sys.version_info < (3, 8):
    sys.exit("Error: nb requires Python 3.8 or later.")

REPO_ROOT = Path(__file__).parent.resolve()

from _nb_install_common import (
    _claude_dir, _is_nb_guard_hook, _save_settings, _remove_nb_guard_entries,
)


def main():
    claude_dir = _claude_dir()
    skill_dir  = claude_dir / "skills" / "nb"
    settings_path = claude_dir / "settings.json"

    # 1. Remove skill directory
    if skill_dir.exists():
        shutil.rmtree(skill_dir)
        print(f"[OK] Removed {skill_dir}")
    else:
        print(f"[INFO] {skill_dir} not found (already removed?)")

    # 2. Patch settings.json
    if not settings_path.exists():
        print("[INFO] settings.json not found — nothing to patch")
        print("[OK] nb skill uninstalled.")
        return

    try:
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        print(f"[WARN] Cannot parse {settings_path}: {e}. Skipping hook removal.",
              file=sys.stderr)
        return

    removed = _remove_nb_guard_entries(settings)
    _save_settings(settings_path, settings)

    if removed:
        print(f"[OK] Removed {removed} nb-guard hook entry(ies) from {settings_path}")
    else:
        print(f"[INFO] No nb-guard hook entries found in {settings_path}")

    print("[OK] nb skill uninstalled.")
    print("Restart Claude Code for the change to take effect.")


if __name__ == "__main__":
    main()
