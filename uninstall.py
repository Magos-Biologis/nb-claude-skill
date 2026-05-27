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


def _default_claude_dir() -> Path:
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        if not appdata:
            sys.exit("Error: %APPDATA% not set.")
        return Path(appdata) / "Claude"
    return Path.home() / ".claude"


def _claude_dir() -> Path:
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    if env:
        return Path(env).resolve()
    return _default_claude_dir()


def _is_nb_guard_hook(cmd: str) -> bool:
    return "nb-guard.py" in cmd or "nb-guard.sh" in cmd


def _save_settings(settings_path: Path, data: dict) -> None:
    import tempfile
    dir_ = settings_path.parent
    dir_.mkdir(parents=True, exist_ok=True)
    tmp_path = None
    try:
        fd, tmp_path_str = tempfile.mkstemp(dir=dir_, suffix=".nb_tmp")
        tmp_path = Path(tmp_path_str)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
        except Exception:
            tmp_path.unlink(missing_ok=True)
            tmp_path = None
            raise
        os.replace(tmp_path_str, settings_path)
        tmp_path = None
    except OSError as e:
        if tmp_path:
            tmp_path.unlink(missing_ok=True)
        sys.exit(f"Error: cannot write {settings_path}: {e}")

    if sys.platform != "win32":
        try:
            os.chmod(settings_path, 0o600)
        except OSError:
            pass


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

    # Remove all nb-guard entries (both .py and legacy .sh)
    pre = settings.get("hooks", {}).get("PreToolUse", [])
    new_pre = []
    removed = 0
    for entry in pre:
        filtered = [h for h in entry.get("hooks", [])
                    if not _is_nb_guard_hook(h.get("command", ""))]
        removed += len(entry.get("hooks", [])) - len(filtered)
        if filtered:
            entry = dict(entry)
            entry["hooks"] = filtered
            new_pre.append(entry)
    hooks = settings.setdefault("hooks", {})
    if new_pre:
        hooks["PreToolUse"] = new_pre
    else:
        hooks.pop("PreToolUse", None)

    _save_settings(settings_path, settings)

    if removed:
        print(f"[OK] Removed {removed} nb-guard hook entry(ies) from {settings_path}")
    else:
        print(f"[INFO] No nb-guard hook entries found in {settings_path}")

    print("[OK] nb skill uninstalled.")
    print("Restart Claude Code for the change to take effect.")


if __name__ == "__main__":
    main()
