"""
Tests for nb-read.py --outputs mode (TDD_INDEX.md §10).

All tests are RED until nb-read.py implements --outputs.

The --outputs flag renders a '[output] ────' section after the source block
for every cell that has outputs, showing the actual output text (not just the
summary count). Cells with no outputs show no [output] section.

Key behaviours under test:
  - '[output]' header line appears after source for cells with outputs
  - Output text is rendered under the [output] header
  - Cells with outputs=[] produce no [output] section
  - Markdown cells produce no [output] section
  - ANSI sequences are stripped in safe mode (default); pass through with --no-safe
  - Error/traceback outputs are shown
  - Multiple cells each get their own [output] section
  - --cells filter applies normally
  - Falls back gracefully when no index exists

subprocess is used throughout so tests exercise the real CLI boundary.
"""

import json
import secrets
import string
import subprocess
import sys
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

class TestOutputsMode:

    def test_outputs_flag_accepted(self, tmp_path):
        """--outputs must exit 0 on a valid notebook."""
        p = _make_nb([
            {"cell_type": "code", "source": ["x = 1"], "outputs": [], "execution_count": 1},
        ], tmp_path)
        r = run_read([p, "--outputs"])
        assert r.returncode == 0, f"--outputs exited non-zero: stderr={r.stderr!r}"

    def test_outputs_section_shown_for_cell_with_output(self, tmp_path):
        """A cell with stream output must show a '[output]' section header in stdout."""
        p = _make_nb([{
            "cell_type": "code",
            "source": ["print('hello')"],
            "outputs": [{"output_type": "stream", "name": "stdout", "text": ["hello\n"]}],
            "execution_count": 1,
        }], tmp_path)
        r = run_read([p, "--outputs"])
        assert r.returncode == 0
        assert "[output]" in r.stdout, (
            f"Expected '[output]' section in stdout, got:\n{r.stdout}"
        )

    def test_outputs_section_not_shown_for_empty_cell(self, tmp_path):
        """A cell with outputs=[] must not produce a '[output]' section."""
        p = _make_nb([{
            "cell_type": "code",
            "source": ["x = 1"],
            "outputs": [],
            "execution_count": 1,
        }], tmp_path)
        r = run_read([p, "--outputs"])
        assert r.returncode == 0
        assert "[output]" not in r.stdout, (
            f"'[output]' section must not appear for empty outputs, got:\n{r.stdout}"
        )

    def test_outputs_text_rendered(self, tmp_path):
        """The actual output text must appear in stdout under the [output] section."""
        p = _make_nb([{
            "cell_type": "code",
            "source": ["print('result: 42')"],
            "outputs": [{"output_type": "stream", "name": "stdout",
                         "text": ["result: 42\n"]}],
            "execution_count": 1,
        }], tmp_path)
        r = run_read([p, "--outputs"])
        assert r.returncode == 0
        assert "result: 42" in r.stdout, (
            f"Expected output text 'result: 42' in stdout, got:\n{r.stdout}"
        )

    def test_outputs_ansi_stripped_in_safe_mode(self, tmp_path):
        """ANSI escape sequences in output text must be stripped in default safe mode."""
        ansi_output = "\x1b[31mred text\x1b[0m\n"
        p = _make_nb([{
            "cell_type": "code",
            "source": ["print(colored_text)"],
            "outputs": [{"output_type": "stream", "name": "stdout",
                         "text": [ansi_output]}],
            "execution_count": 1,
        }], tmp_path)
        r = run_read([p, "--outputs"])
        assert r.returncode == 0
        assert "\x1b" not in r.stdout, (
            f"ANSI escape leaked to stdout in safe mode: {r.stdout!r}"
        )
        assert "red text" in r.stdout, (
            f"Output text content ('red text') must be kept after ANSI strip: {r.stdout!r}"
        )

    def test_outputs_ansi_passes_with_no_safe(self, tmp_path):
        """ANSI sequences in output text must pass through unchanged with --no-safe."""
        ansi_output = "\x1b[31mred text\x1b[0m\n"
        p = _make_nb([{
            "cell_type": "code",
            "source": ["print(colored_text)"],
            "outputs": [{"output_type": "stream", "name": "stdout",
                         "text": [ansi_output]}],
            "execution_count": 1,
        }], tmp_path)
        r = run_read([p, "--outputs", "--no-safe"])
        assert r.returncode == 0
        assert "\x1b" in r.stdout, (
            f"ANSI should pass through with --no-safe, but stdout has no escapes: {r.stdout!r}"
        )

    def test_outputs_markdown_cell_no_output_section(self, tmp_path):
        """Markdown cells have no outputs field; no '[output]' section must appear."""
        p = _make_nb([
            {"cell_type": "markdown", "source": ["## Heading\n"]},
        ], tmp_path)
        r = run_read([p, "--outputs"])
        assert r.returncode == 0
        assert "[output]" not in r.stdout, (
            f"'[output]' must not appear for markdown cells, got:\n{r.stdout}"
        )

    def test_outputs_with_cells_filter(self, tmp_path):
        """--outputs --cells 0 must show only cell 0's output section."""
        p = _make_nb([
            {
                "cell_type": "code",
                "source": ["print('cell0')"],
                "outputs": [{"output_type": "stream", "name": "stdout",
                             "text": ["cell0\n"]}],
                "execution_count": 1,
            },
            {
                "cell_type": "code",
                "source": ["print('cell1')"],
                "outputs": [{"output_type": "stream", "name": "stdout",
                             "text": ["cell1\n"]}],
                "execution_count": 2,
            },
        ], tmp_path)
        r = run_read([p, "--outputs", "--cells", "0"])
        assert r.returncode == 0
        assert "cell0" in r.stdout, (
            f"Expected cell 0 output 'cell0' in stdout, got:\n{r.stdout}"
        )
        assert "cell1" not in r.stdout, (
            f"Cell 1 output 'cell1' must not appear with --cells 0, got:\n{r.stdout}"
        )

    def test_outputs_multiple_cells(self, tmp_path):
        """Multiple cells with outputs must each get their own [output] section."""
        p = _make_nb([
            {
                "cell_type": "code",
                "source": ["print('alpha')"],
                "outputs": [{"output_type": "stream", "name": "stdout",
                             "text": ["alpha\n"]}],
                "execution_count": 1,
            },
            {
                "cell_type": "code",
                "source": ["print('beta')"],
                "outputs": [{"output_type": "stream", "name": "stdout",
                             "text": ["beta\n"]}],
                "execution_count": 2,
            },
        ], tmp_path)
        r = run_read([p, "--outputs"])
        assert r.returncode == 0
        output_section_count = r.stdout.count("[output]")
        assert output_section_count == 2, (
            f"Expected 2 '[output]' sections for 2 cells with output, "
            f"got {output_section_count}:\n{r.stdout}"
        )
        assert "alpha" in r.stdout
        assert "beta" in r.stdout

    def test_outputs_error_traceback_shown(self, tmp_path):
        """Error outputs (output_type: error) must appear in the [output] section."""
        p = _make_nb([{
            "cell_type": "code",
            "source": ["raise ValueError('oops')"],
            "outputs": [{
                "output_type": "error",
                "ename": "ValueError",
                "evalue": "oops",
                "traceback": ["Traceback (most recent call last):", "ValueError: oops"],
            }],
            "execution_count": 1,
        }], tmp_path)
        r = run_read([p, "--outputs"])
        assert r.returncode == 0
        assert "[output]" in r.stdout, (
            f"Expected '[output]' section for error output, got:\n{r.stdout}"
        )
        assert "ValueError" in r.stdout, (
            f"Expected 'ValueError' in output text, got:\n{r.stdout}"
        )

    def test_outputs_binary_only_no_output_section(self, tmp_path):
        """A cell whose only output is binary (image/png) must not render an
        [output] section — binary outputs are not stored as text (§7.4)."""
        p = _make_nb([{
            "cell_type": "code",
            "source": ["plt.show()"],
            "outputs": [{
                "output_type": "display_data",
                "data": {"image/png": "iVBORw0KGgoAAAANSUhEUgAAAAUA"},
                "metadata": {},
            }],
            "execution_count": 1,
        }], tmp_path)
        r = run_read([p, "--outputs"])
        assert r.returncode == 0
        assert "[output]" not in r.stdout, (
            f"Binary-only output must not produce an [output] section, got:\n{r.stdout}"
        )
