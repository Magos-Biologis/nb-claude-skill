"""
Tests for install.py and uninstall.py — cross-platform installer.

All tests red until install.py / uninstall.py are created.

Tests run against a temporary fake "claude config dir" so they don't
touch the real ~/.claude or settings.json.
"""
from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT   = Path(__file__).parent.parent
INSTALL_PY  = REPO_ROOT / "install.py"
UNINSTALL_PY = REPO_ROOT / "uninstall.py"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def run_install(claude_dir: Path, extra_env: dict | None = None):
    env = os.environ.copy()
    env["CLAUDE_CONFIG_DIR"] = str(claude_dir)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, str(INSTALL_PY)],
        capture_output=True, text=True, env=env,
    )


def run_uninstall(claude_dir: Path):
    env = os.environ.copy()
    env["CLAUDE_CONFIG_DIR"] = str(claude_dir)
    return subprocess.run(
        [sys.executable, str(UNINSTALL_PY)],
        capture_output=True, text=True, env=env,
    )


def read_settings(claude_dir: Path) -> dict:
    return json.loads((claude_dir / "settings.json").read_text())


def find_nb_guard_hooks(settings: dict) -> list:
    """Return all hook entries whose command references nb-guard."""
    hooks = []
    for entry in settings.get("hooks", {}).get("PreToolUse", []):
        for h in entry.get("hooks", []):
            if "nb-guard" in h.get("command", ""):
                hooks.append(h)
    return hooks


# ---------------------------------------------------------------------------
# Prerequisite
# ---------------------------------------------------------------------------

def test_install_py_exists():
    assert INSTALL_PY.exists(), f"install.py not found at {INSTALL_PY}"


def test_uninstall_py_exists():
    assert UNINSTALL_PY.exists(), f"uninstall.py not found at {UNINSTALL_PY}"


# ---------------------------------------------------------------------------
# Basic install
# ---------------------------------------------------------------------------

class TestInstall:

    def test_install_creates_skill_dir(self, tmp_path):
        r = run_install(tmp_path)
        assert r.returncode == 0, f"install.py failed: {r.stderr}"
        skill_dir = tmp_path / "skills" / "nb"
        assert skill_dir.exists(), f"skill dir not created: {skill_dir}"

    def test_install_copies_skill_md(self, tmp_path):
        run_install(tmp_path)
        assert (tmp_path / "skills" / "nb" / "SKILL.md").exists()

    def test_install_copies_scripts(self, tmp_path):
        run_install(tmp_path)
        scripts = tmp_path / "skills" / "nb" / "scripts"
        assert (scripts / "nb-read.py").exists()
        assert (scripts / "nb-write.py").exists()
        assert (scripts / "nb-guard.py").exists()

    def test_install_copies_tests(self, tmp_path):
        run_install(tmp_path)
        tests = tmp_path / "skills" / "nb" / "tests"
        assert tests.exists()
        assert any(tests.glob("test_*.py"))

    def test_install_copies_common_module(self, tmp_path):
        """_nb_install_common.py must be present so installed install/uninstall can import it."""
        run_install(tmp_path)
        assert (tmp_path / "skills" / "nb" / "_nb_install_common.py").exists()

    def test_install_writes_hook_to_settings(self, tmp_path):
        run_install(tmp_path)
        s = read_settings(tmp_path)
        hooks = find_nb_guard_hooks(s)
        assert hooks, "No nb-guard hook found in settings.json"

    def test_hook_uses_nb_guard_py_not_sh(self, tmp_path):
        """install.py must register nb-guard.py (not the legacy .sh)."""
        run_install(tmp_path)
        s = read_settings(tmp_path)
        hooks = find_nb_guard_hooks(s)
        for h in hooks:
            assert "nb-guard.py" in h["command"], (
                f"Hook must use nb-guard.py, got: {h['command']!r}"
            )
            assert "nb-guard.sh" not in h["command"]

    def test_hook_command_uses_absolute_path(self, tmp_path):
        run_install(tmp_path)
        s = read_settings(tmp_path)
        hooks = find_nb_guard_hooks(s)
        for h in hooks:
            cmd = h["command"]
            # Extract the script path — may be quoted, e.g. python3 "/abs/path/nb-guard.py"
            script_part = next((p for p in cmd.split() if "nb-guard.py" in p), None)
            assert script_part, f"No script path in command: {cmd!r}"
            # Strip surrounding quotes before checking absoluteness
            script_path = Path(script_part.strip('"').strip("'"))
            assert script_path.is_absolute(), (
                f"Script path must be absolute: {script_part!r}"
            )

    def test_hook_covers_all_four_tools(self, tmp_path):
        """PreToolUse must cover Read, Edit, Write, and MultiEdit."""
        run_install(tmp_path)
        s = read_settings(tmp_path)
        pre = s.get("hooks", {}).get("PreToolUse", [])
        matchers = " | ".join(e.get("matcher", "") for e in pre)
        for tool in ["Read", "Edit", "Write", "MultiEdit"]:
            assert tool in matchers, (
                f"Tool '{tool}' not covered by any PreToolUse matcher"
            )

    def test_hook_has_no_if_condition(self, tmp_path):
        """nb-guard hook must NOT use an `if` condition (subdirectory bypass risk)."""
        run_install(tmp_path)
        s = read_settings(tmp_path)
        hooks = find_nb_guard_hooks(s)
        for h in hooks:
            assert "if" not in h, (
                f"nb-guard hook must not use an `if` condition: {h}"
            )

    def test_install_creates_settings_if_missing(self, tmp_path):
        """If settings.json doesn't exist, install.py must create it."""
        assert not (tmp_path / "settings.json").exists()
        run_install(tmp_path)
        assert (tmp_path / "settings.json").exists()

    @pytest.mark.skipif(platform.system() == "Windows",
                        reason="chmod 600 check not applicable on Windows")
    def test_created_settings_has_restricted_permissions(self, tmp_path):
        """settings.json created by install.py must have mode 0o600."""
        run_install(tmp_path)
        mode = (tmp_path / "settings.json").stat().st_mode & 0o777
        assert mode == 0o600, f"Expected 0o600, got 0o{mode:o}"

    def test_install_preserves_existing_hooks(self, tmp_path):
        """install.py must not delete existing PostToolUse hooks in settings.json."""
        existing = {
            "hooks": {
                "PostToolUse": [
                    {"matcher": "Edit|Write", "hooks": [
                        {"type": "command", "command": "echo existing-hook"}
                    ]}
                ]
            }
        }
        (tmp_path / "settings.json").write_text(json.dumps(existing))
        run_install(tmp_path)
        s = read_settings(tmp_path)
        post = s.get("hooks", {}).get("PostToolUse", [])
        commands = [h["command"] for e in post for h in e.get("hooks", [])]
        assert any("existing-hook" in c for c in commands), (
            "install.py must preserve existing PostToolUse hooks"
        )


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------

class TestInstallIdempotency:

    def test_install_twice_produces_one_hook_entry(self, tmp_path):
        """Running install.py twice must not create duplicate hook entries."""
        run_install(tmp_path)
        run_install(tmp_path)
        s = read_settings(tmp_path)
        hooks = find_nb_guard_hooks(s)
        assert len(hooks) == 1, (
            f"Expected exactly 1 nb-guard hook, got {len(hooks)}: {hooks}"
        )

    def test_install_removes_legacy_sh_entry(self, tmp_path):
        """If settings.json has a stale nb-guard.sh entry, install.py must replace it."""
        stale = {
            "hooks": {
                "PreToolUse": [{
                    "matcher": "Read|Edit|Write|MultiEdit",
                    "hooks": [{
                        "type": "command",
                        "command": f"bash {tmp_path}/skills/nb/scripts/nb-guard.sh"
                    }]
                }]
            }
        }
        (tmp_path / "settings.json").write_text(json.dumps(stale))
        run_install(tmp_path)
        s = read_settings(tmp_path)
        # Old .sh entry must be gone
        all_cmds = [
            h["command"]
            for e in s.get("hooks", {}).get("PreToolUse", [])
            for h in e.get("hooks", [])
        ]
        sh_entries = [c for c in all_cmds if "nb-guard.sh" in c]
        assert not sh_entries, f"Stale .sh entries remain: {sh_entries}"
        # New .py entry must be present
        py_entries = [c for c in all_cmds if "nb-guard.py" in c]
        assert py_entries, "New .py entry not found after upgrade"

    def test_temp_file_not_left_on_disk_after_install(self, tmp_path):
        """install.py must not leave any .nb_tmp temp files behind."""
        run_install(tmp_path)
        tmp_files = list(tmp_path.rglob("*.nb_tmp"))
        assert not tmp_files, f"Orphaned temp files: {tmp_files}"


# ---------------------------------------------------------------------------
# Uninstall
# ---------------------------------------------------------------------------

class TestUninstall:

    def test_uninstall_removes_skill_dir(self, tmp_path):
        run_install(tmp_path)
        assert (tmp_path / "skills" / "nb").exists()
        r = run_uninstall(tmp_path)
        assert r.returncode == 0, f"uninstall.py failed: {r.stderr}"
        assert not (tmp_path / "skills" / "nb").exists()

    def test_uninstall_removes_hook_from_settings(self, tmp_path):
        run_install(tmp_path)
        run_uninstall(tmp_path)
        s = read_settings(tmp_path)
        hooks = find_nb_guard_hooks(s)
        assert not hooks, f"Hook not removed after uninstall: {hooks}"

    def test_uninstall_preserves_other_hooks(self, tmp_path):
        """Uninstall must not delete unrelated hooks."""
        run_install(tmp_path)
        # Add an unrelated hook manually
        s = read_settings(tmp_path)
        s.setdefault("hooks", {}).setdefault("PostToolUse", []).append({
            "matcher": "Bash",
            "hooks": [{"type": "command", "command": "echo keep-me"}]
        })
        (tmp_path / "settings.json").write_text(json.dumps(s))
        run_uninstall(tmp_path)
        s2 = read_settings(tmp_path)
        post = s2.get("hooks", {}).get("PostToolUse", [])
        cmds = [h["command"] for e in post for h in e.get("hooks", [])]
        assert any("keep-me" in c for c in cmds), "Uninstall must not remove unrelated hooks"

    def test_uninstall_also_removes_legacy_sh_entry(self, tmp_path):
        """Uninstall must clean up any stale nb-guard.sh entries too."""
        run_install(tmp_path)
        s = read_settings(tmp_path)
        # Manually inject a legacy .sh entry
        s.setdefault("hooks", {}).setdefault("PreToolUse", []).append({
            "matcher": "Read",
            "hooks": [{"type": "command",
                       "command": "bash /fake/nb-guard.sh"}]
        })
        (tmp_path / "settings.json").write_text(json.dumps(s))
        run_uninstall(tmp_path)
        s2 = read_settings(tmp_path)
        all_cmds = [
            h["command"]
            for e in s2.get("hooks", {}).get("PreToolUse", [])
            for h in e.get("hooks", [])
        ]
        assert not any("nb-guard" in c for c in all_cmds), (
            f"nb-guard entries remain after uninstall: {all_cmds}"
        )
