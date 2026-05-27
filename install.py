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
import platform
import shutil
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Python version check
# ---------------------------------------------------------------------------

if sys.version_info < (3, 8):
    sys.exit("Error: nb requires Python 3.8 or later. "
             f"You have Python {sys.version_info.major}.{sys.version_info.minor}.")

REPO_ROOT = Path(__file__).parent.resolve()


# ---------------------------------------------------------------------------
# Config dir detection
# ---------------------------------------------------------------------------

def _default_claude_dir() -> Path:
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        if not appdata:
            sys.exit("Error: %APPDATA% not set. Cannot determine Claude config dir.")
        return Path(appdata) / "Claude"
    return Path.home() / ".claude"


def _claude_dir() -> Path:
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    if env:
        return Path(env).resolve()
    return _default_claude_dir()


# ---------------------------------------------------------------------------
# Python command detection
# ---------------------------------------------------------------------------

def _python_cmd() -> str:
    """Return 'python3' or 'python' depending on what's on PATH."""
    return "python3" if shutil.which("python3") else "python"


# ---------------------------------------------------------------------------
# Settings.json helpers
# ---------------------------------------------------------------------------

def _load_settings(settings_path: Path) -> dict:
    if not settings_path.exists():
        return {}
    try:
        return json.loads(settings_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        print(f"[WARN] Could not parse {settings_path}: {e}. Starting fresh.", file=sys.stderr)
        return {}


def _save_settings(settings_path: Path, data: dict) -> None:
    """Atomic write to settings.json with restricted permissions."""
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

    # Set permissions to 0o600 on POSIX
    if sys.platform != "win32":
        try:
            os.chmod(settings_path, 0o600)
        except OSError:
            pass  # best-effort


def _is_nb_guard_hook(cmd: str) -> bool:
    return "nb-guard.py" in cmd or "nb-guard.sh" in cmd


def _remove_nb_guard_entries(settings: dict) -> None:
    """Remove all nb-guard hook entries (both .py and .sh) from settings."""
    pre = settings.get("hooks", {}).get("PreToolUse", [])
    new_pre = []
    for entry in pre:
        filtered_hooks = [h for h in entry.get("hooks", [])
                          if not _is_nb_guard_hook(h.get("command", ""))]
        if filtered_hooks:
            entry = dict(entry)
            entry["hooks"] = filtered_hooks
            new_pre.append(entry)
        # If all hooks in this entry were nb-guard, drop the whole entry

    hooks = settings.setdefault("hooks", {})
    if new_pre:
        hooks["PreToolUse"] = new_pre
    else:
        # Drop the key entirely so we don't leave an empty PreToolUse list
        hooks.pop("PreToolUse", None)


def _add_nb_guard_entry(settings: dict, guard_cmd: str) -> None:
    """Add the nb-guard.py PreToolUse entry."""
    settings.setdefault("hooks", {}).setdefault("PreToolUse", []).append({
        "matcher": "Read|Edit|Write|MultiEdit",
        "hooks": [{
            "type": "command",
            "command": guard_cmd,
        }],
    })


# ---------------------------------------------------------------------------
# Main install logic
# ---------------------------------------------------------------------------

def main():
    claude_dir = _claude_dir()
    skill_dir  = claude_dir / "skills" / "nb"
    py_cmd     = _python_cmd()

    print(f"Installing nb skill to: {skill_dir}")

    # 1. Copy files
    skill_dir.mkdir(parents=True, exist_ok=True)

    # SKILL.md
    shutil.copy2(REPO_ROOT / "SKILL.md", skill_dir / "SKILL.md")

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

    # Remove all existing nb-guard entries (idempotent + legacy .sh cleanup)
    _remove_nb_guard_entries(settings)

    # Build the hook command with an absolute path to the installed guard
    guard_script = (scripts_dst / "nb-guard.py").resolve()
    guard_cmd = f'{py_cmd} "{guard_script}"'
    _add_nb_guard_entry(settings, guard_cmd)

    _save_settings(settings_path, settings)
    print(f"[OK] Hook registered in {settings_path}")

    print("[OK] nb skill installed successfully.")
    print()
    print("Next steps:")
    print("  1. Restart Claude Code")
    print(f"  2. Verify: {py_cmd} {skill_dir}/tests/test_scripts.py (or run pytest)")


if __name__ == "__main__":
    main()
