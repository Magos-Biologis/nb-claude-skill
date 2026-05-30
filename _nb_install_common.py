#!/usr/bin/env python3
"""
_nb_install_common.py — shared utilities for install.py and uninstall.py.

Not intended for direct execution. Import only.
"""

import json
import os
import sys
import tempfile
from pathlib import Path


def _default_claude_dir() -> Path:
    return Path.home() / ".claude"


def _claude_dir() -> Path:
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    if env:
        return Path(env).resolve()
    return _default_claude_dir()


def _is_nb_guard_hook(cmd: str) -> bool:
    return "nb-guard.py" in cmd or "nb-guard.sh" in cmd


def _save_settings(settings_path: Path, data: dict) -> None:
    """Atomic write to settings.json with restricted permissions."""
    dir_ = settings_path.parent
    dir_.mkdir(parents=True, exist_ok=True)
    fd, tmp_path_str = tempfile.mkstemp(dir=dir_, suffix=".nb_tmp")
    try:
        try:
            f = os.fdopen(fd, "w", encoding="utf-8")
        except Exception:
            os.close(fd)
            raise
        with f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path_str, settings_path)
    except Exception as e:
        Path(tmp_path_str).unlink(missing_ok=True)
        sys.exit(f"Error: cannot write {settings_path}: {e}")

    if sys.platform != "win32":
        try:
            os.chmod(settings_path, 0o600)
        except OSError:
            pass  # best-effort


def _remove_nb_guard_entries(settings: dict) -> int:
    """Remove all nb-guard hook entries from settings in-place. Returns count removed."""
    pre = settings.get("hooks", {}).get("PreToolUse", [])
    new_pre = []
    removed = 0
    for entry in pre:
        original = entry.get("hooks", [])
        filtered = [h for h in original if not _is_nb_guard_hook(h.get("command", ""))]
        removed += len(original) - len(filtered)
        if filtered:
            entry = dict(entry)
            entry["hooks"] = filtered
            new_pre.append(entry)
    hooks = settings.setdefault("hooks", {})
    if new_pre:
        hooks["PreToolUse"] = new_pre
    else:
        hooks.pop("PreToolUse", None)
    return removed
