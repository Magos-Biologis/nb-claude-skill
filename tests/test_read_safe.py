"""
Tests for nb-read.py safe-mode features:
  - Cell source sanitisation (ANSI, CSI, OSC)
  - │ line-prefix to prevent fake cell boundary injection
  - Robustness against non-string source values
  - Output summary line for code cells with outputs
  - --no-safe flag disables sanitisation

All tests red until the corresponding changes are made to nb-read.py.
"""

import json
import subprocess
import sys
import string
import secrets
from pathlib import Path

import pytest

SCRIPTS  = Path(__file__).parent.parent / "scripts"
NB_READ  = str(SCRIPTS / "nb-read.py")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cell_id():
    return "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(8))


def _make_nb(cells, tmp_path, name="test.ipynb"):
    nb = {
        "nbformat": 4, "nbformat_minor": 5,
        "metadata": {"kernelspec": {"name": "python3", "language": "python",
                                    "display_name": "Python 3"}},
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


def _make_raw_nb(nb_dict, tmp_path, name="raw.ipynb"):
    """Write a notebook dict directly (for malformed / unusual payloads)."""
    p = tmp_path / name
    p.write_text(json.dumps(nb_dict, indent=1), encoding="utf-8")
    return str(p)


def run_read(args, **kw):
    return subprocess.run(
        [sys.executable, NB_READ] + args,
        capture_output=True, text=True, **kw,
    )


# ---------------------------------------------------------------------------
# § Source line prefix
# ---------------------------------------------------------------------------

class TestSourceLinePrefix:

    def test_source_lines_prefixed_with_pipe(self, tmp_path):
        """Every non-empty source line must start with '│ ' in safe mode (default)."""
        p = _make_nb([{"cell_type": "code", "source": ["x = 1\n", "y = 2"]}], tmp_path)
        r = run_read([p])
        assert r.returncode == 0
        # Find source lines in output (those that are not cell headers or blank)
        content_lines = [l for l in r.stdout.splitlines()
                         if l.startswith("│ ")]
        assert content_lines, "Expected source lines prefixed with '│ '"
        assert any("x = 1" in l for l in content_lines)
        assert any("y = 2" in l for l in content_lines)

    def test_fake_cell_boundary_in_source_cannot_be_mistaken_for_header(self, tmp_path):
        """A source line that looks like a cell header must be prefixed, not bare."""
        fake_header = "[0:code] ────────────────────────────────────────────"
        p = _make_nb([{"cell_type": "markdown",
                        "source": [fake_header]}], tmp_path)
        r = run_read([p])
        assert r.returncode == 0
        lines = r.stdout.splitlines()
        # The fake header must appear only as a prefixed line, never as a bare line
        bare_matches = [l for l in lines if l == fake_header]
        assert not bare_matches, (
            f"Fake cell boundary appeared as bare line: {bare_matches}"
        )
        # It must appear prefixed
        prefixed = [l for l in lines if "│ " in l and fake_header in l]
        assert prefixed, "Fake cell boundary not found even as prefixed line"

    def test_empty_cell_marker_prefixed(self, tmp_path):
        """The '(empty)' marker for empty cells should be prefixed with '│ '."""
        p = _make_nb([{"cell_type": "code", "source": []}], tmp_path)
        r = run_read([p])
        assert r.returncode == 0
        assert "│ (empty)" in r.stdout

    def test_no_safe_disables_prefix(self, tmp_path):
        """--no-safe must disable the '│ ' prefix."""
        p = _make_nb([{"cell_type": "code", "source": ["x = 1"]}], tmp_path)
        r = run_read([p, "--no-safe"])
        assert r.returncode == 0
        # Source content present without prefix
        assert "x = 1" in r.stdout
        # No prefixed lines
        prefixed = [l for l in r.stdout.splitlines() if l.startswith("│ ")]
        assert not prefixed, "--no-safe must not prefix source lines"


# ---------------------------------------------------------------------------
# § ANSI / CSI / OSC sanitisation in cell source
# ---------------------------------------------------------------------------

class TestSourceAnsiSanitisation:

    def test_standard_csi_stripped_from_source(self, tmp_path):
        """Standard CSI colour codes must be stripped from cell source in safe mode."""
        p = _make_nb([{"cell_type": "code",
                        "source": ["\x1b[31mred text\x1b[0m"]}], tmp_path)
        r = run_read([p])
        assert r.returncode == 0
        assert "\x1b" not in r.stdout, "ANSI escape leaked to stdout"
        assert "red text" in r.stdout  # content kept, escape stripped

    def test_private_csi_stripped_from_source(self, tmp_path):
        """Private-mode CSI (e.g. \\x1b[?1049h alternate screen) must be stripped."""
        p = _make_nb([{"cell_type": "code",
                        "source": ["\x1b[?1049h\x1b[?25l"]}], tmp_path)
        r = run_read([p])
        assert r.returncode == 0
        assert "\x1b" not in r.stdout

    def test_osc_sequence_stripped_from_source(self, tmp_path):
        """OSC sequences (terminal title injection etc.) must be stripped."""
        # OSC: ESC ] ... BEL
        p = _make_nb([{"cell_type": "code",
                        "source": ["\x1b]0;TITLE INJECTION\x07normal text"]}], tmp_path)
        r = run_read([p])
        assert r.returncode == 0
        assert "\x1b" not in r.stdout
        assert "normal text" in r.stdout

    def test_no_safe_passes_ansi_through(self, tmp_path):
        """--no-safe must pass ANSI sequences through to stdout unchanged."""
        p = _make_nb([{"cell_type": "code",
                        "source": ["\x1b[31mred\x1b[0m"]}], tmp_path)
        r = run_read([p, "--no-safe"])
        assert r.returncode == 0
        assert "\x1b" in r.stdout

    def test_multiline_source_all_lines_sanitised(self, tmp_path):
        """ANSI on any line of multi-line source must be stripped."""
        p = _make_nb([{"cell_type": "code",
                        "source": ["clean line\n",
                                   "\x1b[1mbold\x1b[0m\n",
                                   "another clean\n"]}], tmp_path)
        r = run_read([p])
        assert r.returncode == 0
        assert "\x1b" not in r.stdout
        assert "clean line" in r.stdout
        assert "bold" in r.stdout
        assert "another clean" in r.stdout


# ---------------------------------------------------------------------------
# § Robustness: non-string source values
# ---------------------------------------------------------------------------

class TestNonStringSource:

    def _make_nb_raw_source(self, source_value, tmp_path):
        nb = {
            "nbformat": 4, "nbformat_minor": 5,
            "metadata": {"kernelspec": {"name": "python3", "language": "python",
                                        "display_name": "Python 3"}},
            "cells": [{
                "id": _cell_id(), "cell_type": "code",
                "metadata": {}, "source": source_value,
                "outputs": [], "execution_count": None,
            }],
        }
        return _make_raw_nb(nb, tmp_path)

    def test_source_int_does_not_crash(self, tmp_path):
        """source: 42 must render as '42' and exit 0, not crash."""
        p = self._make_nb_raw_source(42, tmp_path)
        r = run_read([p])
        assert r.returncode == 0, f"Crashed: stderr={r.stderr!r}"
        assert "42" in r.stdout

    def test_source_float_does_not_crash(self, tmp_path):
        """source: 3.14 must render and exit 0."""
        p = self._make_nb_raw_source(3.14, tmp_path)
        r = run_read([p])
        assert r.returncode == 0, f"Crashed: stderr={r.stderr!r}"
        assert "3.14" in r.stdout

    def test_source_null_renders_as_empty(self, tmp_path):
        """source: null must render as empty cell (not crash)."""
        p = self._make_nb_raw_source(None, tmp_path)
        r = run_read([p])
        assert r.returncode == 0, f"Crashed: stderr={r.stderr!r}"

    def test_source_list_with_int_does_not_crash(self, tmp_path):
        """source: ['line\n', 42, 'end'] must render without crash."""
        p = self._make_nb_raw_source(["line\n", 42, "end"], tmp_path)
        r = run_read([p])
        assert r.returncode == 0, f"Crashed: stderr={r.stderr!r}"
        assert "line" in r.stdout
        assert "42" in r.stdout
        assert "end" in r.stdout

    def test_no_partial_output_on_crash(self, tmp_path):
        """A bad source value must not emit a cell header then crash (partial output)."""
        p = self._make_nb_raw_source(42, tmp_path)
        r = run_read([p])
        # Either clean output or clean error — not a Python traceback on stdout
        assert "Traceback" not in r.stdout
        assert "AttributeError" not in r.stdout
        assert "TypeError" not in r.stdout


# ---------------------------------------------------------------------------
# § Output summary line
# ---------------------------------------------------------------------------

class TestOutputSummary:

    def test_output_summary_shown_for_code_cell_with_outputs(self, tmp_path):
        """A code cell with outputs must show a §2.6 canonical summary line after the source."""
        p = _make_nb([{
            "cell_type": "code",
            "source": ["print('hello')"],
            "outputs": [{"output_type": "stream", "name": "stdout",
                         "text": ["hello\n"]}],
        }], tmp_path)
        r = run_read([p])
        assert r.returncode == 0
        # Must use the canonical output-summary format: '│ ── (N output'
        assert "│ ── (1 output" in r.stdout, (
            f"Expected canonical summary '│ ── (1 output' in stdout, got:\n{r.stdout}"
        )

    def test_output_summary_shows_count_and_lines(self, tmp_path):
        """Summary must include the §2.6 canonical prefix and the count of output entries."""
        p = _make_nb([{
            "cell_type": "code",
            "source": ["for i in range(3): print(i)"],
            "outputs": [
                {"output_type": "stream", "name": "stdout", "text": ["0\n"]},
                {"output_type": "stream", "name": "stdout", "text": ["1\n"]},
                {"output_type": "stream", "name": "stdout", "text": ["2\n"]},
            ],
        }], tmp_path)
        r = run_read([p])
        assert r.returncode == 0
        # Must use the canonical prefix AND show the count
        assert "│ ── (3 output" in r.stdout, (
            f"Expected '│ ── (3 output' in stdout, got:\n{r.stdout}"
        )

    def test_output_summary_not_shown_for_empty_outputs(self, tmp_path):
        """A code cell with outputs=[] must not show a summary line."""
        p = _make_nb([{
            "cell_type": "code",
            "source": ["x = 1"],
            "outputs": [],
        }], tmp_path)
        r = run_read([p])
        assert r.returncode == 0
        # No output summary must appear. Check for the §2.6 canonical prefix
        # '│ ── (' — its absence confirms no summary was emitted regardless of
        # format.  (Checking the old non-conforming marker 'output(s)' would
        # silently pass even after a format regression.)
        assert "│ ── (" not in r.stdout

    def test_output_summary_not_shown_for_markdown_cell(self, tmp_path):
        """Markdown cells have no outputs field; no summary line must appear."""
        p = _make_nb([{"cell_type": "markdown", "source": ["## Heading"]}], tmp_path)
        r = run_read([p])
        assert r.returncode == 0
        # Check absence using the §2.6 canonical prefix so the assertion catches
        # any future format, not just the old non-conforming 'output(s)' wording.
        assert "│ ── (" not in r.stdout

    def test_output_summary_mentions_not_shown(self, tmp_path):
        """Summary line must use the §2.6 canonical '│ ── (' prefix format."""
        p = _make_nb([{
            "cell_type": "code",
            "source": ["print('x')"],
            "outputs": [{"output_type": "stream", "name": "stdout", "text": ["x\n"]}],
        }], tmp_path)
        r = run_read([p])
        assert r.returncode == 0
        # The canonical format is '│ ── (N outputs, M lines) ──'
        # Check that the summary line starts with the canonical prefix
        summary_lines = [l for l in r.stdout.splitlines() if l.startswith("│ ── (")]
        assert summary_lines, (
            f"Expected a summary line starting with '│ ── (' in stdout, got:\n{r.stdout}"
        )

    def test_error_output_counted_in_summary(self, tmp_path):
        """Traceback/error outputs must be counted in the summary."""
        p = _make_nb([{
            "cell_type": "code",
            "source": ["raise ValueError('oops')"],
            "outputs": [{
                "output_type": "error",
                "ename": "ValueError",
                "evalue": "oops",
                "traceback": ["Traceback...", "ValueError: oops"],
            }],
        }], tmp_path)
        r = run_read([p])
        assert r.returncode == 0
        # Use the canonical §2.6 prefix — "output" alone is too loose and
        # matches directory paths that may contain the word "output".
        assert "│ ── (1 output" in r.stdout, (
            f"Expected canonical §2.6 summary '│ ── (1 output' in stdout, got:\n{r.stdout}"
        )

    def test_no_safe_still_shows_output_summary(self, tmp_path):
        """--no-safe must not suppress the output summary (count must still appear)."""
        p = _make_nb([{
            "cell_type": "code",
            "source": ["print(1)"],
            "outputs": [{"output_type": "stream", "name": "stdout", "text": ["1\n"]}],
        }], tmp_path)
        r = run_read([p, "--no-safe"])
        assert r.returncode == 0
        # The spec says output summary is shown in both safe and no-safe modes.
        # Pin the canonical §2.6 prefix rather than the loose word "output" which
        # can match a directory name in the tmp path.
        assert "│ ── (1 output" in r.stdout, (
            f"--no-safe must still show §2.6 canonical summary, got:\n{r.stdout}"
        )


# ---------------------------------------------------------------------------
# § C0 control characters in cell source (safe mode)
# ---------------------------------------------------------------------------

class TestSourceControlCharSanitisation:
    """Safe mode strips C0 controls (BEL, backspace, \\r, ...) from source
    lines, but keeps tabs — \\t is legitimate in source code."""

    def test_bel_and_backspace_stripped_from_source(self, tmp_path):
        p = _make_nb([{"cell_type": "code",
                       "source": ["a\x07b\x08c"]}], tmp_path)
        r = run_read([p])
        assert r.returncode == 0
        assert "\x07" not in r.stdout, "BEL leaked to stdout in safe mode"
        assert "\x08" not in r.stdout, "backspace leaked to stdout in safe mode"
        assert "abc" in r.stdout, "content around controls must be kept"

    def test_carriage_return_in_source_removed_not_line_boundary(self, tmp_path):
        """A lone \\r inside a source line is removed before splitting, so it
        can neither overwrite rendered text nor create a fake bare line."""
        p = _make_nb([{"cell_type": "code",
                       "source": ["left\rright"]}], tmp_path)
        r = run_read([p])
        assert r.returncode == 0
        assert "leftright" in r.stdout, (
            f"\\r must be stripped (joining the parts), got:\n{r.stdout!r}"
        )

    def test_tab_survives_in_source(self, tmp_path):
        p = _make_nb([{"cell_type": "code",
                       "source": ["if x:\n", "\tprint(1)"]}], tmp_path)
        r = run_read([p])
        assert r.returncode == 0
        assert "\tprint(1)" in r.stdout, (
            f"Tabs are legitimate in source and must survive safe mode:\n{r.stdout!r}"
        )

    def test_vertical_tab_and_formfeed_stripped(self, tmp_path):
        p = _make_nb([{"cell_type": "code",
                       "source": ["x\x0by\x0cz"]}], tmp_path)
        r = run_read([p])
        assert r.returncode == 0
        assert "\x0b" not in r.stdout
        assert "\x0c" not in r.stdout
        assert "xyz" in r.stdout

    def test_no_safe_passes_controls_through(self, tmp_path):
        p = _make_nb([{"cell_type": "code",
                       "source": ["a\x07b"]}], tmp_path)
        r = run_read([p, "--no-safe"])
        assert r.returncode == 0
        assert "\x07" in r.stdout, "--no-safe must pass C0 controls through"
