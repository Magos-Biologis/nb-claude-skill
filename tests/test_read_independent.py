"""
Independent test suite for nb-read.py, derived purely from the specification.

Tests are grouped into classes by behaviour domain. Each test uses subprocess.run
to invoke the script as a black-box CLI tool.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = str(Path(__file__).parent.parent / "scripts" / "nb-read.py")


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def make_notebook(cells, tmp_path, *, kernel_name="python3", include_cells_key=True):
    """
    Build a valid nbformat 4 notebook dict and write it to a .ipynb file.

    Parameters
    ----------
    cells : list[dict]
        Each dict must have at minimum 'cell_type' and 'source'.
        Defaults (id, metadata, outputs, execution_count) are filled in
        automatically for code cells.
    tmp_path : Path
        Temporary directory supplied by the pytest fixture.
    kernel_name : str
        Kernel name embedded in the notebook metadata.
    include_cells_key : bool
        Set to False to produce a notebook missing the top-level 'cells' key.
    """
    normalised = []
    for i, cell in enumerate(cells):
        c = {
            "id": f"cell{i:04x}",
            "cell_type": cell.get("cell_type", "code"),
            "metadata": cell.get("metadata", {}),
            "source": cell.get("source", ""),
        }
        if c["cell_type"] == "code":
            c.setdefault("outputs", cell.get("outputs", []))
            c.setdefault("execution_count", cell.get("execution_count", None))
        normalised.append(c)

    nb = {
        "nbformat": 4,
        "nbformat_minor": 5,
        "metadata": {
            "kernelspec": {
                "name": kernel_name,
                "language": "python",
                "display_name": "Python 3",
            }
        },
    }
    if include_cells_key:
        nb["cells"] = normalised

    path = tmp_path / "notebook.ipynb"
    path.write_text(json.dumps(nb), encoding="utf-8")
    return path


def run(args, **kwargs):
    """Run nb-read.py with the given argument list and return CompletedProcess."""
    return subprocess.run(
        [sys.executable, SCRIPT] + args,
        capture_output=True,
        text=True,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Class 1 – Basic output format
# ---------------------------------------------------------------------------

class TestBasicOutputFormat:
    """Verify the structural shape of stdout for a well-formed notebook."""

    def test_header_line_present(self, tmp_path):
        """The first stdout line must contain the filename, cell count, and kernel name."""
        nb = make_notebook(
            [{"cell_type": "code", "source": "x = 1"}],
            tmp_path,
            kernel_name="python3",
        )
        result = run([str(nb)])
        assert result.returncode == 0
        first_line = result.stdout.splitlines()[0]
        assert "notebook.ipynb" in first_line
        assert "1 cells" in first_line or "1 cell" in first_line
        assert "python3" in first_line

    def test_header_pipe_separated(self, tmp_path):
        """Header fields are separated by ' | '."""
        nb = make_notebook(
            [{"cell_type": "code", "source": "pass"}],
            tmp_path,
        )
        result = run([str(nb)])
        assert result.returncode == 0
        first_line = result.stdout.splitlines()[0]
        assert "|" in first_line

    def test_cell_header_format(self, tmp_path):
        """Each cell starts with a [index:cell_type] header line."""
        nb = make_notebook(
            [{"cell_type": "code", "source": "x = 1"}],
            tmp_path,
        )
        result = run([str(nb)])
        assert result.returncode == 0
        assert "[0:code:run=" in result.stdout

    def test_cell_source_appears_in_output(self, tmp_path):
        """The cell's source text must appear in stdout."""
        nb = make_notebook(
            [{"cell_type": "code", "source": "print('hello world')"}],
            tmp_path,
        )
        result = run([str(nb)])
        assert result.returncode == 0
        assert "print('hello world')" in result.stdout

    def test_multiple_cells_indexed(self, tmp_path):
        """Multiple cells are each shown with their 0-based index."""
        nb = make_notebook(
            [
                {"cell_type": "code", "source": "a = 1"},
                {"cell_type": "markdown", "source": "# Title"},
                {"cell_type": "code", "source": "b = 2"},
            ],
            tmp_path,
        )
        result = run([str(nb)])
        assert result.returncode == 0
        assert "[0:code:run=" in result.stdout
        assert "[1:markdown]" in result.stdout
        assert "[2:code:run=" in result.stdout

    def test_cell_header_contains_dashes(self, tmp_path):
        """Cell header lines include a visual separator (dashes)."""
        nb = make_notebook(
            [{"cell_type": "code", "source": "pass"}],
            tmp_path,
        )
        result = run([str(nb)])
        assert result.returncode == 0
        # At least several dashes on the same line as the cell header
        lines = result.stdout.splitlines()
        header_lines = [l for l in lines if "[0:code:run=" in l]
        assert header_lines, "No cell header line found"
        assert "─" in header_lines[0] or "-" in header_lines[0] or "─" in header_lines[0]

    def test_empty_notebook_header(self, tmp_path):
        """A notebook with zero cells still prints a valid header."""
        nb = make_notebook([], tmp_path)
        result = run([str(nb)])
        assert result.returncode == 0
        first_line = result.stdout.splitlines()[0]
        assert "0 cells" in first_line or "0 cell" in first_line

    def test_nothing_on_stderr_for_clean_notebook(self, tmp_path):
        """A clean, small notebook should produce nothing on stderr."""
        nb = make_notebook(
            [{"cell_type": "code", "source": "x = 1"}],
            tmp_path,
        )
        result = run([str(nb)])
        assert result.returncode == 0
        assert result.stderr == ""

    def test_header_shows_total_cell_count(self, tmp_path):
        """Header reports total cell count, not filtered count."""
        nb = make_notebook(
            [
                {"cell_type": "code", "source": "a = 1"},
                {"cell_type": "code", "source": "b = 2"},
                {"cell_type": "markdown", "source": "text"},
            ],
            tmp_path,
        )
        result = run([str(nb)])
        assert result.returncode == 0
        first_line = result.stdout.splitlines()[0]
        assert "3 cells" in first_line or "3 cell" in first_line


# ---------------------------------------------------------------------------
# Class 2 – Source format variants
# ---------------------------------------------------------------------------

class TestSourceFormatVariants:
    """Verify that 'source' as a list of strings and as a plain string both work."""

    def test_source_as_list_of_strings(self, tmp_path):
        """source given as a list of strings is joined and displayed correctly."""
        nb = make_notebook(
            [{"cell_type": "code", "source": ["line1\n", "line2\n", "line3"]}],
            tmp_path,
        )
        result = run([str(nb)])
        assert result.returncode == 0
        assert "line1" in result.stdout
        assert "line2" in result.stdout
        assert "line3" in result.stdout

    def test_source_as_single_string(self, tmp_path):
        """source given as a single string is displayed correctly."""
        nb = make_notebook(
            [{"cell_type": "code", "source": "line1\nline2\nline3"}],
            tmp_path,
        )
        result = run([str(nb)])
        assert result.returncode == 0
        assert "line1" in result.stdout
        assert "line2" in result.stdout
        assert "line3" in result.stdout

    def test_empty_source_string(self, tmp_path):
        """A cell with empty string source does not crash."""
        nb = make_notebook(
            [{"cell_type": "code", "source": ""}],
            tmp_path,
        )
        result = run([str(nb)])
        assert result.returncode == 0
        assert "[0:code:run=" in result.stdout

    def test_empty_source_list(self, tmp_path):
        """A cell with empty list source does not crash."""
        nb = make_notebook(
            [{"cell_type": "code", "source": []}],
            tmp_path,
        )
        result = run([str(nb)])
        assert result.returncode == 0

    def test_source_list_preserves_content(self, tmp_path):
        """Multi-line source list content is fully present in output."""
        lines = ["import os\n", "import sys\n", "print(os.getcwd())"]
        nb = make_notebook(
            [{"cell_type": "code", "source": lines}],
            tmp_path,
        )
        result = run([str(nb)])
        assert result.returncode == 0
        for line in lines:
            assert line.rstrip() in result.stdout


# ---------------------------------------------------------------------------
# Class 3 – --cells option
# ---------------------------------------------------------------------------

class TestCellsOption:
    """Verify --cells N, N-M, and N,M,K filtering."""

    def _three_cell_nb(self, tmp_path):
        return make_notebook(
            [
                {"cell_type": "code", "source": "cell_zero"},
                {"cell_type": "markdown", "source": "cell_one"},
                {"cell_type": "code", "source": "cell_two"},
            ],
            tmp_path,
        )

    def test_single_index(self, tmp_path):
        """--cells N shows only cell at index N."""
        nb = self._three_cell_nb(tmp_path)
        result = run([str(nb), "--cells", "1"])
        assert result.returncode == 0
        assert "cell_one" in result.stdout
        assert "cell_zero" not in result.stdout
        assert "cell_two" not in result.stdout

    def test_range_inclusive(self, tmp_path):
        """--cells N-M shows cells N through M inclusive."""
        nb = self._three_cell_nb(tmp_path)
        result = run([str(nb), "--cells", "0-1"])
        assert result.returncode == 0
        assert "cell_zero" in result.stdout
        assert "cell_one" in result.stdout
        assert "cell_two" not in result.stdout

    def test_range_full(self, tmp_path):
        """--cells 0-2 shows all three cells of a 3-cell notebook."""
        nb = self._three_cell_nb(tmp_path)
        result = run([str(nb), "--cells", "0-2"])
        assert result.returncode == 0
        assert "cell_zero" in result.stdout
        assert "cell_one" in result.stdout
        assert "cell_two" in result.stdout

    def test_comma_list(self, tmp_path):
        """--cells N,M shows cells at those specific indices."""
        nb = self._three_cell_nb(tmp_path)
        result = run([str(nb), "--cells", "0,2"])
        assert result.returncode == 0
        assert "cell_zero" in result.stdout
        assert "cell_two" in result.stdout
        assert "cell_one" not in result.stdout

    def test_comma_list_three_indices(self, tmp_path):
        """--cells N,M,K shows all three specified cells."""
        nb = self._three_cell_nb(tmp_path)
        result = run([str(nb), "--cells", "0,1,2"])
        assert result.returncode == 0
        assert "cell_zero" in result.stdout
        assert "cell_one" in result.stdout
        assert "cell_two" in result.stdout

    def test_single_first_cell(self, tmp_path):
        """--cells 0 shows only the first cell."""
        nb = self._three_cell_nb(tmp_path)
        result = run([str(nb), "--cells", "0"])
        assert result.returncode == 0
        assert "cell_zero" in result.stdout
        assert "cell_one" not in result.stdout

    def test_single_last_cell(self, tmp_path):
        """--cells pointing to the last cell works correctly."""
        nb = self._three_cell_nb(tmp_path)
        result = run([str(nb), "--cells", "2"])
        assert result.returncode == 0
        assert "cell_two" in result.stdout
        assert "cell_zero" not in result.stdout

    def test_negative_index_errors(self, tmp_path):
        """Negative indices in --cells produce a non-zero exit code."""
        nb = self._three_cell_nb(tmp_path)
        result = run([str(nb), "--cells", "-1"])
        assert result.returncode != 0

    def test_non_integer_cells_errors(self, tmp_path):
        """Non-integer value in --cells produces a clean error, not a traceback."""
        nb = self._three_cell_nb(tmp_path)
        result = run([str(nb), "--cells", "abc"])
        assert result.returncode != 0
        # Must not dump a raw Python traceback
        combined = result.stdout + result.stderr
        assert "Traceback" not in combined

    def test_giant_range_errors(self, tmp_path):
        """--cells 0-9999999 on a small notebook errors or is guarded, not hangs."""
        nb = self._three_cell_nb(tmp_path)
        result = run([str(nb), "--cells", "0-9999999"], timeout=10)
        # Must return in reasonable time; exit code should be non-zero
        assert result.returncode != 0

    def test_out_of_range_index_produces_no_cells(self, tmp_path):
        """An index that exceeds the cell count results in no cells shown.

        The spec does not explicitly require a non-zero exit for an out-of-range
        positive index; the implementation returns 0 with an empty cell list.
        This test verifies that no cell content from the notebook leaks out.
        """
        nb = self._three_cell_nb(tmp_path)
        result = run([str(nb), "--cells", "99"])
        # Whether it errors or returns 0 with empty output is an implementation
        # choice not fully specified. At minimum, no cell sources should appear.
        assert "cell_zero" not in result.stdout
        assert "cell_one" not in result.stdout
        assert "cell_two" not in result.stdout

    def test_range_with_equal_bounds(self, tmp_path):
        """--cells N-N (same start and end) shows exactly that one cell."""
        nb = self._three_cell_nb(tmp_path)
        result = run([str(nb), "--cells", "1-1"])
        assert result.returncode == 0
        assert "cell_one" in result.stdout
        assert "cell_zero" not in result.stdout
        assert "cell_two" not in result.stdout

    def test_inverted_range_errors(self, tmp_path):
        """--cells M-N where M > N (inverted range) should error."""
        nb = self._three_cell_nb(tmp_path)
        result = run([str(nb), "--cells", "2-0"])
        assert result.returncode != 0

    def test_float_cells_value_errors(self, tmp_path):
        """Floating-point value in --cells should produce a clean error."""
        nb = self._three_cell_nb(tmp_path)
        result = run([str(nb), "--cells", "1.5"])
        assert result.returncode != 0
        combined = result.stdout + result.stderr
        assert "Traceback" not in combined


# ---------------------------------------------------------------------------
# Class 4 – --type filter
# ---------------------------------------------------------------------------

class TestTypeFilter:
    """Verify --type code|markdown|raw filtering."""

    def _mixed_nb(self, tmp_path):
        return make_notebook(
            [
                {"cell_type": "code", "source": "code_cell"},
                {"cell_type": "markdown", "source": "markdown_cell"},
                {"cell_type": "raw", "source": "raw_cell"},
                {"cell_type": "code", "source": "another_code_cell"},
            ],
            tmp_path,
        )

    def test_type_code_only(self, tmp_path):
        """--type code shows only code cells."""
        nb = self._mixed_nb(tmp_path)
        result = run([str(nb), "--type", "code"])
        assert result.returncode == 0
        assert "code_cell" in result.stdout
        assert "another_code_cell" in result.stdout
        assert "markdown_cell" not in result.stdout
        assert "raw_cell" not in result.stdout

    def test_type_markdown_only(self, tmp_path):
        """--type markdown shows only markdown cells."""
        nb = self._mixed_nb(tmp_path)
        result = run([str(nb), "--type", "markdown"])
        assert result.returncode == 0
        assert "markdown_cell" in result.stdout
        assert "code_cell" not in result.stdout
        assert "raw_cell" not in result.stdout

    def test_type_raw_only(self, tmp_path):
        """--type raw shows only raw cells."""
        nb = self._mixed_nb(tmp_path)
        result = run([str(nb), "--type", "raw"])
        assert result.returncode == 0
        assert "raw_cell" in result.stdout
        assert "code_cell" not in result.stdout
        assert "markdown_cell" not in result.stdout

    def test_type_filter_with_no_matching_cells(self, tmp_path):
        """--type raw on a notebook with no raw cells returns exit 0 and empty body."""
        nb = make_notebook(
            [{"cell_type": "code", "source": "x = 1"}],
            tmp_path,
        )
        result = run([str(nb), "--type", "raw"])
        assert result.returncode == 0

    def test_type_filter_invalid_value(self, tmp_path):
        """--type with an unrecognised value should produce a non-zero exit code."""
        nb = make_notebook(
            [{"cell_type": "code", "source": "x = 1"}],
            tmp_path,
        )
        result = run([str(nb), "--type", "python"])
        assert result.returncode != 0


# ---------------------------------------------------------------------------
# Class 5 – --truncate option
# ---------------------------------------------------------------------------

class TestTruncate:
    """Verify --truncate N line-truncation behaviour."""

    def _long_cell_nb(self, tmp_path, num_lines=100):
        source = "\n".join(f"line_{i}" for i in range(num_lines))
        return make_notebook(
            [{"cell_type": "code", "source": source}],
            tmp_path,
        )

    def test_truncate_limits_lines_in_stdout(self, tmp_path):
        """--truncate 5 on a 100-line cell shows at most ~5 source lines per cell."""
        nb = self._long_cell_nb(tmp_path, num_lines=100)
        result = run([str(nb), "--truncate", "5"])
        assert result.returncode == 0
        # line_50 should NOT appear if truncated at 5
        assert "line_50" not in result.stdout

    def test_truncate_zero_unlimited(self, tmp_path):
        """--truncate 0 means unlimited; all lines must appear."""
        nb = self._long_cell_nb(tmp_path, num_lines=100)
        result = run([str(nb), "--truncate", "0"])
        assert result.returncode == 0
        assert "line_99" in result.stdout

    def test_truncate_warning_on_stderr_not_stdout(self, tmp_path):
        """Truncation notice/warning must appear on stderr, never stdout.

        The spec is explicit: 'Truncation warnings/notices go to stderr only —
        they must never appear in stdout'. We use a notebook whose cell source
        contains only 'line_N' patterns, so any truncation diagnostic that leaks
        to stdout is easy to identify (it won't match 'line_\\d+').
        """
        nb = self._long_cell_nb(tmp_path, num_lines=200)
        result = run([str(nb), "--truncate", "3"])
        assert result.returncode == 0
        # Build a set of lines that look like a truncation diagnostic:
        # they contain "truncat" (case-insensitive), are not empty, and are not
        # a notebook path header line or a source line ("line_NNN" pattern).
        import re
        stdout_lines = result.stdout.splitlines()
        diagnostic_lines = [
            l for l in stdout_lines
            if "truncat" in l.lower()
            and not re.search(r"\bline_\d+\b", l)                # not source content
            and not l.strip().startswith("/")                     # not a POSIX path/header
            and not re.match(r"^[A-Za-z]:[/\\]", l.strip())     # not a Windows path/header
            and not l.strip().startswith("[")                     # not a cell header
        ]
        assert diagnostic_lines == [], (
            f"Truncation message leaked to stdout: {diagnostic_lines}"
        )

    def test_default_truncate_is_80(self, tmp_path):
        """Default truncation is 80 lines; line 81 should not appear."""
        source = "\n".join(f"line_{i}" for i in range(200))
        nb = make_notebook(
            [{"cell_type": "code", "source": source}],
            tmp_path,
        )
        result = run([str(nb)])
        assert result.returncode == 0
        assert "line_80" not in result.stdout
        assert "line_0" in result.stdout

    def test_truncate_negative_errors(self, tmp_path):
        """--truncate -1 should produce a non-zero exit code.

        The spec documents --truncate N with N=0 meaning unlimited. Negative
        values are not defined and imply an error per the spec's intent that N
        is a line count. NOTE: the current implementation treats -1 as unlimited
        (exit 0) — this test documents the spec-vs-implementation gap.
        """
        nb = self._long_cell_nb(tmp_path)
        result = run([str(nb), "--truncate", "-1"])
        # Per spec, negative N is not valid; should exit non-zero.
        # Current implementation exits 0 — this is a known gap.
        assert result.returncode != 0, (
            "SPEC GAP: --truncate -1 exits 0 (implementation treats it as "
            "unlimited), but the spec implies only 0 means unlimited."
        )

    def test_truncate_non_integer_errors(self, tmp_path):
        """--truncate with a non-integer value produces a clean error."""
        nb = self._long_cell_nb(tmp_path)
        result = run([str(nb), "--truncate", "abc"])
        assert result.returncode != 0
        combined = result.stdout + result.stderr
        assert "Traceback" not in combined


# ---------------------------------------------------------------------------
# Class 6 – Error handling and edge cases
# ---------------------------------------------------------------------------

class TestErrorHandling:
    """Verify correct behaviour for invalid inputs and edge cases."""

    def test_wrong_extension_errors(self, tmp_path):
        """Passing a .txt file (not .ipynb) should exit non-zero."""
        txt = tmp_path / "notebook.txt"
        txt.write_text("{}", encoding="utf-8")
        result = run([str(txt)])
        assert result.returncode != 0

    def test_no_extension_errors(self, tmp_path):
        """Passing a file without .ipynb extension exits non-zero."""
        f = tmp_path / "notebook"
        f.write_text("{}", encoding="utf-8")
        result = run([str(f)])
        assert result.returncode != 0

    def test_missing_file_errors(self, tmp_path):
        """Non-existent file path exits non-zero."""
        result = run([str(tmp_path / "ghost.ipynb")])
        assert result.returncode != 0

    def test_no_arguments_errors(self):
        """Invoking the script with no arguments exits non-zero."""
        result = run([])
        assert result.returncode != 0

    def test_invalid_json_errors(self, tmp_path):
        """A .ipynb file containing invalid JSON exits non-zero."""
        bad = tmp_path / "bad.ipynb"
        bad.write_text("this is not json", encoding="utf-8")
        result = run([str(bad)])
        assert result.returncode != 0

    def test_missing_cells_key_errors(self, tmp_path):
        """A notebook missing the top-level 'cells' key exits non-zero."""
        nb = make_notebook([], tmp_path, include_cells_key=False)
        result = run([str(nb)])
        assert result.returncode != 0

    def test_utf8_bom_handled(self, tmp_path):
        """A notebook file with a UTF-8 BOM is processed without error."""
        nb_dict = {
            "nbformat": 4,
            "nbformat_minor": 5,
            "metadata": {
                "kernelspec": {"name": "python3", "language": "python", "display_name": "Python 3"}
            },
            "cells": [
                {
                    "id": "bom_cell",
                    "cell_type": "code",
                    "metadata": {},
                    "source": "x = 1",
                    "outputs": [],
                    "execution_count": None,
                }
            ],
        }
        nb_path = tmp_path / "bom_notebook.ipynb"
        # Write with UTF-8 BOM
        nb_path.write_bytes(b"\xef\xbb\xbf" + json.dumps(nb_dict).encode("utf-8"))
        result = run([str(nb_path)])
        assert result.returncode == 0
        assert "x = 1" in result.stdout

    def test_errors_reported_not_silently_swallowed(self, tmp_path):
        """Any error produces output on stderr or stdout — never silent."""
        bad = tmp_path / "bad.ipynb"
        bad.write_text("not json", encoding="utf-8")
        result = run([str(bad)])
        assert result.returncode != 0
        assert result.stderr != "" or result.stdout != ""

    def test_directory_path_errors(self, tmp_path):
        """Passing a directory path (not a file) exits non-zero."""
        # Create a directory ending in .ipynb (unusual but worth testing)
        d = tmp_path / "notebook.ipynb"
        d.mkdir()
        result = run([str(d)])
        assert result.returncode != 0

    def test_wrong_extension_py_errors(self, tmp_path):
        """Passing a .py file exits non-zero even if it contains valid JSON."""
        f = tmp_path / "script.py"
        f.write_text("{}", encoding="utf-8")
        result = run([str(f)])
        assert result.returncode != 0

    def test_file_over_100mb_errors(self, tmp_path):
        """A file over 100 MB should be refused with a non-zero exit code."""
        big = tmp_path / "big.ipynb"
        # Write a minimal valid notebook header, then pad to ~101 MB
        # We fake it: write a real notebook with padding in a cell
        nb_dict = {
            "nbformat": 4,
            "nbformat_minor": 5,
            "metadata": {
                "kernelspec": {"name": "python3", "language": "python", "display_name": "Python 3"}
            },
            "cells": [
                {
                    "id": "fat_cell",
                    "cell_type": "code",
                    "metadata": {},
                    "source": "x" * (101 * 1024 * 1024),
                    "outputs": [],
                    "execution_count": None,
                }
            ],
        }
        big.write_text(json.dumps(nb_dict), encoding="utf-8")
        result = run([str(big)], timeout=30)
        assert result.returncode != 0


# ---------------------------------------------------------------------------
# Class 7 – Kernel name display
# ---------------------------------------------------------------------------

class TestKernelDisplay:
    """Verify that the kernel name from metadata is shown in the header."""

    def test_custom_kernel_name_in_header(self, tmp_path):
        """A notebook with kernel 'ir' shows 'ir' in the header."""
        nb = make_notebook(
            [{"cell_type": "code", "source": "1 + 1"}],
            tmp_path,
            kernel_name="ir",
        )
        result = run([str(nb)])
        assert result.returncode == 0
        assert "ir" in result.stdout.splitlines()[0]

    def test_missing_kernelspec_still_works(self, tmp_path):
        """A notebook without a kernelspec in metadata should not crash."""
        nb_dict = {
            "nbformat": 4,
            "nbformat_minor": 5,
            "metadata": {},
            "cells": [
                {
                    "id": "cell0",
                    "cell_type": "code",
                    "metadata": {},
                    "source": "pass",
                    "outputs": [],
                    "execution_count": None,
                }
            ],
        }
        nb_path = tmp_path / "nokern.ipynb"
        nb_path.write_text(json.dumps(nb_dict), encoding="utf-8")
        result = run([str(nb_path)])
        assert result.returncode == 0


# ---------------------------------------------------------------------------
# Class 8 – Combined options
# ---------------------------------------------------------------------------

class TestCombinedOptions:
    """Verify that --cells and --type can be combined, and --cells + --truncate."""

    def _five_cell_nb(self, tmp_path):
        return make_notebook(
            [
                {"cell_type": "code", "source": "cell0_code"},
                {"cell_type": "markdown", "source": "cell1_md"},
                {"cell_type": "code", "source": "cell2_code"},
                {"cell_type": "markdown", "source": "cell3_md"},
                {"cell_type": "code", "source": "cell4_code"},
            ],
            tmp_path,
        )

    def test_cells_and_type_combined(self, tmp_path):
        """--cells 0-2 --type code shows only code cells within that range."""
        nb = self._five_cell_nb(tmp_path)
        result = run([str(nb), "--cells", "0-2", "--type", "code"])
        assert result.returncode == 0
        assert "cell0_code" in result.stdout
        assert "cell2_code" in result.stdout
        assert "cell1_md" not in result.stdout
        assert "cell3_md" not in result.stdout
        assert "cell4_code" not in result.stdout

    def test_cells_range_and_truncate(self, tmp_path):
        """--cells 0-1 --truncate 2 limits output correctly."""
        source_long = "\n".join(f"line_{i}" for i in range(50))
        nb = make_notebook(
            [
                {"cell_type": "code", "source": source_long},
                {"cell_type": "code", "source": source_long},
                {"cell_type": "code", "source": "untouched_cell"},
            ],
            tmp_path,
        )
        result = run([str(nb), "--cells", "0-1", "--truncate", "2"])
        assert result.returncode == 0
        assert "untouched_cell" not in result.stdout
        assert "line_49" not in result.stdout

    def test_single_cell_comma_list_deduplication(self, tmp_path):
        """--cells 1,1 should not show cell 1 twice (or at least not crash)."""
        nb = self._five_cell_nb(tmp_path)
        result = run([str(nb), "--cells", "1,1"])
        # Must not crash
        assert result.returncode == 0


# ---------------------------------------------------------------------------
# Class 9 – Unicode and special content
# ---------------------------------------------------------------------------

class TestUnicodeAndSpecialContent:
    """Verify correct handling of non-ASCII characters in cell sources."""

    def test_unicode_source_preserved(self, tmp_path):
        """Source with Unicode characters is displayed correctly."""
        nb = make_notebook(
            [{"cell_type": "markdown", "source": "# 日本語テスト\nこんにちは世界"}],
            tmp_path,
        )
        result = run([str(nb)])
        assert result.returncode == 0
        assert "日本語" in result.stdout

    def test_emoji_in_source(self, tmp_path):
        """Source containing emoji characters is handled without error."""
        nb = make_notebook(
            [{"cell_type": "markdown", "source": "# Hello 🌍\nGood morning 😀"}],
            tmp_path,
        )
        result = run([str(nb)])
        assert result.returncode == 0

    def test_source_with_backslashes(self, tmp_path):
        """Source containing backslashes (e.g. Windows paths) is preserved."""
        nb = make_notebook(
            [{"cell_type": "code", "source": r"path = 'C:\Users\test\file.txt'"}],
            tmp_path,
        )
        result = run([str(nb)])
        assert result.returncode == 0
        assert "C:" in result.stdout


# ---------------------------------------------------------------------------
# Class 10 – Exit codes and stderr/stdout separation
# ---------------------------------------------------------------------------

class TestExitCodesAndStreams:
    """Ensure exit codes and stream separation follow the spec."""

    def test_success_exit_zero(self, tmp_path):
        """Successful invocation exits with code 0."""
        nb = make_notebook(
            [{"cell_type": "code", "source": "pass"}],
            tmp_path,
        )
        result = run([str(nb)])
        assert result.returncode == 0

    def test_error_exit_nonzero(self, tmp_path):
        """Any error condition exits with non-zero code."""
        result = run(["/nonexistent/path/notebook.ipynb"])
        assert result.returncode != 0

    def test_error_message_not_silent(self):
        """Error for missing file produces output on stderr or stdout."""
        result = run(["/nonexistent/path/notebook.ipynb"])
        combined = result.stdout + result.stderr
        assert combined.strip() != ""

    def test_wrong_extension_message_not_silent(self, tmp_path):
        """Wrong extension error produces output on stderr or stdout."""
        f = tmp_path / "data.csv"
        f.write_text("a,b,c", encoding="utf-8")
        result = run([str(f)])
        assert result.returncode != 0
        combined = result.stdout + result.stderr
        assert combined.strip() != ""
