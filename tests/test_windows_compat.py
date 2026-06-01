"""
Tests for Windows encoding and path compatibility gaps — TDD §2.14 / §15.

All tests correspond to issues identified in the post-audit of commit 5523dad
(the Windows compatibility patch).  Issues:

  A1  nb-index.py missing reconfigure block
  A2  reconfigure guard shares a single hasattr(sys.stdout) check for both streams
  A3  _compute_notebook_key absolute-path fallback uses str(nb_path) (backslashes on Windows)
  A4  Three sites use str(rel).replace(os.sep, "/") instead of rel.as_posix()
  B1  guard_cmd in install.py embeds Windows backslashes in settings.json
  B2  nb-guard.py _python_cmd() not updated for Windows py launcher
  B3  os.replace in nb-write.py dies immediately on PermissionError (no retry)
      nb-index.py os.replace PermissionError calls _die() (exit 1) instead of exit 0

Source-level checks are used when the bug is only observable on Windows (the CI
runs on Linux where paths and encodings are already UTF-8/forward-slash).
Subprocess/behavioral tests are added for invariants that hold on all platforms.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

REPO_ROOT    = Path(__file__).parent.parent
SCRIPTS_DIR  = REPO_ROOT / "scripts"

NB_GUARD_PY  = SCRIPTS_DIR / "nb-guard.py"
NB_INDEX_PY  = SCRIPTS_DIR / "nb-index.py"
NB_READ_PY   = SCRIPTS_DIR / "nb-read.py"
NB_SEARCH_PY = SCRIPTS_DIR / "nb-search.py"
NB_WRITE_PY  = SCRIPTS_DIR / "nb-write.py"

ALL_SCRIPTS = [NB_GUARD_PY, NB_INDEX_PY, NB_READ_PY, NB_SEARCH_PY, NB_WRITE_PY]

PYTHON = sys.executable


# ---------------------------------------------------------------------------
# Notebook factory (minimal, mirrors test_nb_index.py helper)
# ---------------------------------------------------------------------------

def _make_notebook(tmp_path: Path, name: str = "test.ipynb") -> Path:
    nb = {
        "nbformat": 4,
        "nbformat_minor": 5,
        "metadata": {
            "kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
            "language_info": {"name": "python"},
        },
        "cells": [
            {
                "cell_type": "code",
                "id": "c001",
                "metadata": {},
                "source": ["x = 1"],
                "outputs": [],
                "execution_count": 1,
            }
        ],
    }
    p = tmp_path / name
    p.write_text(json.dumps(nb), encoding="utf-8")
    return p


def _run_indexer(nb_path: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [PYTHON, str(NB_INDEX_PY), str(nb_path)],
        capture_output=True,
        text=True,
    )


# ===========================================================================
# A1 / A2 — reconfigure block: presence and independent guards
# ===========================================================================

class TestReconfigureCompliance:
    """Source-level checks that all five scripts have the encoding fix (A1/A2)."""

    @pytest.mark.parametrize("script", ALL_SCRIPTS, ids=lambda p: p.name)
    def test_all_scripts_have_reconfigure(self, script: Path):
        """A1: every script must call .reconfigure(encoding='utf-8') (§2.14 Gap 1)."""
        src = script.read_text(encoding="utf-8")
        assert "reconfigure" in src, (
            f"{script.name} is missing the reconfigure block — "
            f"Unicode output will crash on Windows cp1252 consoles."
        )

    @pytest.mark.parametrize("script", ALL_SCRIPTS, ids=lambda p: p.name)
    def test_stderr_has_independent_guard(self, script: Path):
        """A2: sys.stderr.reconfigure must be guarded independently, not under
        the sys.stdout hasattr check (§2.14 Gap 2).

        The wrong pattern:
            if hasattr(sys.stdout, 'reconfigure'):
                sys.stdout.reconfigure(...)
                sys.stderr.reconfigure(...)   # unguarded

        The correct pattern includes a separate hasattr(sys.stderr, ...) check.
        """
        src = script.read_text(encoding="utf-8")
        assert "hasattr(sys.stderr, 'reconfigure')" in src, (
            f"{script.name} must guard sys.stderr.reconfigure independently with "
            f"hasattr(sys.stderr, 'reconfigure'). Sharing the sys.stdout check is wrong "
            f"when the two streams are heterogeneous objects."
        )

    def test_nb_index_runs_without_encoding_crash(self, tmp_path):
        """A1 behavioral: nb-index.py must exit 0 (not crash with UnicodeEncodeError)
        when invoked on a notebook whose path contains a non-ASCII directory name.

        On Linux the locale is UTF-8 so the crash is not observable; on Windows cp1252
        this test would catch a missing reconfigure block. Included as a cross-platform
        smoke test and regression guard.
        """
        # Directory name with non-ASCII (safe on Linux/macOS; on Windows the test
        # is skipped if the filesystem rejects the name)
        unicode_dir = tmp_path / "données_café"
        try:
            unicode_dir.mkdir()
        except (OSError, UnicodeEncodeError):
            pytest.skip("Filesystem does not support this Unicode directory name")

        nb = _make_notebook(unicode_dir)
        r = _run_indexer(nb)
        assert r.returncode == 0, (
            f"nb-index.py crashed on Unicode path.\n"
            f"stderr: {r.stderr!r}"
        )
        assert "UnicodeEncodeError" not in r.stderr
        assert "UnicodeDecodeError" not in r.stderr


# ===========================================================================
# A3 / A4 — path normalization: forward slashes everywhere
# ===========================================================================

class TestPathNormalization:
    """Tests that index files always store forward-slash paths (A3/A4, §15.2)."""

    def test_no_str_replace_os_sep_in_nb_index(self):
        """A4: all str(rel).replace(os.sep, '/') usages must be replaced with
        rel.as_posix() (§2.14 Gap 4). This source check fails while the old
        pattern is present and passes once all three sites are updated.
        """
        src = NB_INDEX_PY.read_text(encoding="utf-8")
        count = src.count("str(rel).replace(os.sep")
        assert count == 0, (
            f"nb-index.py still contains {count} occurrence(s) of "
            f"str(rel).replace(os.sep, '/') — replace with rel.as_posix() "
            f"for consistency and correctness (§2.14 Gap 4)."
        )

    def test_notebook_path_field_no_backslashes(self, tmp_path):
        """The 'notebook_path' field in the generated JSON index must use
        forward slashes on all platforms (§15.2 / Index Schema).
        """
        nb = _make_notebook(tmp_path)
        r = _run_indexer(nb)
        assert r.returncode == 0, f"Indexer failed: {r.stderr}"

        # Locate the index file — walk .nb_index/ directories
        index_files = list(tmp_path.rglob(".nb_index/*.json"))
        assert index_files, "No index file written"

        data = json.loads(index_files[0].read_text(encoding="utf-8"))
        nb_path_field = data.get("notebook_path", "")
        assert "\\" not in nb_path_field, (
            f"notebook_path contains backslashes: {nb_path_field!r}\n"
            f"Must always use forward slashes (§15.2)."
        )

    def test_symbols_json_key_no_backslashes(self, tmp_path):
        """A3: the notebook key in symbols.json must use forward slashes.

        This test exercises the absolute-path fallback by placing the notebook
        outside any git root (in a plain tmp_path with no .git directory).
        On Windows, _compute_notebook_key's str(nb_path) fallback would return
        backslashes; this test catches that if run on Windows or if the code is
        refactored to reintroduce the bug.
        """
        # Ensure there is no .git in the tmp_path hierarchy by using an isolated
        # directory that cannot be inside the repo's git root.
        standalone = tmp_path / "standalone"
        standalone.mkdir()
        nb = _make_notebook(standalone)
        r = _run_indexer(nb)
        assert r.returncode == 0, f"Indexer failed: {r.stderr}"

        # Find symbols.json
        symbols_files = list(standalone.rglob("symbols.json"))
        if not symbols_files:
            # Some platforms may not write symbols.json in all configurations.
            # Check in tmp_path too (index may be placed at parent level)
            symbols_files = list(tmp_path.rglob("symbols.json"))

        if not symbols_files:
            pytest.skip("symbols.json not written — cannot verify key format")

        data = json.loads(symbols_files[0].read_text(encoding="utf-8"))
        # All notebook keys in the 'notebooks' or any cross-notebook section
        for section in ("symbols", "imports"):
            for _sym, locations in data.get(section, {}).items():
                for loc in locations:
                    nb_key_part = loc.rsplit(":", 1)[0]
                    assert "\\" not in nb_key_part, (
                        f"symbols.json location key contains backslashes: {loc!r}\n"
                        f"Must use forward slashes (§13.2a / §15.2)."
                    )


# ===========================================================================
# B2 — nb-guard.py _python_cmd(): Windows py launcher support
# ===========================================================================

class TestNbGuardPythonCmd:
    """B2: nb-guard.py's _python_cmd() must have a branch for the Windows py launcher."""

    def test_nb_guard_has_windows_py_branch(self):
        """Source check: nb-guard.py _python_cmd() must contain logic for
        the 'py' or 'py -3' Windows Python Launcher (§2.14 Bug B2).

        Without this, the redirect messages printed by nb-guard.py on Windows
        would say 'python3 nb-read.py ...' when 'python3' is not on the PATH.
        """
        src = NB_GUARD_PY.read_text(encoding="utf-8")
        # The function body must contain "py" as a command option for Windows.
        # Accept "py -3" or standalone "py" branch — both are valid.
        assert '"py"' in src or '"py -3"' in src, (
            "nb-guard.py _python_cmd() must include a Windows py launcher branch "
            "('py' or 'py -3'). Currently it only handles 'python3'/'python' "
            "(§2.14 Bug B2)."
        )

    def test_nb_guard_python_cmd_function_body_has_win32_check(self):
        """Source check: nb-guard.py must check sys.platform for the py branch."""
        src = NB_GUARD_PY.read_text(encoding="utf-8")
        assert "win32" in src, (
            "nb-guard.py must check sys.platform == 'win32' in _python_cmd() "
            "to conditionally return 'py -3' (§2.14 Bug B2)."
        )


# ===========================================================================
# B3 — os.replace retry and PermissionError handling
# ===========================================================================

class TestAtomicWriteRetry:
    """B3: os.replace must retry on transient PermissionError; nb-index.py
    PermissionError must exit 0, not 1 (§2.14 Bug B3 / §15.3).
    """

    def test_nb_write_has_replace_retry_logic(self):
        """Source check: nb-write.py save() must contain retry logic around
        os.replace to handle antivirus-triggered transient PermissionError on
        Windows (§2.14 Bug B3).

        The canonical implementation uses time.sleep() inside a retry loop.
        """
        src = NB_WRITE_PY.read_text(encoding="utf-8")
        has_retry = "time.sleep" in src or "_REPLACE_RETRIES" in src
        assert has_retry, (
            "nb-write.py save() is missing retry logic for os.replace(). "
            "On Windows, antivirus scanners hold transient PermissionError "
            "locks that resolve within milliseconds — a retry loop is required "
            "(§2.14 Bug B3). Add time.sleep() between attempts."
        )

    def test_nb_write_imports_time_module(self):
        """Companion to retry test: the 'time' module must be imported if retry
        logic using time.sleep() is present.
        """
        src = NB_WRITE_PY.read_text(encoding="utf-8")
        # Only enforce the import if retry logic is present
        if "time.sleep" in src:
            assert "import time" in src, (
                "nb-write.py uses time.sleep() but 'import time' is not at the "
                "module level."
            )

    def test_nb_write_permission_error_message_mentions_locked(self, tmp_path):
        """B3 behavioral: the error message when os.replace fails with PermissionError
        must mention that the file is locked (so users know to close Jupyter).

        NOTE: True PermissionError from os.replace is only reliably triggerable on
        Windows (AV scanner or Jupyter holding the file). On Linux we verify the
        message text via source inspection; the source check
        test_nb_write_has_replace_retry_logic ensures retry logic exists.
        """
        src = NB_WRITE_PY.read_text(encoding="utf-8")
        assert "locked by another process" in src, (
            "nb-write.py's PermissionError message must tell the user the file is "
            "'locked by another process'. This message helps Windows users understand "
            "they need to close Jupyter or wait for the AV scan to complete."
        )

    def test_nb_index_permission_error_is_nonfatal(self):
        """§15.3 source check: nb-index.py's os.replace call must catch PermissionError
        separately from OSError and must exit 0, not call _die() (§2.14 Bug B3).

        Since indexing is fire-and-forget, a transient file-lock on the index
        file must not cause a non-zero exit.
        """
        import re as _re
        src = NB_INDEX_PY.read_text(encoding="utf-8")

        # 1. A 'except PermissionError' clause must exist.
        assert "PermissionError" in src, (
            "nb-index.py has no PermissionError handling for os.replace. "
            "Transient AV-triggered PermissionError must be caught and exit 0 (§15.3)."
        )

        # 2. Extract each 'except PermissionError' block and verify it does NOT
        #    call _die(). We look for blocks of the pattern:
        #       except PermissionError:
        #           <body>
        #    terminated by a line at the same or lower indentation as 'except'.
        #
        #    Strategy: find each 'except PermissionError' line, then collect
        #    subsequent indented lines (the block body). Check that _die( does
        #    not appear in that body.
        lines = src.splitlines()
        in_pe_block = False
        pe_indent = None
        pe_body_lines: list[str] = []

        for line in lines:
            stripped = line.lstrip()
            indent = len(line) - len(stripped)

            if _re.match(r'except\s+PermissionError\s*:', stripped):
                # Start of a new except PermissionError block
                in_pe_block = True
                pe_indent = indent
                pe_body_lines = []
                continue

            if in_pe_block:
                if not stripped:
                    # Blank line — stay in block
                    pe_body_lines.append(line)
                elif indent > pe_indent:
                    # Indented body line
                    pe_body_lines.append(line)
                else:
                    # Back to same or lower indent — block ended; check it
                    block_src = "\n".join(pe_body_lines)
                    assert "_die(" not in block_src, (
                        f"nb-index.py has a 'except PermissionError' block that calls "
                        f"_die() — this exits 1 but index write failures must exit 0 "
                        f"(§15.3 / §2.14 Bug B3).\nBlock body:\n{block_src}"
                    )
                    in_pe_block = False
                    pe_indent = None
                    pe_body_lines = []

        # Handle block at end of file
        if in_pe_block and pe_body_lines:
            block_src = "\n".join(pe_body_lines)
            assert "_die(" not in block_src, (
                f"nb-index.py has a 'except PermissionError' block that calls "
                f"_die() — this exits 1 but index write failures must exit 0 "
                f"(§15.3 / §2.14 Bug B3).\nBlock body:\n{block_src}"
            )
