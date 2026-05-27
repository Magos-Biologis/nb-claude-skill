"""
Hardened TDD tests for nb-guard.sh.

Covers security and correctness findings from adversarial audit:

  Security:
    - Subdirectory paths (notebooks/a.ipynb) must be blocked, not bypassed by glob
    - Deeply nested paths (a/b/c/nb.ipynb) must be blocked
    - ANSI escape sequences in file paths must not appear in output
    - Newline injection in file paths must not produce extra output lines
    - Non-.ipynb files must be ALLOWED (exit 0), not blocked

  Correctness:
    - MultiEdit: file path must be extracted from tool_input.edits[].file_path
    - MultiEdit: must block when ANY edit targets a .ipynb file
    - MultiEdit: must allow (exit 0) when no edit targets .ipynb

  Robustness:
    - jq failure (malformed JSON) must exit cleanly (fail open: exit 0)
    - Unknown TOOL with .ipynb path must exit 1 (not silently allow)
"""

import json
import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent
GUARD     = REPO_ROOT / "scripts" / "nb-guard.sh"

_claude_dir = Path(os.environ.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude")))
SETTINGS    = _claude_dir / "settings.json"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def run_guard_raw(payload_str: str) -> subprocess.CompletedProcess:
    """Run nb-guard.sh with arbitrary stdin string."""
    return subprocess.run(
        ["bash", str(GUARD)],
        input=payload_str,
        capture_output=True,
        text=True,
    )


def run_guard(tool_name: str, file_path: str = "analysis.ipynb") -> subprocess.CompletedProcess:
    """Run nb-guard.sh with a correctly-shaped payload for the given tool.

    MultiEdit uses tool_input.edits[].file_path; all other tools use
    tool_input.file_path.  Using the correct shape ensures tests match
    real harness payloads.
    """
    if tool_name == "MultiEdit":
        tool_input = {"edits": [{"file_path": file_path, "old_string": "x", "new_string": "y"}]}
    else:
        tool_input = {"file_path": file_path}
    payload = json.dumps({
        "tool_name": tool_name,
        "tool_input": tool_input,
        "session_id": "test-session",
    })
    return run_guard_raw(payload)


def run_guard_multiedit(edits: list) -> subprocess.CompletedProcess:
    """Run nb-guard.sh with a MultiEdit-style payload (edits array)."""
    payload = json.dumps({
        "tool_name": "MultiEdit",
        "tool_input": {"edits": edits},
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
        assert r.returncode == 1, (
            f"Expected exit 1 for Read on '{path}', got {r.returncode}\n"
            f"stdout: {r.stdout!r}\nstderr: {r.stderr!r}"
        )

    @pytest.mark.parametrize("path", [
        "notebooks/analysis.ipynb",
        "a/b/c/deep.ipynb",
    ])
    def test_subdirectory_ipynb_blocked_message(self, path):
        """Block message must mention the subdirectory path."""
        r = run_guard("Read", path)
        assert "Blocked" in r.stdout, f"Expected 'Blocked' in stdout, got: {r.stdout!r}"

    @pytest.mark.parametrize("tool", ["Read", "Edit", "Write", "MultiEdit"])
    def test_subdirectory_blocked_for_all_tools(self, tool):
        """All four tools must be blocked even for subdirectory .ipynb paths."""
        r = run_guard(tool, "sub/dir/notebook.ipynb")
        assert r.returncode == 1, (
            f"{tool} on subdirectory .ipynb should exit 1, got {r.returncode}"
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
        "analysis.IPYNB",             # wrong case (case-sensitive glob)
    ])
    def test_non_ipynb_exits_zero(self, path):
        """Non-.ipynb files must exit 0 (allow), not block."""
        r = run_guard("Read", path)
        assert r.returncode == 0, (
            f"Expected exit 0 (allow) for Read on '{path}', got {r.returncode}\n"
            f"stdout: {r.stdout!r}"
        )

    @pytest.mark.parametrize("path", [
        "script.py",
        "data.csv",
    ])
    def test_non_ipynb_produces_no_output(self, path):
        """Non-.ipynb files must produce no blocking output on stdout."""
        r = run_guard("Read", path)
        assert r.stdout.strip() == "", (
            f"Expected empty stdout for non-.ipynb '{path}', got: {r.stdout!r}"
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
        """A newline embedded in the file path must not produce extra 'Blocked:' lines.

        The path ends with .ipynb so the guard fires (bash * matches newlines in
        double-bracket glob patterns).  Without sanitisation the echo would split
        across lines, producing two 'Blocked:' prefixes; with sanitisation, one.
        """
        injected_path = "malicious\nBlocked: injected-override.ipynb"
        r = run_guard("Read", injected_path)
        assert r.returncode == 1, (
            f"Expected exit 1 for .ipynb path with embedded newline, got {r.returncode}"
        )
        blocked_lines = [l for l in r.stdout.splitlines() if l.startswith("Blocked:")]
        assert len(blocked_lines) == 1, (
            f"Expected exactly 1 'Blocked:' line, got {len(blocked_lines)}:\n{r.stdout!r}"
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

    def test_multiedit_with_ipynb_in_edits_is_blocked(self):
        """MultiEdit with a .ipynb file in edits[] must be blocked."""
        edits = [{"file_path": "analysis.ipynb", "old_string": "x", "new_string": "y"}]
        r = run_guard_multiedit(edits)
        assert r.returncode == 1, (
            f"MultiEdit targeting .ipynb should exit 1, got {r.returncode}\n"
            f"stdout: {r.stdout!r}\nstderr: {r.stderr!r}"
        )

    def test_multiedit_with_ipynb_shows_blocked_message(self):
        """MultiEdit block message must include 'Blocked'."""
        edits = [{"file_path": "nb.ipynb", "old_string": "a", "new_string": "b"}]
        r = run_guard_multiedit(edits)
        assert "Blocked" in r.stdout, f"Expected 'Blocked' in MultiEdit output: {r.stdout!r}"

    def test_multiedit_with_mixed_files_blocked_when_ipynb_present(self):
        """MultiEdit with one .ipynb and one .py file must still be blocked."""
        edits = [
            {"file_path": "helper.py", "old_string": "a", "new_string": "b"},
            {"file_path": "analysis.ipynb", "old_string": "x", "new_string": "y"},
        ]
        r = run_guard_multiedit(edits)
        assert r.returncode == 1, (
            f"MultiEdit with mixed files (incl. .ipynb) should exit 1, got {r.returncode}"
        )

    def test_multiedit_with_no_ipynb_is_allowed(self):
        """MultiEdit with only non-.ipynb files must be allowed (exit 0)."""
        edits = [
            {"file_path": "script.py", "old_string": "a", "new_string": "b"},
            {"file_path": "config.json", "old_string": "x", "new_string": "y"},
        ]
        r = run_guard_multiedit(edits)
        assert r.returncode == 0, (
            f"MultiEdit with no .ipynb files should exit 0, got {r.returncode}\n"
            f"stdout: {r.stdout!r}"
        )

    def test_multiedit_empty_edits_is_allowed(self):
        """MultiEdit with empty edits array must be allowed (exit 0)."""
        r = run_guard_multiedit([])
        assert r.returncode == 0, (
            f"MultiEdit with empty edits should exit 0, got {r.returncode}"
        )

    def test_multiedit_subdirectory_ipynb_in_edits_is_blocked(self):
        """MultiEdit targeting a subdirectory .ipynb file must be blocked."""
        edits = [{"file_path": "notebooks/analysis.ipynb", "old_string": "x", "new_string": "y"}]
        r = run_guard_multiedit(edits)
        assert r.returncode == 1, (
            f"MultiEdit on notebooks/analysis.ipynb should exit 1, got {r.returncode}"
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

    def test_jq_error_does_not_produce_exit_code_other_than_0_or_1(self):
        """Previously 'set -e' caused jq error code to propagate. Must be 0 or 1."""
        r = run_guard_raw("{broken json")
        assert r.returncode in (0, 1), (
            f"Script must exit 0 (allow) or 1 (block), not jq's code. Got: {r.returncode}"
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
    def test_ipynb_always_exits_exactly_1(self, tool):
        """Blocked .ipynb operations must exit exactly 1 (not 2=abort, not 127=missing)."""
        r = run_guard(tool, "test.ipynb")
        assert r.returncode == 1, (
            f"{tool} on .ipynb must exit exactly 1, got {r.returncode}"
        )


# ---------------------------------------------------------------------------
# Settings.json: no-if-condition approach (post-install verification)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(
    not SETTINGS.exists(),
    reason="settings.json not found — run install.sh first",
)
class TestSettingsHardenedApproach:
    """Verifies settings.json is correctly configured post-install."""

    @pytest.fixture(scope="class")
    def settings(self):
        return json.loads(SETTINGS.read_text())

    @pytest.fixture(scope="class")
    def pretooluse_hooks(self, settings):
        hooks = settings.get("hooks", {}).get("PreToolUse", [])
        if not hooks:
            pytest.skip("No PreToolUse hooks in settings.json — run install.sh first")
        return hooks

    def test_nb_guard_hook_present(self, pretooluse_hooks):
        """At least one PreToolUse hook must reference nb-guard.sh."""
        all_hooks = []
        for entry in pretooluse_hooks:
            all_hooks.extend(entry.get("hooks", []))
        guard_hooks = [h for h in all_hooks if "nb-guard.sh" in h.get("command", "")]
        assert guard_hooks, "No PreToolUse hook references nb-guard.sh"

    def test_nb_guard_command_is_absolute_path(self, pretooluse_hooks):
        """nb-guard.sh command must be an absolute path, not a shell variable."""
        all_hooks = []
        for entry in pretooluse_hooks:
            all_hooks.extend(entry.get("hooks", []))
        for h in [h for h in all_hooks if "nb-guard.sh" in h.get("command", "")]:
            assert "${CLAUDE_CONFIG_DIR}" not in h["command"], (
                "nb-guard.sh path must not use ${CLAUDE_CONFIG_DIR} in settings.json"
            )

    def test_nb_guard_matcher_covers_multiedit(self, pretooluse_hooks):
        """The nb-guard PreToolUse hook matcher must include MultiEdit."""
        for entry in pretooluse_hooks:
            matcher = entry.get("matcher", "")
            hooks = entry.get("hooks", [])
            if any("nb-guard.sh" in h.get("command", "") for h in hooks):
                assert "MultiEdit" in matcher, (
                    f"nb-guard entry matcher must include MultiEdit: {matcher!r}"
                )
                return
        pytest.fail("No PreToolUse entry contains nb-guard.sh")
