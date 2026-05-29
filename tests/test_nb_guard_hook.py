"""
TDD tests for the nb-guard hook.

Two concerns under test:
  1. Script behaviour  — nb-guard.sh receives a JSON payload on stdin and
     must exit 1 with a helpful redirect message for every tool/notebook
     combination it guards.
  2. Settings registration — settings.json must have the PreToolUse entry
     that actually fires the script (post-install verification; skipped if
     the skill has not been installed yet).
"""

import json
import os
import subprocess
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Paths — all relative to this file so the test suite is portable
# ---------------------------------------------------------------------------

REPO_ROOT  = Path(__file__).parent.parent
GUARD      = REPO_ROOT / "scripts" / "nb-guard.sh"
GUARD_PY   = REPO_ROOT / "scripts" / "nb-guard.py"
NB_SCRIPTS = REPO_ROOT / "scripts"

# settings.json lives in the user's Claude config dir — only meaningful
# after `install.sh` has been run.
_claude_dir = Path(os.environ.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude")))
SETTINGS    = _claude_dir / "settings.json"


# ---------------------------------------------------------------------------
# Helper: invoke the guard script with a crafted payload
# ---------------------------------------------------------------------------

def run_guard(tool_name: str, file_path: str = "analysis.ipynb") -> subprocess.CompletedProcess:
    # MultiEdit uses tool_input.edits[].file_path, not tool_input.file_path.
    # Use the correct payload shape so tests match real harness payloads.
    if tool_name == "MultiEdit":
        tool_input = {"edits": [{"file_path": file_path, "old_string": "x", "new_string": "y"}]}
    else:
        tool_input = {"file_path": file_path}
    payload = json.dumps({
        "tool_name":  tool_name,
        "tool_input": tool_input,
        "session_id": "test-session",
    })
    return subprocess.run(
        ["bash", str(GUARD)],
        input=payload,
        capture_output=True,
        text=True,
    )


# ---------------------------------------------------------------------------
# 1. Script behaviour
# ---------------------------------------------------------------------------

class TestGuardScriptExists:

    def test_script_file_exists(self):
        """nb-guard.sh must exist at the expected path."""
        assert GUARD.exists(), f"nb-guard.sh not found at {GUARD}"

    def test_script_is_executable(self):
        """nb-guard.sh must be executable."""
        assert os.access(GUARD, os.X_OK), f"{GUARD} is not executable"


class TestGuardBlocking:

    @pytest.mark.parametrize("tool", ["Read", "Edit", "Write", "MultiEdit"])
    def test_exits_exactly_1_for_ipynb(self, tool):
        """All four guarded tools must exit exactly 1 (not 127/jq-error) for .ipynb files."""
        r = run_guard(tool, "notebook.ipynb")
        assert r.returncode == 1, (
            f"{tool} on .ipynb should exit 1 (blocked), got {r.returncode}\n"
            f"stdout: {r.stdout!r}\nstderr: {r.stderr!r}"
        )

    @pytest.mark.parametrize("tool", ["Read", "Edit", "Write", "MultiEdit"])
    def test_output_contains_blocked(self, tool):
        """Redirect message must include the word 'Blocked' so Claude understands why."""
        r = run_guard(tool)
        assert "Blocked" in r.stdout, (
            f"Expected 'Blocked' in stdout for {tool}, got: {r.stdout!r}"
        )

    def test_read_message_references_nb_read_py(self):
        """Read block message must point Claude to nb-read.py."""
        r = run_guard("Read")
        assert "nb-read.py" in r.stdout, (
            f"Expected 'nb-read.py' in Read block message, got: {r.stdout!r}"
        )

    @pytest.mark.parametrize("tool", ["Edit", "Write", "MultiEdit"])
    def test_write_tools_message_references_nb_write_py(self, tool):
        """Write-family block messages must point Claude to nb-write.py."""
        r = run_guard(tool)
        assert "nb-write.py" in r.stdout, (
            f"Expected 'nb-write.py' in {tool} block message, got: {r.stdout!r}"
        )

    @pytest.mark.parametrize("tool", ["Read", "Edit", "Write", "MultiEdit"])
    def test_message_includes_full_file_path(self, tool):
        """The full blocked file path (including directory) must appear in the message."""
        r = run_guard(tool, "data/my_analysis.ipynb")
        assert "data/my_analysis.ipynb" in r.stdout, (
            f"Expected full path 'data/my_analysis.ipynb' in {tool} block message, "
            f"got: {r.stdout!r}"
        )

    def test_no_raw_traceback_on_stdout(self):
        """A shell error must not produce a raw bash/python traceback on stdout."""
        r = run_guard("Read")
        assert "Traceback" not in r.stdout
        assert "line " not in r.stdout or "nb-read" in r.stdout  # allow "line N" in the command hint


class TestGuardGracefulEdgeCases:

    def test_unknown_tool_exits_nonzero(self):
        """An unrecognised tool name must still exit non-zero, not silently succeed."""
        r = run_guard("UnknownTool", "nb.ipynb")
        assert r.returncode != 0

    def test_missing_file_path_field_does_not_crash(self):
        """A payload missing tool_input.file_path must not crash the script.

        Design: fail open — when we cannot determine the target file we allow
        the operation rather than blocking all file I/O on bad payloads.
        """
        payload = json.dumps({"tool_name": "Read", "tool_input": {}, "session_id": "x"})
        r = subprocess.run(
            ["bash", str(GUARD)],
            input=payload, capture_output=True, text=True,
        )
        # Fail open: exit 0 (allow) when file path is undetermined
        assert r.returncode == 0
        assert "Traceback" not in r.stdout
        assert "Traceback" not in r.stderr

    def test_malformed_json_does_not_crash(self):
        """Completely malformed stdin must not crash the script."""
        r = subprocess.run(
            ["bash", str(GUARD)],
            input="not json at all",
            capture_output=True, text=True,
        )
        assert "Traceback" not in r.stdout

    def test_message_on_stdout_not_stderr(self):
        """
        Hook redirect messages must go to stdout so Claude receives them as
        the blocking reason (the harness surfaces hook stdout to Claude).
        """
        r = run_guard("Read")
        assert r.stdout.strip() != "", "Expected message on stdout, got nothing"


# ---------------------------------------------------------------------------
# 2. Settings.json registration (post-install verification)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(
    not SETTINGS.exists(),
    reason="settings.json not found — run install.sh first",
)
class TestSettingsRegistration:

    @pytest.fixture(scope="class")
    def settings(self):
        return json.loads(SETTINGS.read_text())

    @pytest.fixture(scope="class")
    def pretooluse_hooks(self, settings):
        hooks = settings.get("hooks", {}).get("PreToolUse", [])
        if not hooks:
            pytest.skip("No PreToolUse hooks in settings.json — run install.sh first")
        return hooks

    def _find_entries_for_tool(self, pretooluse_hooks, tool: str):
        """Return all hook entries whose matcher covers `tool`."""
        matches = []
        for entry in pretooluse_hooks:
            matcher = entry.get("matcher", "")
            tools_in_matcher = [t.strip() for t in matcher.split("|")]
            if tool in tools_in_matcher or matcher == tool:
                matches.extend(entry.get("hooks", []))
        return matches

    @pytest.mark.parametrize("tool", ["Read", "Edit", "Write", "MultiEdit"])
    def test_tool_has_pretooluse_entry(self, pretooluse_hooks, tool):
        """Each guarded tool must appear in a PreToolUse matcher."""
        entries = self._find_entries_for_tool(pretooluse_hooks, tool)
        assert entries, f"No PreToolUse hook entry found for tool '{tool}'"

    def test_nb_guard_uses_no_if_conditions(self, pretooluse_hooks):
        """
        The hardened design delegates .ipynb detection to the script itself — no
        harness-level `if` conditions.  Per-tool `if: "Read(*.ipynb)"` globs only
        matched same-directory notebooks and can't filter MultiEdit edits arrays.
        Absence of `if` means the script runs for all file ops (fast exit 0 for
        non-.ipynb targets).
        """
        all_hooks = []
        for entry in pretooluse_hooks:
            all_hooks.extend(entry.get("hooks", []))
        guard_hooks = [h for h in all_hooks
                       if "nb-guard.py" in h.get("command", "")]
        assert guard_hooks, "No hook references nb-guard.py in settings.json"
        for h in guard_hooks:
            assert "if" not in h, (
                f"nb-guard hook must not use an `if` condition (subdirectory bypass risk). "
                f"Found: {h}"
            )

    def test_nb_guard_command_references_py_not_sh(self, pretooluse_hooks):
        """Post install.py, the hook command must reference nb-guard.py, not nb-guard.sh.

        nb-guard.sh is the legacy shell implementation. install.py replaces it with
        nb-guard.py (the cross-platform Python implementation). This test ensures
        install.py ran successfully and updated the hook command.
        """
        all_hooks = []
        for entry in pretooluse_hooks:
            all_hooks.extend(entry.get("hooks", []))
        guard_hooks = [h for h in all_hooks
                       if "nb-guard" in h.get("command", "")]
        assert guard_hooks, "No hook references nb-guard in settings.json"
        for h in guard_hooks:
            cmd = h.get("command", "")
            assert "nb-guard.py" in cmd, (
                f"Hook command must reference nb-guard.py (Python implementation), "
                f"not nb-guard.sh (legacy shell). Found: {cmd!r}. "
                f"Run: python3 install.py to update settings.json."
            )
            assert "nb-guard.sh" not in cmd, (
                f"Legacy nb-guard.sh entry found — install.py should have replaced it. "
                f"Run: python3 install.py to update settings.json."
            )

    @pytest.mark.parametrize("tool", ["Read", "Edit", "Write", "MultiEdit"])
    def test_tool_hook_references_nb_guard_absolute_path(self, pretooluse_hooks, tool):
        """Each guarded tool's hook entry must use a hardcoded absolute path to nb-guard."""
        entries = self._find_entries_for_tool(pretooluse_hooks, tool)
        guard_entries = [e for e in entries
                         if "nb-guard.py" in e.get("command", "")]
        assert guard_entries, (
            f"No hook entry for '{tool}' references nb-guard.py (Python implementation). "
            f"Entries: {entries}"
        )
        for e in guard_entries:
            cmd = e.get("command", "")
            assert "${CLAUDE_CONFIG_DIR}" not in cmd, (
                f"nb-guard command should not rely on shell variable expansion: {cmd!r}"
            )

    def test_existing_posttooluse_hooks_preserved(self, settings):
        """install.sh must not clobber existing PostToolUse hooks."""
        post = settings.get("hooks", {}).get("PostToolUse", [])
        matchers = [e.get("matcher", "") for e in post]
        assert any("Edit" in m for m in matchers), \
            "consensus PostToolUse hook (Edit|Write|MultiEdit) is missing after install"
        assert any("Bash" in m for m in matchers), \
            "pr-review-wait PostToolUse hook (Bash) is missing after install"

    def test_pr_review_wait_hook_has_async_rewake(self, settings):
        """The pr-review-wait PostToolUse hook must retain asyncRewake: true and timeout."""
        post = settings.get("hooks", {}).get("PostToolUse", [])
        bash_entries = []
        for entry in post:
            if "Bash" in entry.get("matcher", ""):
                bash_entries.extend(entry.get("hooks", []))
        pr_hooks = [e for e in bash_entries if "pr-review-wait" in e.get("command", "")]
        assert pr_hooks, "pr-review-wait PostToolUse hook command is missing"
        for h in pr_hooks:
            assert h.get("asyncRewake") is True, \
                f"pr-review-wait hook must have asyncRewake: true, got: {h}"
            assert "timeout" in h, \
                f"pr-review-wait hook must have a timeout field, got: {h}"
