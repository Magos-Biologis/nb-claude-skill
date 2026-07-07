"""
Tests for nb-read.py --outputs mode.

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

    def test_outputs_binary_only_renders_placeholder(self, tmp_path):
        """A cell whose only output is binary (image/png) must render an
        [output] section with a one-line placeholder naming the mime type —
        'no output' must be distinguishable from 'plot exists'."""
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
        assert "[output]" in r.stdout, (
            f"Binary-only output must produce an [output] section, got:\n{r.stdout}"
        )
        assert "image/png" in r.stdout, (
            f"Placeholder must name the mime type, got:\n{r.stdout}"
        )
        assert "not shown" in r.stdout, (
            f"Placeholder must say the output is not shown, got:\n{r.stdout}"
        )
        # base64 payload must never be printed
        assert "iVBORw0KGgo" not in r.stdout, (
            f"Binary payload must not be printed, got:\n{r.stdout}"
        )

    def test_outputs_placeholder_lists_multiple_mimes(self, tmp_path):
        """A non-text output with several mime types lists them comma-separated,
        richest first (image/png before text/html)."""
        p = _make_nb([{
            "cell_type": "code",
            "source": ["display(obj)"],
            "outputs": [{
                "output_type": "display_data",
                "data": {
                    "text/html": "<div>hi</div>",
                    "image/png": "iVBORw0KGgoAAAANSUhEUgAAAAUA",
                },
                "metadata": {},
            }],
            "execution_count": 1,
        }], tmp_path)
        r = run_read([p, "--outputs"])
        assert r.returncode == 0
        placeholder_lines = [l for l in r.stdout.splitlines()
                             if "not shown" in l]
        assert placeholder_lines, f"Expected a placeholder line, got:\n{r.stdout}"
        line = placeholder_lines[0]
        assert "image/png" in line and "text/html" in line, (
            f"Placeholder must list all mime types, got: {line!r}"
        )
        assert line.index("image/png") < line.index("text/html"), (
            f"Richest mime type must be listed first, got: {line!r}"
        )

    def test_outputs_mixed_text_and_image(self, tmp_path):
        """A cell with both a text output and an image-only output shows the
        text plus a placeholder for the image."""
        p = _make_nb([{
            "cell_type": "code",
            "source": ["print('stats'); plt.show()"],
            "outputs": [
                {"output_type": "stream", "name": "stdout",
                 "text": ["mean=3.5\n"]},
                {"output_type": "display_data",
                 "data": {"image/png": "iVBORw0KGgoAAAANSUhEUgAAAAUA"},
                 "metadata": {}},
            ],
            "execution_count": 1,
        }], tmp_path)
        r = run_read([p, "--outputs"])
        assert r.returncode == 0
        assert "mean=3.5" in r.stdout, (
            f"Text output must be rendered, got:\n{r.stdout}"
        )
        assert "image/png" in r.stdout and "not shown" in r.stdout, (
            f"Placeholder for the image output must be rendered, got:\n{r.stdout}"
        )

    def test_outputs_text_plain_preferred_over_placeholder(self, tmp_path):
        """An execute_result with both text/plain and image/png renders the
        text/plain repr (no placeholder needed for that output)."""
        p = _make_nb([{
            "cell_type": "code",
            "source": ["fig"],
            "outputs": [{
                "output_type": "execute_result",
                "data": {
                    "text/plain": "<Figure size 640x480>",
                    "image/png": "iVBORw0KGgoAAAANSUhEUgAAAAUA",
                },
                "metadata": {},
                "execution_count": 1,
            }],
            "execution_count": 1,
        }], tmp_path)
        r = run_read([p, "--outputs"])
        assert r.returncode == 0
        assert "<Figure size 640x480>" in r.stdout
        assert "not shown" not in r.stdout, (
            f"text/plain present: no placeholder expected, got:\n{r.stdout}"
        )

    def test_summary_counts_binary_only_output(self, tmp_path):
        """Normal-mode summary must agree with --outputs that a binary-only
        output exists (counts the placeholder line)."""
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
        r = run_read([p])
        assert r.returncode == 0
        assert "│ ── (1 output, 1 line" in r.stdout, (
            f"Summary must count the placeholder line for a binary-only "
            f"output, got:\n{r.stdout}"
        )


# ---------------------------------------------------------------------------
# § Output truncation
# ---------------------------------------------------------------------------

class TestOutputTruncation:

    def _long_output_nb(self, tmp_path, n_lines=100):
        text = "".join(f"line {i}\n" for i in range(n_lines))
        return _make_nb([{
            "cell_type": "code",
            "source": ["for i in range(100): print(f'line {i}')"],
            "outputs": [{"output_type": "stream", "name": "stdout",
                         "text": [text]}],
            "execution_count": 1,
        }], tmp_path)

    def test_outputs_truncated_at_n_lines_with_marker(self, tmp_path):
        """--truncate N caps each output block at N lines and appends a marker
        naming the cell index and how to see the full output."""
        p = self._long_output_nb(tmp_path, 100)
        r = run_read([p, "--outputs", "--truncate", "10"])
        assert r.returncode == 0
        assert "line 9" in r.stdout
        assert "line 10\n" not in r.stdout and "│ line 10" not in r.stdout, (
            f"Output must be cut at 10 lines, got:\n{r.stdout}"
        )
        assert "output truncated to 10 lines" in r.stdout, (
            f"Expected truncation marker, got:\n{r.stdout}"
        )
        assert "--outputs --cells 0 --truncate 0" in r.stdout, (
            f"Marker must explain how to see the full output, got:\n{r.stdout}"
        )

    def test_outputs_default_truncation_applies(self, tmp_path):
        """The default --truncate (80) must apply to output blocks too."""
        p = self._long_output_nb(tmp_path, 200)
        r = run_read([p, "--outputs"])
        assert r.returncode == 0
        assert "line 79" in r.stdout
        assert "│ line 150" not in r.stdout, (
            f"Default truncation (80 lines) must apply to outputs, got:\n{r.stdout}"
        )
        assert "output truncated to 80 lines" in r.stdout

    def test_outputs_truncate_zero_unlimited(self, tmp_path):
        """--truncate 0 must print the full output with no marker."""
        p = self._long_output_nb(tmp_path, 200)
        r = run_read([p, "--outputs", "--truncate", "0"])
        assert r.returncode == 0
        assert "line 199" in r.stdout, (
            f"--truncate 0 must print all output lines, got:\n{r.stdout}"
        )
        assert "output truncated" not in r.stdout

    def test_outputs_short_output_no_marker(self, tmp_path):
        """An output shorter than the truncation limit gets no marker."""
        p = self._long_output_nb(tmp_path, 5)
        r = run_read([p, "--outputs", "--truncate", "10"])
        assert r.returncode == 0
        assert "line 4" in r.stdout
        assert "output truncated" not in r.stdout

    def test_outputs_wide_line_capped(self, tmp_path):
        """A single output line wider than 2000 chars is capped with a
        '…[truncated]' suffix."""
        wide = "x" * 5000
        p = _make_nb([{
            "cell_type": "code",
            "source": ["print('x' * 5000)"],
            "outputs": [{"output_type": "stream", "name": "stdout",
                         "text": [wide + "\n"]}],
            "execution_count": 1,
        }], tmp_path)
        r = run_read([p, "--outputs"])
        assert r.returncode == 0
        assert "…[truncated]" in r.stdout, (
            f"Expected width-cap suffix on a 5000-char line, got first 200 "
            f"chars:\n{r.stdout[:200]}"
        )
        assert "x" * 5000 not in r.stdout, "5000-char line must not print in full"
        assert "x" * 2000 in r.stdout, "Capped line must keep the first 2000 chars"

    def test_source_lines_not_width_capped(self, tmp_path):
        """The 2000-char width cap applies to output lines only — wide source
        lines print untouched."""
        wide_src = "s = '" + "y" * 3000 + "'"
        p = _make_nb([{
            "cell_type": "code",
            "source": [wide_src],
            "outputs": [],
            "execution_count": 1,
        }], tmp_path)
        r = run_read([p, "--outputs"])
        assert r.returncode == 0
        assert "y" * 3000 in r.stdout, (
            "Source lines must not be width-capped"
        )


# ---------------------------------------------------------------------------
# § Empty text/plain, stream join, \r handling
# ---------------------------------------------------------------------------

class TestReviewFixes:

    def test_empty_text_plain_with_image_renders_placeholder(self, tmp_path):
        """data={'text/plain': '', 'image/png': ...} has no renderable text —
        the placeholder must appear (key presence is not enough)."""
        nb = _make_nb([{
            "source": ["plot()"],
            "outputs": [{
                "output_type": "display_data",
                "data": {"text/plain": "", "image/png": "aGVsbG8="},
                "metadata": {},
            }],
        }], tmp_path)
        r = run_read([nb, "--outputs"])
        assert r.returncode == 0
        assert "[image/png output — not shown]" in r.stdout
        assert "aGVsbG8=" not in r.stdout

    def test_empty_list_text_plain_with_image_renders_placeholder(self, tmp_path):
        nb = _make_nb([{
            "source": ["plot()"],
            "outputs": [{
                "output_type": "display_data",
                "data": {"text/plain": [], "image/png": "aGVsbG8="},
                "metadata": {},
            }],
        }], tmp_path)
        r = run_read([nb, "--outputs"])
        assert r.returncode == 0
        assert "[image/png output — not shown]" in r.stdout

    def test_consecutive_stream_chunks_join_on_one_line(self, tmp_path):
        """Jupyter stream semantics: chunks 'foo' + 'bar\\n' are one logical
        line 'foobar', not two lines."""
        nb = _make_nb([{
            "source": ["print('x', end='')"],
            "outputs": [
                {"output_type": "stream", "name": "stdout", "text": ["foo"]},
                {"output_type": "stream", "name": "stdout", "text": ["bar\n"]},
            ],
        }], tmp_path)
        r = run_read([nb, "--outputs"])
        assert r.returncode == 0
        assert "│ foobar" in r.stdout
        assert "│ foo\n" not in r.stdout

    def test_carriage_returns_do_not_create_lines(self, tmp_path):
        """\\r-overwritten progress frames must be stripped before splitting,
        not become line boundaries that explode the line count."""
        frames = "\r".join(f"progress {i}%" for i in range(50)) + "\ndone\n"
        nb = _make_nb([{
            "source": ["train()"],
            "outputs": [{"output_type": "stream", "name": "stdout", "text": [frames]}],
        }], tmp_path)
        r = run_read([nb, "--outputs", "--truncate", "10"])
        assert r.returncode == 0
        # All frames collapse onto one line; 'done' must still be visible,
        # not hidden behind a truncation marker.
        assert "done" in r.stdout
        assert "output truncated" not in r.stdout

    def test_placeholder_between_stream_chunks_flushes_buffer(self, tmp_path):
        """A placeholder output between two stream chunks must not glue the
        chunks across it."""
        nb = _make_nb([{
            "source": ["x"],
            "outputs": [
                {"output_type": "stream", "name": "stdout", "text": ["before\n"]},
                {"output_type": "display_data",
                 "data": {"image/png": "aGVsbG8="}, "metadata": {}},
                {"output_type": "stream", "name": "stdout", "text": ["after\n"]},
            ],
        }], tmp_path)
        r = run_read([nb, "--outputs"])
        assert r.returncode == 0
        out = r.stdout
        assert out.index("│ before") < out.index("[image/png output — not shown]") < out.index("│ after")


# ---------------------------------------------------------------------------
# § Summary derived from the renderer (single source of truth)
# ---------------------------------------------------------------------------

class TestSummaryMatchesRenderer:
    """The normal-mode '(N outputs, M lines)' summary must report exactly the
    number of lines that --outputs renders, for every output shape."""

    @staticmethod
    def _summary_lines(stdout):
        """Extract M from the '│ ── (N output(s), M lines) ──' summary."""
        import re
        m = re.search(r"│ ── \(\d+ outputs?, (\d+) lines\) ──", stdout)
        assert m, f"No summary line found in:\n{stdout}"
        return int(m.group(1))

    @staticmethod
    def _rendered_lines(stdout):
        """Count body lines of the [output] block (the '│ '-prefixed lines
        after the '[output]' header)."""
        lines = stdout.splitlines()
        start = next(i for i, l in enumerate(lines) if l.startswith("[output]"))
        n = 0
        for l in lines[start + 1:]:
            if l.startswith("│ "):
                n += 1
            else:
                break
        return n

    def _assert_agreement(self, p):
        r_normal = run_read([p])
        assert r_normal.returncode == 0
        r_out = run_read([p, "--outputs", "--truncate", "0"])
        assert r_out.returncode == 0
        summary = self._summary_lines(r_normal.stdout)
        rendered = self._rendered_lines(r_out.stdout)
        assert summary == rendered, (
            f"Summary says {summary} lines but --outputs renders {rendered}:\n"
            f"--- normal ---\n{r_normal.stdout}\n--- outputs ---\n{r_out.stdout}"
        )

    def test_execute_result_lines_counted(self, tmp_path):
        """execute_result text/plain must be counted (was '0 lines' before)."""
        p = _make_nb([{
            "cell_type": "code",
            "source": ["df"],
            "outputs": [{
                "output_type": "execute_result",
                "data": {"text/plain": "   a  b\n0  1  2\n1  3  4"},
                "metadata": {}, "execution_count": 1,
            }],
            "execution_count": 1,
        }], tmp_path)
        r = run_read([p])
        assert r.returncode == 0
        assert "│ ── (1 output, 3 lines) ──" in r.stdout, (
            f"execute_result must count its text/plain lines, got:\n{r.stdout}"
        )
        self._assert_agreement(p)

    def test_stream_partial_chunks_counted_as_one_line(self, tmp_path):
        """Two chunks forming one logical line count as 1 line, matching
        --outputs (which joins them)."""
        p = _make_nb([{
            "cell_type": "code",
            "source": ["print('x', end=''); print('y')"],
            "outputs": [
                {"output_type": "stream", "name": "stdout", "text": ["foo"]},
                {"output_type": "stream", "name": "stdout", "text": ["bar\n"]},
            ],
            "execution_count": 1,
        }], tmp_path)
        r = run_read([p])
        assert r.returncode == 0
        assert "│ ── (2 outputs, 1 lines) ──" in r.stdout, (
            f"Joined stream chunks must count as 1 line, got:\n{r.stdout}"
        )
        self._assert_agreement(p)

    def test_error_traceback_embedded_newlines_counted(self, tmp_path):
        """Traceback line count uses rendered lines, not list length."""
        p = _make_nb([{
            "cell_type": "code",
            "source": ["boom()"],
            "outputs": [{
                "output_type": "error", "ename": "E", "evalue": "v",
                "traceback": ["first\nsecond", "third"],
            }],
            "execution_count": 1,
        }], tmp_path)
        r = run_read([p])
        assert r.returncode == 0
        assert "│ ── (1 output, 3 lines) ──" in r.stdout, (
            f"Traceback with embedded newlines is 3 rendered lines, got:\n{r.stdout}"
        )
        self._assert_agreement(p)

    def test_placeholder_agreement(self, tmp_path):
        """Binary-only outputs: summary counts the placeholder line, same as
        --outputs renders."""
        p = _make_nb([{
            "cell_type": "code",
            "source": ["plt.show()"],
            "outputs": [{
                "output_type": "display_data",
                "data": {"image/png": "aGVsbG8="}, "metadata": {},
            }],
            "execution_count": 1,
        }], tmp_path)
        self._assert_agreement(p)

    def test_mixed_outputs_agreement(self, tmp_path):
        p = _make_nb([{
            "cell_type": "code",
            "source": ["mix()"],
            "outputs": [
                {"output_type": "stream", "name": "stdout", "text": ["a\nb\n"]},
                {"output_type": "display_data",
                 "data": {"image/png": "aGVsbG8="}, "metadata": {}},
                {"output_type": "execute_result",
                 "data": {"text/plain": "result"}, "metadata": {},
                 "execution_count": 1},
            ],
            "execution_count": 1,
        }], tmp_path)
        self._assert_agreement(p)
