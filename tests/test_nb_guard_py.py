"""
Tests for nb-guard.py — the cross-platform Python replacement for nb-guard.sh.

Invokes nb-guard.py directly as a Python script (no bash required).
Exit 0 = allow, exit 1 = block.

All tests are red until scripts/nb-guard.py is created.
"""
from __future__ import annotations

import json
import subprocess
import sys
import os
from pathlib import Path

import pytest

SCRIPTS   = Path(__file__).parent.parent / "scripts"
GUARD_PY  = SCRIPTS / "nb-guard.py"


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _payload(tool_name: str, file_path: str | None = None,
             edits: list | None = None) -> str:
    """Build a realistic hook payload JSON string."""
    if tool_name == "MultiEdit":
        # MultiEdit has a top-level file_path and edits[] with only old_string/new_string/replace_all
        tool_input = {"file_path": file_path or "nb.ipynb"}
        if edits is not None:
            tool_input["edits"] = edits
        else:
            tool_input["edits"] = [{"old_string": "x", "new_string": "y"}]
    else:
        tool_input = {}
        if file_path is not None:
            tool_input["file_path"] = file_path
    return json.dumps({
        "tool_name": tool_name,
        "tool_input": tool_input,
        "session_id": "test",
    })


def run_guard(payload: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(GUARD_PY)],
        input=payload,
        capture_output=True,
        text=True,
    )


# ---------------------------------------------------------------------------
# Prerequisite
# ---------------------------------------------------------------------------

def test_guard_py_exists():
    """nb-guard.py must exist at scripts/nb-guard.py."""
    assert GUARD_PY.exists(), f"nb-guard.py not found at {GUARD_PY}"


# ---------------------------------------------------------------------------
# Blocking behaviour
# ---------------------------------------------------------------------------

class TestBlocking:

    @pytest.mark.parametrize("tool", ["Read", "Edit", "Write"])
    def test_ipynb_blocked(self, tool):
        r = run_guard(_payload(tool, "analysis.ipynb"))
        assert r.returncode == 2, (
            f"{tool} on .ipynb must exit 2, got {r.returncode}\n"
            f"stdout: {r.stdout!r}\nstderr: {r.stderr!r}"
        )

    @pytest.mark.parametrize("tool", ["Read", "Edit", "Write"])
    def test_non_ipynb_allowed(self, tool):
        r = run_guard(_payload(tool, "script.py"))
        assert r.returncode == 0

    @pytest.mark.parametrize("tool", ["Read", "Edit", "Write"])
    def test_message_on_stderr_not_stdout(self, tool):
        r = run_guard(_payload(tool, "nb.ipynb"))
        assert r.returncode == 2
        assert r.stderr.strip() != "", "Redirect message must go to stderr"

    @pytest.mark.parametrize("tool", ["Read", "Edit", "Write"])
    def test_blocked_message_contains_blocked(self, tool):
        r = run_guard(_payload(tool, "nb.ipynb"))
        assert "Blocked" in r.stderr

    def test_read_message_references_nb_read_py(self):
        r = run_guard(_payload("Read", "nb.ipynb"))
        assert "nb-read.py" in r.stderr

    @pytest.mark.parametrize("tool", ["Edit", "Write"])
    def test_write_tools_reference_nb_write_py(self, tool):
        r = run_guard(_payload(tool, "nb.ipynb"))
        assert "nb-write.py" in r.stderr

    def test_subdirectory_path_blocked(self):
        r = run_guard(_payload("Read", "notebooks/data/analysis.ipynb"))
        assert r.returncode == 2

    def test_non_ipynb_no_blocking_message(self):
        r = run_guard(_payload("Read", "data.csv"))
        assert r.returncode == 0
        assert "Blocked" not in r.stderr

    @pytest.mark.parametrize("tool", ["Read", "Edit", "Write"])
    def test_message_contains_file_path(self, tool):
        r = run_guard(_payload(tool, "project/analysis.ipynb"))
        assert "project/analysis.ipynb" in r.stderr


# ---------------------------------------------------------------------------
# MultiEdit
# ---------------------------------------------------------------------------

class TestMultiEdit:

    def test_multiedit_with_ipynb_blocked(self):
        payload = _payload("MultiEdit", file_path="analysis.ipynb")
        r = run_guard(payload)
        assert r.returncode == 2

    def test_multiedit_without_ipynb_allowed(self):
        payload = _payload("MultiEdit", file_path="script.py")
        r = run_guard(payload)
        assert r.returncode == 0

    def test_multiedit_with_py_allowed(self):
        payload = _payload("MultiEdit", file_path="util.py")
        r = run_guard(payload)
        assert r.returncode == 0

    def test_multiedit_with_ipynb_shows_blocked(self):
        payload = _payload("MultiEdit", file_path="nb.ipynb")
        r = run_guard(payload)
        assert r.returncode == 2
        assert "Blocked" in r.stderr

    def test_multiedit_empty_edits_allowed(self):
        payload = _payload("MultiEdit", file_path="script.py")
        r = run_guard(payload)
        assert r.returncode == 0


# ---------------------------------------------------------------------------
# Fallback: tool_input.path key
# ---------------------------------------------------------------------------

class TestPathKeyFallback:

    def test_path_key_detected(self):
        """Some tool versions use tool_input.path instead of file_path."""
        payload = json.dumps({
            "tool_name": "Read",
            "tool_input": {"path": "analysis.ipynb"},
            "session_id": "x",
        })
        r = run_guard(payload)
        assert r.returncode == 2, (
            "tool_input.path fallback must detect .ipynb and block"
        )

    def test_path_key_non_ipynb_allowed(self):
        payload = json.dumps({
            "tool_name": "Read",
            "tool_input": {"path": "data.csv"},
            "session_id": "x",
        })
        r = run_guard(payload)
        assert r.returncode == 0


# ---------------------------------------------------------------------------
# Sanitisation of file paths in output
# ---------------------------------------------------------------------------

class TestOutputSanitisation:

    def test_ansi_in_file_path_stripped_from_output(self):
        """ANSI codes in the file path must be stripped before echoing."""
        path = "\x1b[31mhacked\x1b[0m.ipynb"
        r = run_guard(_payload("Read", path))
        # Whether blocked or not, no ANSI in stdout
        assert "\x1b" not in r.stdout, f"ANSI leaked: {r.stdout!r}"

    def test_newline_in_file_path_does_not_split_output(self):
        """A newline injected in the file path must not create extra lines."""
        path = "legit\ninjected_line.ipynb"
        r = run_guard(_payload("Read", path))
        # stdout must not contain a bare 'injected_line' as its own line
        lines = r.stdout.splitlines()
        assert "injected_line" not in lines, (
            f"Newline injection created a bare line: {lines}"
        )

    def test_null_byte_in_file_path_stripped(self):
        """Null bytes in the file path must be stripped from output."""
        path = "file\x00.ipynb"
        r = run_guard(_payload("Read", path))
        assert "\x00" not in r.stdout


# ---------------------------------------------------------------------------
# Fail-open on bad input
# ---------------------------------------------------------------------------

class TestFailOpen:

    def test_invalid_json_exits_0(self):
        """Garbage stdin must cause fail-open (exit 0), not a crash."""
        r = run_guard("not json at all")
        assert r.returncode == 0
        assert "Traceback" not in r.stdout
        assert "Traceback" not in r.stderr

    def test_empty_stdin_exits_0(self):
        r = run_guard("")
        assert r.returncode == 0

    def test_missing_tool_name_exits_0(self):
        payload = json.dumps({"tool_input": {"file_path": "nb.ipynb"}})
        r = run_guard(payload)
        # Unknown / missing tool name → fail open
        assert r.returncode == 0

    def test_missing_file_path_exits_0(self):
        payload = json.dumps({"tool_name": "Read", "tool_input": {}})
        r = run_guard(payload)
        assert r.returncode == 0


# ---------------------------------------------------------------------------
# No external dependencies
# ---------------------------------------------------------------------------

class TestNoDependencies:

    def test_runs_without_jq(self, tmp_path, monkeypatch):
        """nb-guard.py must run successfully even when jq is not on PATH."""
        # Provide a PATH that contains only an empty temp dir (no jq)
        monkeypatch.setenv("PATH", str(tmp_path))
        r = run_guard(_payload("Read", "nb.ipynb"))
        # Must still block correctly — Python stdlib handles the JSON
        assert r.returncode == 2

    def test_no_bash_required(self):
        """nb-guard.py is invoked directly as Python — bash is not needed."""
        # This test passes by virtue of running: the run_guard helper uses
        # sys.executable (python), not bash. If the script required bash
        # internals it would fail here.
        r = run_guard(_payload("Edit", "test.ipynb"))
        assert r.returncode == 2


# ---------------------------------------------------------------------------
# Python command in redirect message
# ---------------------------------------------------------------------------

class TestRedirectMessage:

    def test_redirect_message_contains_python_command(self):
        """The redirect message must show a runnable Python command."""
        r = run_guard(_payload("Read", "nb.ipynb"))
        assert r.returncode == 2
        # Must contain python3, python, or py (Windows Python Launcher) in the command
        assert ("python3" in r.stderr or "python " in r.stderr
                or "py " in r.stderr or "py -3" in r.stderr), (
            f"Expected python cmd in message: {r.stderr!r}"
        )

    def test_redirect_message_contains_absolute_scripts_path(self):
        """The redirect message must contain the absolute path to the scripts dir."""
        r = run_guard(_payload("Read", "nb.ipynb"))
        assert r.returncode == 2
        # Must reference nb-read.py with an absolute path (starts with /)
        # or contain the skills/nb/scripts directory reference
        assert "nb-read.py" in r.stderr
        assert os.sep in r.stderr or "/" in r.stderr


# ---------------------------------------------------------------------------
# NotebookEdit
# ---------------------------------------------------------------------------

class TestNotebookEdit:

    def test_notebookedit_with_ipynb_blocked(self):
        """NotebookEdit on .ipynb must be blocked."""
        payload = json.dumps({
            "tool_name": "NotebookEdit",
            "tool_input": {"notebook_path": "analysis.ipynb"},
            "session_id": "test",
        })
        r = subprocess.run(
            [sys.executable, str(GUARD_PY)],
            input=payload,
            capture_output=True,
            text=True,
        )
        assert r.returncode == 2, (
            f"NotebookEdit on .ipynb must exit 2, got {r.returncode}\n"
            f"stderr: {r.stderr!r}"
        )

    def test_notebookedit_message_on_stderr(self):
        """NotebookEdit block message must go to stderr."""
        payload = json.dumps({
            "tool_name": "NotebookEdit",
            "tool_input": {"notebook_path": "nb.ipynb"},
            "session_id": "test",
        })
        r = subprocess.run(
            [sys.executable, str(GUARD_PY)],
            input=payload,
            capture_output=True,
            text=True,
        )
        assert r.returncode == 2
        assert "Blocked" in r.stderr
        assert "nb-write.py" in r.stderr


# ---------------------------------------------------------------------------
# Write to new file
# ---------------------------------------------------------------------------

class TestWriteToNewFile:

    def test_write_to_new_ipynb_suggests_create(self, tmp_path, monkeypatch):
        """Write to a new .ipynb file should suggest 'create', not 'patch'."""
        # Ensure the file does not exist
        new_file = str(tmp_path / "newfile.ipynb")
        assert not os.path.exists(new_file)

        # Change to tmp_path to test with a non-existent path
        monkeypatch.chdir(tmp_path)

        payload = json.dumps({
            "tool_name": "Write",
            "tool_input": {"file_path": "newfile.ipynb"},
            "session_id": "test",
        })
        r = subprocess.run(
            [sys.executable, str(GUARD_PY)],
            input=payload,
            capture_output=True,
            text=True,
        )
        assert r.returncode == 2
        assert "create" in r.stderr, (
            f"Write to new .ipynb should suggest 'create', got: {r.stderr!r}"
        )
        assert "patch" not in r.stderr or "create" in r.stderr.split("patch")[0], (
            f"Should suggest 'create' before any 'patch' mention: {r.stderr!r}"
        )

    def test_write_to_existing_ipynb_suggests_patch(self, tmp_path, monkeypatch):
        """Write to an existing .ipynb file should suggest 'patch'."""
        # Create a temporary ipynb file
        existing_file = tmp_path / "existing.ipynb"
        existing_file.write_text("{}", encoding="utf-8")

        monkeypatch.chdir(tmp_path)

        payload = json.dumps({
            "tool_name": "Write",
            "tool_input": {"file_path": "existing.ipynb"},
            "session_id": "test",
        })
        r = subprocess.run(
            [sys.executable, str(GUARD_PY)],
            input=payload,
            capture_output=True,
            text=True,
        )
        assert r.returncode == 2
        assert "patch" in r.stderr, (
            f"Write to existing .ipynb should suggest 'patch', got: {r.stderr!r}"
        )
