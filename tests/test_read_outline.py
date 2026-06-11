"""
Tests for nb-read.py --outline mode (TDD_INDEX.md §9).

All tests are RED until nb-read.py implements --outline.

The --outline flag renders a compact one-line-per-cell table instead of the
full source view. Format per cell:

    [N:code:run=N ] first_line       (code cells, right-padded)
    [N:markdown   ] first_line       (markdown/raw cells, no run= field)

Key behaviours under test:
  - One stdout line per cell, no ─── bar between cells
  - run=—— when execution_count is null
  - Falls back to reading the notebook directly when no index exists
  - Prints [STALE INDEX] to stderr and falls back when the index is stale
  - Compatible with --cells and --type filters
  - Empty cells show "(empty)" as the first-line placeholder

subprocess is used throughout so tests exercise the real CLI boundary.
"""

import json
import secrets
import string
import subprocess
import sys
import time
from pathlib import Path

import pytest

SCRIPTS      = Path(__file__).parent.parent / "scripts"
NB_READ      = str(SCRIPTS / "nb-read.py")
INDEX_SCRIPT = SCRIPTS / "nb-index.py"   # may not exist yet


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cell_id():
    return "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(8))


def _make_nb(cells, tmp_path, name="test.ipynb"):
    """Build a minimal nbformat-4 notebook and write it to tmp_path."""
    nb = {
        "nbformat": 4,
        "nbformat_minor": 5,
        "metadata": {
            "kernelspec": {
                "name": "python3",
                "language": "python",
                "display_name": "Python 3",
            }
        },
        "cells": [],
    }
    for c in cells:
        cell = {
            "id": _cell_id(),
            "cell_type": c.get("cell_type", "code"),
            "metadata": {},
            "source": c.get("source", []),
        }
        if cell["cell_type"] == "code":
            cell["outputs"] = c.get("outputs", [])
            cell["execution_count"] = c.get("execution_count", None)
        nb["cells"].append(cell)
    p = tmp_path / name
    p.write_text(json.dumps(nb, indent=1), encoding="utf-8")
    return str(p)


def run_read(args, **kw):
    return subprocess.run(
        [sys.executable, NB_READ] + args,
        capture_output=True,
        text=True,
        **kw,
    )


def _run_index(nb_path):
    """Run nb-index.py on the given notebook path. Returns CompletedProcess."""
    return subprocess.run(
        [sys.executable, str(INDEX_SCRIPT), nb_path],
        capture_output=True,
        text=True,
    )


# ---------------------------------------------------------------------------
# § Basic acceptance
# ---------------------------------------------------------------------------

class TestOutlineBasic:

    def test_outline_flag_accepted(self, tmp_path):
        """--outline must exit 0 on a valid notebook."""
        p = _make_nb([
            {"cell_type": "code", "source": ["x = 1"], "execution_count": 1},
        ], tmp_path)
        r = run_read([p, "--outline"])
        assert r.returncode == 0, f"--outline exited non-zero: stderr={r.stderr!r}"

    def test_outline_one_line_per_cell(self, tmp_path):
        """stdout must have exactly N non-blank lines for N cells (after the header line)."""
        cells = [
            {"cell_type": "code",     "source": ["a = 1"],   "execution_count": 1},
            {"cell_type": "markdown", "source": ["## Head"]},
            {"cell_type": "code",     "source": ["b = 2"],   "execution_count": None},
        ]
        p = _make_nb(cells, tmp_path)
        r = run_read([p, "--outline"])
        assert r.returncode == 0
        non_blank = [l for l in r.stdout.splitlines() if l.strip()]
        # First non-blank line is the notebook header; remainder are cell lines
        cell_lines = non_blank[1:]
        assert len(cell_lines) == 3, (
            f"Expected 3 cell lines, got {len(cell_lines)}:\n{r.stdout}"
        )

    def test_outline_no_bar(self, tmp_path):
        """No '────' separator bar must appear in outline output."""
        p = _make_nb([
            {"cell_type": "code", "source": ["x = 1"], "execution_count": 1},
            {"cell_type": "markdown", "source": ["## Title"]},
        ], tmp_path)
        r = run_read([p, "--outline"])
        assert r.returncode == 0
        assert "────" not in r.stdout, (
            f"Found '────' bar in --outline output (should not be there):\n{r.stdout}"
        )

    def test_outline_stdout_silent_except_cells(self, tmp_path):
        """No stray blank lines between cell lines (compact one-per-line table)."""
        cells = [
            {"cell_type": "code",     "source": ["a = 1"], "execution_count": 1},
            {"cell_type": "markdown", "source": ["## H"]},
        ]
        p = _make_nb(cells, tmp_path)
        r = run_read([p, "--outline"])
        assert r.returncode == 0
        lines = r.stdout.splitlines()
        # After the header line, there must be no blank lines interspersed
        # between consecutive cell lines
        non_blank_indices = [i for i, l in enumerate(lines) if l.strip()]
        if len(non_blank_indices) >= 2:
            # The header is lines[0]; cell lines start after
            cell_line_indices = non_blank_indices[1:]
            for a, b in zip(cell_line_indices, cell_line_indices[1:]):
                assert b == a + 1, (
                    f"Blank line found between cell lines at positions {a} and {b}:\n"
                    f"{r.stdout!r}"
                )


# ---------------------------------------------------------------------------
# § Cell format
# ---------------------------------------------------------------------------

class TestOutlineCellFormat:

    def test_outline_code_cell_format(self, tmp_path):
        """Code cell outline line must start with '[0:code:' prefix."""
        p = _make_nb([
            {"cell_type": "code", "source": ["import os"], "execution_count": 1},
        ], tmp_path)
        r = run_read([p, "--outline"])
        assert r.returncode == 0
        cell_lines = [l for l in r.stdout.splitlines() if l.startswith("[")]
        assert any(l.startswith("[0:code:") for l in cell_lines), (
            f"Expected '[0:code:' prefix, got cell lines: {cell_lines}"
        )

    def test_outline_markdown_cell_format(self, tmp_path):
        """Markdown cell outline line must contain '[N:markdown' (no run= field)."""
        p = _make_nb([
            {"cell_type": "code",     "source": ["x = 1"], "execution_count": 1},
            {"cell_type": "markdown", "source": ["## Heading"]},
        ], tmp_path)
        r = run_read([p, "--outline"])
        assert r.returncode == 0
        cell_lines = [l for l in r.stdout.splitlines() if l.startswith("[")]
        md_lines = [l for l in cell_lines if ":markdown" in l]
        assert md_lines, f"No markdown outline line found:\n{r.stdout}"
        # Markdown lines must NOT contain run=
        for l in md_lines:
            assert "run=" not in l, (
                f"Markdown outline line must not contain 'run=': {l!r}"
            )

    def test_outline_run_count_shown(self, tmp_path):
        """'run=1' must appear in the outline line for a code cell with execution_count=1."""
        p = _make_nb([
            {"cell_type": "code", "source": ["x = 1"], "execution_count": 1},
        ], tmp_path)
        r = run_read([p, "--outline"])
        assert r.returncode == 0
        assert "run=1" in r.stdout, (
            f"Expected 'run=1' in outline output, got:\n{r.stdout}"
        )

    def test_outline_run_dashes_for_not_run(self, tmp_path):
        """'run=——' must appear when execution_count is null (cell never executed)."""
        p = _make_nb([
            {"cell_type": "code", "source": ["x = 1"], "execution_count": None},
        ], tmp_path)
        r = run_read([p, "--outline"])
        assert r.returncode == 0
        assert "run=——" in r.stdout, (
            f"Expected 'run=——' for null execution_count, got:\n{r.stdout}"
        )

    def test_outline_first_line_extracted(self, tmp_path):
        """The first source line of the cell must appear in the outline output."""
        p = _make_nb([
            {"cell_type": "code",
             "source": ["import pandas as pd\n", "import numpy as np\n"],
             "execution_count": 1},
        ], tmp_path)
        r = run_read([p, "--outline"])
        assert r.returncode == 0
        assert "import pandas as pd" in r.stdout, (
            f"Expected first source line in outline, got:\n{r.stdout}"
        )

    def test_outline_empty_cell_shows_empty_marker(self, tmp_path):
        """An empty cell must show '(empty)' in its outline line."""
        p = _make_nb([
            {"cell_type": "code", "source": [], "execution_count": None},
        ], tmp_path)
        r = run_read([p, "--outline"])
        assert r.returncode == 0
        assert "(empty)" in r.stdout, (
            f"Expected '(empty)' for empty cell in outline, got:\n{r.stdout}"
        )


# ---------------------------------------------------------------------------
# § Fallback behaviour (index absent / stale)
# ---------------------------------------------------------------------------

class TestOutlineFallback:

    def test_outline_works_without_index(self, tmp_path):
        """--outline must succeed (exit 0, produce cell lines) without any index."""
        p = _make_nb([
            {"cell_type": "code",     "source": ["x = 1"], "execution_count": 1},
            {"cell_type": "markdown", "source": ["## Title"]},
        ], tmp_path)
        # Explicitly ensure no .nb_index directory exists
        nb_dir = Path(p).parent
        index_dir = nb_dir / ".nb_index"
        assert not index_dir.exists(), "Test setup error: index dir should not exist"

        r = run_read([p, "--outline"])
        assert r.returncode == 0, f"--outline failed without index: stderr={r.stderr!r}"
        non_blank = [l for l in r.stdout.splitlines() if l.strip()]
        # At minimum: header + 2 cell lines
        assert len(non_blank) >= 3, (
            f"Expected at least 3 non-blank lines (header + 2 cells), got:\n{r.stdout}"
        )

    @pytest.mark.skipif(
        not INDEX_SCRIPT.exists(),
        reason="nb-index.py not yet implemented — stale-index test requires it",
    )
    def test_outline_stale_index_warns_stderr(self, tmp_path):
        """When the index exists but the notebook mtime is newer, stderr must contain
        '[STALE INDEX]' and nb-read must fall back to reading the notebook."""
        p = _make_nb([
            {"cell_type": "code", "source": ["x = 1"], "execution_count": 1},
        ], tmp_path)

        # Build a fresh index
        idx_result = _run_index(p)
        assert idx_result.returncode == 0, (
            f"nb-index.py failed: {idx_result.stderr!r}"
        )

        # Advance the notebook mtime deterministically so it is strictly newer
        # than the index (sleep+touch is flaky on coarse-mtime filesystems)
        nb_path = Path(p)
        t = nb_path.stat().st_mtime + 2.0
        os.utime(nb_path, (t, t))

        r = run_read([p, "--outline"])
        assert r.returncode == 0
        assert "[STALE INDEX]" in r.stderr, (
            f"Expected '[STALE INDEX]' on stderr when index is stale, got: {r.stderr!r}"
        )


# ---------------------------------------------------------------------------
# § Filter compatibility
# ---------------------------------------------------------------------------

class TestOutlineCompatibility:

    def test_outline_with_cells_filter(self, tmp_path):
        """--outline --cells 0 must show only the outline line for cell 0."""
        cells = [
            {"cell_type": "code",     "source": ["alpha = 1"], "execution_count": 1},
            {"cell_type": "markdown", "source": ["## Beta"]},
            {"cell_type": "code",     "source": ["gamma = 3"], "execution_count": 2},
        ]
        p = _make_nb(cells, tmp_path)
        r = run_read([p, "--outline", "--cells", "0"])
        assert r.returncode == 0
        cell_lines = [l for l in r.stdout.splitlines() if l.startswith("[")]
        assert len(cell_lines) == 1, (
            f"Expected exactly 1 cell line with --cells 0, got {len(cell_lines)}:\n{r.stdout}"
        )
        assert "[0:" in cell_lines[0], (
            f"Expected cell 0 line, got: {cell_lines[0]!r}"
        )
        assert "alpha" in cell_lines[0], (
            f"Expected first_line 'alpha = 1' in cell 0 line: {cell_lines[0]!r}"
        )

    def test_outline_with_type_filter(self, tmp_path):
        """--outline --type code must exclude markdown cells."""
        cells = [
            {"cell_type": "code",     "source": ["x = 1"],   "execution_count": 1},
            {"cell_type": "markdown", "source": ["## Head"]},
            {"cell_type": "code",     "source": ["y = 2"],   "execution_count": 2},
        ]
        p = _make_nb(cells, tmp_path)
        r = run_read([p, "--outline", "--type", "code"])
        assert r.returncode == 0
        cell_lines = [l for l in r.stdout.splitlines() if l.startswith("[")]
        assert all(":markdown" not in l for l in cell_lines), (
            f"Markdown cell found in --type code output:\n{r.stdout}"
        )
        assert len(cell_lines) == 2, (
            f"Expected 2 code cell lines, got {len(cell_lines)}:\n{r.stdout}"
        )

    def test_outline_no_safe_is_accepted(self, tmp_path):
        """--outline --no-safe must exit 0 and produce output (§9.7)."""
        p = _make_nb([
            {"cell_type": "code", "source": ["x = 1"], "execution_count": 1},
        ], tmp_path)
        r = run_read([p, "--outline", "--no-safe"])
        assert r.returncode == 0, (
            f"--outline --no-safe must not error, got stderr={r.stderr!r}"
        )
        non_blank = [l for l in r.stdout.splitlines() if l.strip()]
        assert len(non_blank) >= 2, (
            f"Expected header + at least 1 cell line with --outline --no-safe, "
            f"got:\n{r.stdout}"
        )


# ---------------------------------------------------------------------------
# § Index-backed fast path (fresh index → notebook never opened)
# ---------------------------------------------------------------------------

import os


def _index_file_for(nb_path):
    """No-git case: the index lives at <nb-dir>/.nb_index/<name>.json."""
    p = Path(nb_path)
    return p.parent / ".nb_index" / (p.name + ".json")


@pytest.mark.skipif(
    not INDEX_SCRIPT.exists(),
    reason="nb-index.py required for index-backed outline tests",
)
class TestOutlineIndexBacked:

    @pytest.mark.skipif(
        sys.platform == "win32" or getattr(os, "geteuid", lambda: -1)() == 0,
        reason="chmod 000 is not enforceable on Windows or as root",
    )
    def test_outline_from_fresh_index_never_opens_notebook(self, tmp_path):
        """With a fresh index, --outline must render entirely from the index:
        it must succeed even when the notebook file itself is unreadable."""
        p = _make_nb([
            {"cell_type": "code", "source": ["fastpath_marker = 1\n"],
             "execution_count": 3},
            {"cell_type": "markdown", "source": ["## Section A\n"]},
        ], tmp_path)
        r_idx = _run_index(p)
        assert r_idx.returncode == 0, f"nb-index.py failed: {r_idx.stderr!r}"
        assert _index_file_for(p).exists(), "test setup: index file missing"

        os.chmod(p, 0o000)
        try:
            r = run_read([p, "--outline"])
        finally:
            os.chmod(p, 0o644)

        assert r.returncode == 0, (
            f"--outline with a fresh index must not open the notebook; "
            f"stderr={r.stderr!r}"
        )
        assert "fastpath_marker" in r.stdout
        assert "run=3" in r.stdout
        assert "[STALE INDEX]" not in r.stderr

    def test_outline_fast_path_respects_filters(self, tmp_path):
        """--cells / --type filters must also apply on the index fast path."""
        p = _make_nb([
            {"cell_type": "code", "source": ["aaa = 1\n"], "execution_count": 1},
            {"cell_type": "markdown", "source": ["## bbb\n"]},
            {"cell_type": "code", "source": ["ccc = 3\n"], "execution_count": 2},
        ], tmp_path)
        r_idx = _run_index(p)
        assert r_idx.returncode == 0, f"nb-index.py failed: {r_idx.stderr!r}"

        r = run_read([p, "--outline", "--cells", "0-1", "--type", "code"])
        assert r.returncode == 0
        cell_lines = [l for l in r.stdout.splitlines() if l.startswith("[")]
        assert len(cell_lines) == 1, f"Expected 1 line, got:\n{r.stdout}"
        assert "aaa" in cell_lines[0]


# ---------------------------------------------------------------------------
# § Malformed index → warned fallback to the notebook
# ---------------------------------------------------------------------------

@pytest.mark.skipif(
    not INDEX_SCRIPT.exists(),
    reason="nb-index.py required for malformed-index tests",
)
class TestOutlineMalformedIndex:

    def _fresh_then_corrupt(self, tmp_path, corrupt):
        """Index a notebook, then mutate the index cells via corrupt(cells)
        (freshness metadata — notebook mtime/size — is preserved)."""
        p = _make_nb([
            {"cell_type": "code", "source": ["fallback_marker = 1\n"],
             "execution_count": 1},
        ], tmp_path)
        r_idx = _run_index(p)
        assert r_idx.returncode == 0, f"nb-index.py failed: {r_idx.stderr!r}"
        idx_file = _index_file_for(p)
        data = json.loads(idx_file.read_text(encoding="utf-8"))
        data["cells"] = corrupt(data["cells"])
        idx_file.write_text(json.dumps(data), encoding="utf-8")
        return p

    def test_index_cell_missing_i_falls_back(self, tmp_path):
        p = self._fresh_then_corrupt(
            tmp_path,
            lambda cells: [{k: v for k, v in c.items() if k != "i"} for c in cells],
        )
        r = run_read([p, "--outline"])
        assert r.returncode == 0, (
            f"Malformed index must fall back, not crash: stderr={r.stderr!r}"
        )
        assert "fallback_marker" in r.stdout
        assert "Traceback" not in r.stderr
        assert "MALFORMED INDEX" in r.stderr, (
            f"Expected a malformed-index warning on stderr, got: {r.stderr!r}"
        )

    def test_index_cells_not_dicts_falls_back(self, tmp_path):
        p = self._fresh_then_corrupt(tmp_path, lambda cells: ["bogus", 42])
        r = run_read([p, "--outline"])
        assert r.returncode == 0
        assert "fallback_marker" in r.stdout
        assert "MALFORMED INDEX" in r.stderr

    def test_index_cells_not_a_list_falls_back(self, tmp_path):
        p = self._fresh_then_corrupt(tmp_path, lambda cells: {"oops": 1})
        r = run_read([p, "--outline"])
        assert r.returncode == 0
        assert "fallback_marker" in r.stdout
        assert "MALFORMED INDEX" in r.stderr


class TestWorktreeGitFile:

    def test_outline_finds_index_when_git_is_a_file(self, tmp_path):
        """nb-read's index lookup must recognise a 'gitdir:' .git file as a
        repo root (synced from the canonical nb-index.py detection)."""
        import subprocess, sys as _sys
        repo = tmp_path / "wt"
        (repo / "sub").mkdir(parents=True)
        (repo / ".git").write_text("gitdir: /elsewhere\n", encoding="utf-8")
        nb_path = _make_nb([{"source": ["wt_first_line = 1\n"]}], repo / "sub")
        r = subprocess.run([_sys.executable, str(INDEX_SCRIPT), str(nb_path)],
                           capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        assert (repo / ".nb_index" / "sub").is_dir()
        out = run_read([str(nb_path), "--outline"])
        assert out.returncode == 0, out.stderr
        assert "wt_first_line" in out.stdout
