#!/usr/bin/env python3
"""
install.py — cross-platform installer for the nb Claude Code skill.

Usage:
  python3 install.py                  # Linux / macOS
  python   install.py                 # Windows
  CLAUDE_CONFIG_DIR=/path python3 install.py  # custom config dir

What it does:
  1. Copies SKILL.md, scripts/, and tests/ into <claude_dir>/skills/nb/
  2. Registers nb-guard.py as a PreToolUse hook in settings.json
  3. Replaces any legacy nb-guard.sh hook entries from previous installs
  4. Idempotent: running twice results in exactly one hook entry

Requirements:
  - Python 3.8+
  - No external dependencies (stdlib only)
"""

import json
import os
import shutil
import sys
from pathlib import Path

if sys.version_info < (3, 8):
    sys.exit("Error: nb requires Python 3.8 or later. "
             f"You have Python {sys.version_info.major}.{sys.version_info.minor}.")

REPO_ROOT = Path(__file__).parent.resolve()

from _nb_install_common import (
    _claude_dir, _is_nb_guard_hook, _save_settings, _remove_nb_guard_entries,
)


def _python_cmd() -> str:
    """Return the best Python 3 command for the current platform."""
    if sys.platform == "win32" and shutil.which("py"):
        return "py -3"
    return "python3" if shutil.which("python3") else "python"


def _load_settings(settings_path: Path) -> dict:
    if not settings_path.exists():
        return {}
    try:
        return json.loads(settings_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        print(f"[WARN] Could not parse {settings_path}: {e}. Starting fresh.", file=sys.stderr)
        return {}


def _add_nb_guard_entry(settings: dict, guard_cmd: str) -> None:
    settings.setdefault("hooks", {}).setdefault("PreToolUse", []).append({
        "matcher": "Read|Edit|Write|MultiEdit",
        "hooks": [{
            "type": "command",
            "command": guard_cmd,
        }],
    })


def main():
    claude_dir = _claude_dir()
    skill_dir  = claude_dir / "skills" / "nb"
    py_cmd     = _python_cmd()

    print(f"Installing nb skill to: {skill_dir}")

    # 1. Copy files
    skill_dir.mkdir(parents=True, exist_ok=True)

    shutil.copy2(REPO_ROOT / "SKILL.md", skill_dir / "SKILL.md")

    # install.py / uninstall.py / shared module — self-contained installed copy
    for stem in ("install.py", "uninstall.py", "_nb_install_common.py"):
        src = REPO_ROOT / stem
        if src.exists():
            shutil.copy2(src, skill_dir / stem)

    # scripts/ — copy only .py and .sh files (skip __pycache__ etc.)
    scripts_dst = skill_dir / "scripts"
    scripts_dst.mkdir(exist_ok=True)
    for f in (REPO_ROOT / "scripts").iterdir():
        if f.is_file() and f.suffix in (".py", ".sh"):
            shutil.copy2(f, scripts_dst / f.name)

    # tests/ — dirs_exist_ok=True avoids the TOCTOU window of rmtree+copytree
    tests_dst = skill_dir / "tests"
    shutil.copytree(REPO_ROOT / "tests", tests_dst, dirs_exist_ok=True)

    # Make scripts executable on POSIX
    if sys.platform != "win32":
        for f in scripts_dst.iterdir():
            if f.suffix in (".py", ".sh"):
                f.chmod(f.stat().st_mode | 0o111)

    print("[OK] Files copied")

    # 2. Patch settings.json
    settings_path = claude_dir / "settings.json"
    settings = _load_settings(settings_path)

    _remove_nb_guard_entries(settings)

    guard_script = (scripts_dst / "nb-guard.py").resolve()
    guard_cmd = f'{py_cmd} "{guard_script.as_posix()}"'
    _add_nb_guard_entry(settings, guard_cmd)

    _save_settings(settings_path, settings)
    print(f"[OK] Hook registered in {settings_path}")

    print("[OK] nb skill installed successfully.")
    print()
    tests_path = skill_dir / "tests"
    print("Next steps:")
    print("  1. Restart Claude Code")
    print(f"  2. Verify: {py_cmd} -m pytest \"{tests_path}\"")


if __name__ == "__main__":
    main()
