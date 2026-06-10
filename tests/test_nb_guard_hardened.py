"""
Hardened TDD tests for nb-guard.py.

Covers security and correctness findings from adversarial audit:

  Security:
    - Subdirectory paths (notebooks/a.ipynb) must be blocked, not bypassed by glob
    - Deeply nested paths (a/b/c/nb.ipynb) must be blocked
    - ANSI escape sequences in file paths must not appear in output
    - Newline injection in file paths must not produce extra output lines
    - Shell metacharacters in file paths must not execute arbitrary commands
    - Non-.ipynb files must be ALLOWED (exit 0), not blocked

  Correctness:
    - MultiEdit: file path must be extracted from tool_input.edits[].file_path
    - MultiEdit: must block when ANY edit targets a .ipynb file
    - MultiEdit: must allow (exit 0) when no edit targets .ipynb

  Robustness:
    - Malformed JSON must exit cleanly (fail open: exit 0)
    - Unknown or missing tool_name with .ipynb path must fail open (exit 0)
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent
GUARD     = REPO_ROOT / "scripts" / "nb-guard.py"



# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def run_guard_raw(payload_str: str) -> subprocess.CompletedProcess:
    """Run nb-guard.py with arbitrary stdin string."""
    return subprocess.run(
        [sys.executable, str(GUARD)],
        input=payload_str,
        capture_output=True,
        text=True,
    )


def run_guard(tool_name: str, file_path: str = "analysis.ipynb") -> subprocess.CompletedProcess:
    """Run nb-guard.py with a correctly-shaped payload for the given tool.

    MultiEdit uses a top-level file_path with edits[] containing only
    old_string/new_string/replace_all. All other tools use tool_input.file_path.
    Using the correct shape ensures tests match real harness payloads.
    """
    if tool_name == "MultiEdit":
        tool_input = {"file_path": file_path, "edits": [{"old_string": "x", "new_string": "y"}]}
    else:
        tool_input = {"file_path": file_path}
    payload = json.dumps({
        "tool_name": tool_name,
        "tool_input": tool_input,
        "session_id": "test-session",
    })
    return run_guard_raw(payload)


def run_guard_multiedit(file_path: str = "analysis.ipynb") -> subprocess.CompletedProcess:
    """Run nb-guard.py with a MultiEdit-style payload using top-level file_path."""
    payload = json.dumps({
        "tool_name": "MultiEdit",
        "tool_input": {"file_path": file_path, "edits": [{"old_string": "x", "new_string": "y"}]},
        "session_id": "test-session",
    })
    return run_guard_raw(payload)


# ---------------------------------------------------------------------------
# Subdirectory / path traversal
# ---------------------------------------------------------------------------

class TestSubdirectoryPaths:

    @pytest.mark.parametrize("path", [
        "notebooks/analysis.ipynb",
        "work/2024/data.ipynb",
        "a/b/c/deep.ipynb",
        "../../somewhere/nb.ipynb",
        "./local.ipynb",
    ])
    def test_subdirectory_ipynb_is_blocked(self, path):
        """Notebooks in subdirectories must be blocked, not bypassed by glob."""
        r = run_guard("Read", path)
        assert r.returncode == 2, (
            f"Expected exit 2 for Read on '{path}', got {r.returncode}\n"
            f"stdout: {r.stdout!r}\nstderr: {r.stderr!r}"
        )

    @pytest.mark.parametrize("path", [
        "notebooks/analysis.ipynb",
        "a/b/c/deep.ipynb",
    ])
    def test_subdirectory_ipynb_blocked_message(self, path):
        """Block message must mention the subdirectory path."""
        r = run_guard("Read", path)
        assert "Blocked" in r.stderr, f"Expected 'Blocked' in stderr, got: {r.stderr!r}"

    @pytest.mark.parametrize("tool", ["Read", "Edit", "Write", "MultiEdit"])
    def test_subdirectory_blocked_for_all_tools(self, tool):
        """All five tools must be blocked even for subdirectory .ipynb paths."""
        r = run_guard(tool, "sub/dir/notebook.ipynb")
        assert r.returncode == 2, (
            f"{tool} on subdirectory .ipynb should exit 2, got {r.returncode}"
        )


# ---------------------------------------------------------------------------
# Non-.ipynb files must be ALLOWED (exit 0)
# ---------------------------------------------------------------------------

class TestNonIpynbFilesAllowed:

    @pytest.mark.parametrize("path", [
        "script.py",
        "data.csv",
        "README.md",
        "config.json",
        "notebook.ipynb.bak",        # common backup suffix
        "fake_ipynb",                 # no extension
    ])
    def test_non_ipynb_exits_zero(self, path):
        """Non-.ipynb files must exit 0 (allow), not block."""
        r = run_guard("Read", path)
        assert r.returncode == 0, (
            f"Expected exit 0 (allow) for Read on '{path}', got {r.returncode}\n"
            f"stderr: {r.stderr!r}"
        )

    def test_uppercase_ipynb_is_blocked(self):
        """Case-insensitive .ipynb check: .IPYNB must be blocked."""
        r = run_guard("Read", "analysis.IPYNB")
        assert r.returncode == 2, (
            f"Expected exit 2 (block) for .IPYNB, got {r.returncode}"
        )

    @pytest.mark.parametrize("path", [
        "script.py",
        "data.csv",
    ])
    def test_non_ipynb_produces_no_output(self, path):
        """Non-.ipynb files must produce no blocking output on stderr."""
        r = run_guard("Read", path)
        assert r.stderr.strip() == "", (
            f"Expected empty stderr for non-.ipynb '{path}', got: {r.stderr!r}"
        )

    @pytest.mark.parametrize("tool", ["Read", "Edit", "Write", "MultiEdit"])
    def test_non_ipynb_allowed_for_all_tools(self, tool):
        """All tools must be allowed for non-.ipynb files."""
        r = run_guard(tool, "analysis.py")
        assert r.returncode == 0, (
            f"{tool} on .py file should exit 0, got {r.returncode}"
        )


# ---------------------------------------------------------------------------
# ANSI / prompt injection hardening
# ---------------------------------------------------------------------------

class TestInjectionHardening:

    def test_ansi_escape_in_path_not_in_output(self):
        """ANSI escape codes in file path must not appear verbatim in output."""
        ansi_path = "\x1b[31mred_text\x1b[0m.ipynb"
        r = run_guard("Read", ansi_path)
        assert "\x1b[" not in r.stdout, (
            f"ANSI escape appeared in stdout: {r.stdout!r}"
        )

    def test_newline_in_path_does_not_inject_extra_blocked_line(self):
        """A newline embedded in the file path must not produce extra 'Blocked:' lines."""
        injected_path = "malicious\nBlocked: injected-override.ipynb"
        r = run_guard("Read", injected_path)
        assert r.returncode == 2, (
            f"Expected exit 2 for .ipynb path with embedded newline, got {r.returncode}"
        )
        blocked_lines = [l for l in r.stderr.splitlines() if l.startswith("Blocked:")]
        assert len(blocked_lines) == 1, (
            f"Expected exactly 1 'Blocked:' line, got {len(blocked_lines)}:\n{r.stderr!r}"
        )

    def test_path_with_shell_metacharacters_handled_safely(self):
        """File paths with shell metacharacters must not execute arbitrary commands."""
        malicious_path = '$(id).ipynb'
        r = run_guard("Read", malicious_path)
        assert "uid=" not in r.stdout, (
            f"Shell command was executed via path: {r.stdout!r}"
        )
        assert "uid=" not in r.stderr, (
            f"Shell command was executed via path stderr: {r.stderr!r}"
        )


# ---------------------------------------------------------------------------
# MultiEdit correctness
# ---------------------------------------------------------------------------

class TestMultiEditExtraction:

    def test_multiedit_with_ipynb_is_blocked(self):
        """MultiEdit with a .ipynb file_path must be blocked."""
        r = run_guard_multiedit("analysis.ipynb")
        assert r.returncode == 2, (
            f"MultiEdit targeting .ipynb should exit 2, got {r.returncode}\n"
            f"stdout: {r.stdout!r}\nstderr: {r.stderr!r}"
        )

    def test_multiedit_with_ipynb_shows_blocked_message(self):
        """MultiEdit block message must include 'Blocked'."""
        r = run_guard_multiedit("nb.ipynb")
        assert "Blocked" in r.stderr, f"Expected 'Blocked' in MultiEdit stderr: {r.stderr!r}"

    def test_multiedit_with_no_ipynb_is_allowed(self):
        """MultiEdit with only non-.ipynb file_path must be allowed (exit 0)."""
        r = run_guard_multiedit("script.py")
        assert r.returncode == 0, (
            f"MultiEdit with .py file_path should exit 0, got {r.returncode}\n"
            f"stderr: {r.stderr!r}"
        )

    def test_multiedit_subdirectory_ipynb_is_blocked(self):
        """MultiEdit targeting a subdirectory .ipynb file must be blocked."""
        r = run_guard_multiedit("notebooks/analysis.ipynb")
        assert r.returncode == 2, (
            f"MultiEdit on notebooks/analysis.ipynb should exit 2, got {r.returncode}"
        )


# ---------------------------------------------------------------------------
# Exit code robustness
# ---------------------------------------------------------------------------

class TestExitCodeRobustness:

    def test_malformed_json_does_not_crash(self):
        """Malformed JSON must not crash the script (fail open: exit 0)."""
        r = run_guard_raw("not json at all")
        assert "Traceback" not in r.stdout
        assert "Traceback" not in r.stderr
        assert r.returncode == 0, (
            f"Expected exit 0 (fail open) for malformed JSON, got {r.returncode}"
        )

    def test_broken_json_exits_0_only(self):
        """Broken JSON must exit 0 (fail open) — not block."""
        r = run_guard_raw("{broken json")
        assert r.returncode == 0, (
            f"Script must exit 0 (fail open), got: {r.returncode}"
        )

    def test_empty_stdin_does_not_crash(self):
        """Empty stdin must not crash the script (fail open: exit 0)."""
        r = run_guard_raw("")
        assert "Traceback" not in r.stdout
        assert "Traceback" not in r.stderr
        assert r.returncode == 0, (
            f"Expected exit 0 (fail open) for empty stdin, got {r.returncode}"
        )

    @pytest.mark.parametrize("tool", ["Read", "Edit", "Write", "MultiEdit"])
    def test_ipynb_always_exits_exactly_2(self, tool):
        """Blocked .ipynb operations must exit exactly 2 (PreToolUse blocking code)."""
        r = run_guard(tool, "test.ipynb")
        assert r.returncode == 2, (
            f"{tool} on .ipynb must exit exactly 2, got {r.returncode}"
        )

    def test_unknown_tool_with_ipynb_fails_open(self):
        """Unknown tool_name with a .ipynb path must fail open (exit 0), not block."""
        payload = json.dumps({
            "tool_name": "UnknownTool",
            "tool_input": {"file_path": "nb.ipynb"},
            "session_id": "test",
        })
        r = run_guard_raw(payload)
        assert r.returncode == 0, (
            f"Unknown tool must fail open (exit 0), got {r.returncode}"
        )
