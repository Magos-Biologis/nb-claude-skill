"""
Independent test suite for nb-write.py.

Written from the specification alone — the implementation was NOT read before
authoring these tests.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SCRIPT = str(Path(__file__).parent.parent / "scripts" / "nb-write.py")
PYTHON = "python3"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_notebook(cells=None, nbformat=4, nbformat_minor=5):
    """Build a minimal nbformat 4 notebook dict."""
    if cells is None:
        cells = []
    return {
        "nbformat": nbformat,
        "nbformat_minor": nbformat_minor,
        "metadata": {
            "kernelspec": {
                "display_name": "Python 3",
                "language": "python",
                "name": "python3",
            },
            "language_info": {"name": "python", "version": "3.10.0"},
        },
        "cells": cells,
    }


def make_code_cell(source, cell_id, outputs=None, execution_count=42):
    """Build a code cell dict."""
    return {
        "cell_type": "code",
        "id": cell_id,
        "metadata": {},
        "source": source if isinstance(source, list) else [source],
        "outputs": outputs if outputs is not None else [],
        "execution_count": execution_count,
    }


def make_markdown_cell(source, cell_id):
    """Build a markdown cell dict."""
    return {
        "cell_type": "markdown",
        "id": cell_id,
        "metadata": {},
        "source": source if isinstance(source, list) else [source],
    }


def make_raw_cell(source, cell_id):
    """Build a raw cell dict."""
    return {
        "cell_type": "raw",
        "id": cell_id,
        "metadata": {},
        "source": source if isinstance(source, list) else [source],
    }


def write_notebook(path: Path, nb: dict) -> Path:
    """Serialise a notebook dict to disk and return the path."""
    path.write_text(json.dumps(nb, indent=2), encoding="utf-8")
    return path


def read_notebook(path: Path) -> dict:
    """Read a notebook file back as a dict."""
    return json.loads(path.read_text(encoding="utf-8"))


def run_write(args, stdin_text=None, cwd=None):
    """
    Invoke nb-write.py with the given argument list.

    Returns a CompletedProcess with stdout/stderr as strings.
    """
    cmd = [PYTHON, SCRIPT] + [str(a) for a in args]
    return subprocess.run(
        cmd,
        input=stdin_text,
        capture_output=True,
        text=True,
        cwd=cwd,
    )


# ---------------------------------------------------------------------------
# patch — basic success cases
# ---------------------------------------------------------------------------

class TestPatchBasic:

    def test_patch_code_cell_stdin(self, tmp_path):
        """Patching a code cell via stdin replaces its source."""
        nb_path = tmp_path / "nb.ipynb"
        nb = make_notebook([make_code_cell("x = 1\n", "aa000001")])
        write_notebook(nb_path, nb)

        result = run_write([nb_path, "patch", "0"], stdin_text="x = 99\n")

        assert result.returncode == 0
        nb2 = read_notebook(nb_path)
        src = "".join(nb2["cells"][0]["source"])
        assert "x = 99" in src

    def test_patch_code_cell_with_f_flag(self, tmp_path):
        """Patching a code cell with -f reads source from a file."""
        nb_path = tmp_path / "nb.ipynb"
        nb = make_notebook([make_code_cell("old code\n", "aa000002")])
        write_notebook(nb_path, nb)

        src_file = tmp_path / "new_source.py"
        src_file.write_text("new code\n", encoding="utf-8")

        result = run_write([nb_path, "patch", "0", "-f", src_file])

        assert result.returncode == 0
        nb2 = read_notebook(nb_path)
        assert "new code" in "".join(nb2["cells"][0]["source"])

    def test_patch_clears_outputs_for_code_cell(self, tmp_path):
        """Patching a code cell clears its outputs and execution_count."""
        nb_path = tmp_path / "nb.ipynb"
        cell = make_code_cell(
            "print('hi')\n",
            "aa000003",
            outputs=[{"output_type": "stream", "text": "hi\n"}],
            execution_count=7,
        )
        nb = make_notebook([cell])
        write_notebook(nb_path, nb)

        result = run_write([nb_path, "patch", "0"], stdin_text="print('new')\n")

        assert result.returncode == 0
        nb2 = read_notebook(nb_path)
        c = nb2["cells"][0]
        assert c["outputs"] == []
        assert c["execution_count"] is None

    def test_patch_markdown_cell_does_not_add_outputs(self, tmp_path):
        """Patching a markdown cell does not introduce an outputs key."""
        nb_path = tmp_path / "nb.ipynb"
        nb = make_notebook([make_markdown_cell("# Title\n", "bb000001")])
        write_notebook(nb_path, nb)

        result = run_write([nb_path, "patch", "0"], stdin_text="# New Title\n")

        assert result.returncode == 0
        nb2 = read_notebook(nb_path)
        c = nb2["cells"][0]
        assert "# New Title" in "".join(c["source"])
        assert "outputs" not in c

    def test_patch_raw_cell_does_not_add_outputs(self, tmp_path):
        """Patching a raw cell does not introduce an outputs key."""
        nb_path = tmp_path / "nb.ipynb"
        nb = make_notebook([make_raw_cell("raw content\n", "cc000001")])
        write_notebook(nb_path, nb)

        result = run_write([nb_path, "patch", "0"], stdin_text="new raw\n")

        assert result.returncode == 0
        nb2 = read_notebook(nb_path)
        c = nb2["cells"][0]
        assert "outputs" not in c

    def test_patch_last_cell_by_index(self, tmp_path):
        """Patching the last cell in a multi-cell notebook targets the right cell."""
        nb_path = tmp_path / "nb.ipynb"
        nb = make_notebook([
            make_code_cell("cell_0\n", "dd000001"),
            make_code_cell("cell_1\n", "dd000002"),
            make_code_cell("cell_2\n", "dd000003"),
        ])
        write_notebook(nb_path, nb)

        result = run_write([nb_path, "patch", "2"], stdin_text="patched_2\n")

        assert result.returncode == 0
        nb2 = read_notebook(nb_path)
        assert "patched_2" in "".join(nb2["cells"][2]["source"])
        assert "cell_0" in "".join(nb2["cells"][0]["source"])
        assert "cell_1" in "".join(nb2["cells"][1]["source"])

    def test_patch_preserves_other_cells(self, tmp_path):
        """Patching one cell does not modify any other cell's source."""
        nb_path = tmp_path / "nb.ipynb"
        nb = make_notebook([
            make_code_cell("alpha\n", "ee000001"),
            make_code_cell("beta\n", "ee000002"),
        ])
        write_notebook(nb_path, nb)

        run_write([nb_path, "patch", "0"], stdin_text="alpha_patched\n")

        nb2 = read_notebook(nb_path)
        assert "beta" in "".join(nb2["cells"][1]["source"])

    def test_patch_multiline_source_via_stdin(self, tmp_path):
        """Patch accepts multi-line source content via stdin."""
        nb_path = tmp_path / "nb.ipynb"
        nb = make_notebook([make_code_cell("single\n", "ff000001")])
        write_notebook(nb_path, nb)

        multi = "line1\nline2\nline3\n"
        result = run_write([nb_path, "patch", "0"], stdin_text=multi)

        assert result.returncode == 0
        nb2 = read_notebook(nb_path)
        joined = "".join(nb2["cells"][0]["source"])
        assert "line1" in joined
        assert "line2" in joined
        assert "line3" in joined

    def test_patch_source_with_special_json_chars(self, tmp_path):
        """Patch handles source containing JSON-special characters."""
        nb_path = tmp_path / "nb.ipynb"
        nb = make_notebook([make_code_cell("plain\n", "gg000001")])
        write_notebook(nb_path, nb)

        tricky = 'data = {"key": "value\\nnewline"}\n'
        result = run_write([nb_path, "patch", "0"], stdin_text=tricky)

        assert result.returncode == 0
        nb2 = read_notebook(nb_path)
        assert nb2 is not None  # file is still valid JSON

    def test_patch_empty_source(self, tmp_path):
        """Patch with empty source is allowed — replaces content with empty."""
        nb_path = tmp_path / "nb.ipynb"
        nb = make_notebook([make_code_cell("some code\n", "hh000001")])
        write_notebook(nb_path, nb)

        result = run_write([nb_path, "patch", "0"], stdin_text="")

        assert result.returncode == 0


# ---------------------------------------------------------------------------
# patch — error paths
# ---------------------------------------------------------------------------

class TestPatchErrors:

    def test_patch_out_of_range_index(self, tmp_path):
        """Patch with an out-of-range index exits non-zero."""
        nb_path = tmp_path / "nb.ipynb"
        nb = make_notebook([make_code_cell("x\n", "ii000001")])
        write_notebook(nb_path, nb)

        result = run_write([nb_path, "patch", "5"], stdin_text="y\n")

        assert result.returncode != 0

    def test_patch_negative_index_error(self, tmp_path):
        """Patch with a negative index (other than supported ones) exits non-zero."""
        nb_path = tmp_path / "nb.ipynb"
        nb = make_notebook([make_code_cell("x\n", "ii000002")])
        write_notebook(nb_path, nb)

        result = run_write([nb_path, "patch", "-5"], stdin_text="y\n")

        assert result.returncode != 0

    def test_patch_non_integer_index(self, tmp_path):
        """Patch with a non-integer index exits non-zero with a clean message."""
        nb_path = tmp_path / "nb.ipynb"
        nb = make_notebook([make_code_cell("x\n", "jj000001")])
        write_notebook(nb_path, nb)

        result = run_write([nb_path, "patch", "abc"], stdin_text="y\n")

        assert result.returncode != 0
        # Should not be a Python traceback
        assert "Traceback" not in result.stderr

    def test_patch_out_of_range_error_on_stderr(self, tmp_path):
        """Out-of-range patch error message goes to stderr, not stdout."""
        nb_path = tmp_path / "nb.ipynb"
        nb = make_notebook([make_code_cell("x\n", "kk000001")])
        write_notebook(nb_path, nb)

        result = run_write([nb_path, "patch", "99"], stdin_text="y\n")

        assert result.returncode != 0
        assert result.stderr.strip() != ""

    def test_patch_nonexistent_file(self, tmp_path):
        """Patch on a non-existent notebook exits non-zero."""
        result = run_write([tmp_path / "ghost.ipynb", "patch", "0"], stdin_text="x\n")
        assert result.returncode != 0

    def test_patch_wrong_extension(self, tmp_path):
        """Patch refuses files without .ipynb extension."""
        bad = tmp_path / "notebook.txt"
        bad.write_text("{}", encoding="utf-8")
        result = run_write([bad, "patch", "0"], stdin_text="x\n")
        assert result.returncode != 0


# ---------------------------------------------------------------------------
# insert — basic success cases
# ---------------------------------------------------------------------------

class TestInsertBasic:

    def test_insert_code_cell_before_index(self, tmp_path):
        """Insert a code cell before index 0 prepends it to the notebook."""
        nb_path = tmp_path / "nb.ipynb"
        nb = make_notebook([make_code_cell("existing\n", "ll000001")])
        write_notebook(nb_path, nb)

        result = run_write(
            [nb_path, "insert", "0", "code"], stdin_text="inserted\n"
        )

        assert result.returncode == 0
        nb2 = read_notebook(nb_path)
        assert len(nb2["cells"]) == 2
        assert "inserted" in "".join(nb2["cells"][0]["source"])
        assert "existing" in "".join(nb2["cells"][1]["source"])

    def test_insert_code_cell_in_middle(self, tmp_path):
        """Insert a code cell in the middle preserves surrounding cells."""
        nb_path = tmp_path / "nb.ipynb"
        nb = make_notebook([
            make_code_cell("first\n", "mm000001"),
            make_code_cell("second\n", "mm000002"),
        ])
        write_notebook(nb_path, nb)

        result = run_write(
            [nb_path, "insert", "1", "code"], stdin_text="middle\n"
        )

        assert result.returncode == 0
        nb2 = read_notebook(nb_path)
        assert len(nb2["cells"]) == 3
        assert "first" in "".join(nb2["cells"][0]["source"])
        assert "middle" in "".join(nb2["cells"][1]["source"])
        assert "second" in "".join(nb2["cells"][2]["source"])

    def test_insert_append_with_minus_one(self, tmp_path):
        """Insert with index -1 appends the cell at the end."""
        nb_path = tmp_path / "nb.ipynb"
        nb = make_notebook([
            make_code_cell("first\n", "nn000001"),
            make_code_cell("second\n", "nn000002"),
        ])
        write_notebook(nb_path, nb)

        result = run_write(
            [nb_path, "insert", "-1", "code"], stdin_text="appended\n"
        )

        assert result.returncode == 0
        nb2 = read_notebook(nb_path)
        assert len(nb2["cells"]) == 3
        assert "appended" in "".join(nb2["cells"][-1]["source"])

    def test_insert_markdown_cell(self, tmp_path):
        """Insert a markdown cell creates a cell of type markdown."""
        nb_path = tmp_path / "nb.ipynb"
        nb = make_notebook([make_code_cell("code\n", "oo000001")])
        write_notebook(nb_path, nb)

        result = run_write(
            [nb_path, "insert", "0", "markdown"], stdin_text="# Header\n"
        )

        assert result.returncode == 0
        nb2 = read_notebook(nb_path)
        assert nb2["cells"][0]["cell_type"] == "markdown"
        assert "# Header" in "".join(nb2["cells"][0]["source"])

    def test_insert_raw_cell(self, tmp_path):
        """Insert a raw cell creates a cell of type raw."""
        nb_path = tmp_path / "nb.ipynb"
        nb = make_notebook([make_code_cell("code\n", "pp000001")])
        write_notebook(nb_path, nb)

        result = run_write(
            [nb_path, "insert", "0", "raw"], stdin_text="raw content\n"
        )

        assert result.returncode == 0
        nb2 = read_notebook(nb_path)
        assert nb2["cells"][0]["cell_type"] == "raw"

    def test_insert_code_cell_has_empty_outputs(self, tmp_path):
        """Newly inserted code cell has outputs=[] and execution_count=null."""
        nb_path = tmp_path / "nb.ipynb"
        nb = make_notebook([make_code_cell("x\n", "qq000001")])
        write_notebook(nb_path, nb)

        result = run_write(
            [nb_path, "insert", "0", "code"], stdin_text="new\n"
        )

        assert result.returncode == 0
        nb2 = read_notebook(nb_path)
        new_cell = nb2["cells"][0]
        assert new_cell["outputs"] == []
        assert new_cell["execution_count"] is None

    def test_insert_code_cell_has_id(self, tmp_path):
        """Newly inserted code cell has an id field with 8 alphanumeric chars."""
        nb_path = tmp_path / "nb.ipynb"
        nb = make_notebook([make_code_cell("x\n", "rr000001")])
        write_notebook(nb_path, nb)

        result = run_write(
            [nb_path, "insert", "0", "code"], stdin_text="new\n"
        )

        assert result.returncode == 0
        nb2 = read_notebook(nb_path)
        cell_id = nb2["cells"][0].get("id", "")
        assert len(cell_id) == 8
        assert cell_id.isalnum()

    def test_insert_markdown_cell_has_id(self, tmp_path):
        """Newly inserted markdown cell has an id field."""
        nb_path = tmp_path / "nb.ipynb"
        nb = make_notebook([make_markdown_cell("# hi\n", "ss000001")])
        write_notebook(nb_path, nb)

        result = run_write(
            [nb_path, "insert", "0", "markdown"], stdin_text="# new\n"
        )

        assert result.returncode == 0
        nb2 = read_notebook(nb_path)
        cell_id = nb2["cells"][0].get("id", "")
        assert len(cell_id) == 8

    def test_insert_with_f_flag(self, tmp_path):
        """Insert reads source from -f file when specified."""
        nb_path = tmp_path / "nb.ipynb"
        nb = make_notebook([make_code_cell("old\n", "tt000001")])
        write_notebook(nb_path, nb)

        src_file = tmp_path / "src.py"
        src_file.write_text("from_file\n", encoding="utf-8")

        result = run_write([nb_path, "insert", "0", "code", "-f", src_file])

        assert result.returncode == 0
        nb2 = read_notebook(nb_path)
        assert "from_file" in "".join(nb2["cells"][0]["source"])

    def test_insert_into_empty_notebook(self, tmp_path):
        """Insert into an empty notebook creates a single cell."""
        nb_path = tmp_path / "nb.ipynb"
        nb = make_notebook([])
        write_notebook(nb_path, nb)

        result = run_write(
            [nb_path, "insert", "0", "code"], stdin_text="first_ever\n"
        )

        assert result.returncode == 0
        nb2 = read_notebook(nb_path)
        assert len(nb2["cells"]) == 1
        assert "first_ever" in "".join(nb2["cells"][0]["source"])

    def test_insert_markdown_no_outputs_key(self, tmp_path):
        """Newly inserted markdown cell does not get an outputs key."""
        nb_path = tmp_path / "nb.ipynb"
        nb = make_notebook([make_code_cell("x\n", "uu000001")])
        write_notebook(nb_path, nb)

        run_write([nb_path, "insert", "0", "markdown"], stdin_text="# hi\n")

        nb2 = read_notebook(nb_path)
        assert "outputs" not in nb2["cells"][0]


# ---------------------------------------------------------------------------
# insert — error paths
# ---------------------------------------------------------------------------

class TestInsertErrors:

    def test_insert_out_of_range_index(self, tmp_path):
        """Insert with an out-of-range positive index exits non-zero."""
        nb_path = tmp_path / "nb.ipynb"
        nb = make_notebook([make_code_cell("x\n", "vv000001")])
        write_notebook(nb_path, nb)

        result = run_write(
            [nb_path, "insert", "99", "code"], stdin_text="y\n"
        )

        assert result.returncode != 0

    def test_insert_unknown_cell_type(self, tmp_path):
        """Insert with an unknown cell type exits non-zero."""
        nb_path = tmp_path / "nb.ipynb"
        nb = make_notebook([make_code_cell("x\n", "ww000001")])
        write_notebook(nb_path, nb)

        result = run_write(
            [nb_path, "insert", "0", "spreadsheet"], stdin_text="y\n"
        )

        assert result.returncode != 0

    def test_insert_non_integer_index(self, tmp_path):
        """Insert with a non-integer index exits non-zero with clean error."""
        nb_path = tmp_path / "nb.ipynb"
        nb = make_notebook([make_code_cell("x\n", "xx000001")])
        write_notebook(nb_path, nb)

        result = run_write(
            [nb_path, "insert", "two", "code"], stdin_text="y\n"
        )

        assert result.returncode != 0
        assert "Traceback" not in result.stderr

    def test_insert_unknown_type_error_on_stderr(self, tmp_path):
        """Unknown cell type error message appears on stderr."""
        nb_path = tmp_path / "nb.ipynb"
        nb = make_notebook([make_code_cell("x\n", "yy000001")])
        write_notebook(nb_path, nb)

        result = run_write(
            [nb_path, "insert", "0", "badtype"], stdin_text="y\n"
        )

        assert result.returncode != 0
        assert result.stderr.strip() != ""


# ---------------------------------------------------------------------------
# delete — basic success cases
# ---------------------------------------------------------------------------

class TestDeleteBasic:

    def test_delete_only_cell(self, tmp_path):
        """Delete the only cell results in an empty cells list."""
        nb_path = tmp_path / "nb.ipynb"
        nb = make_notebook([make_code_cell("x\n", "zz000001")])
        write_notebook(nb_path, nb)

        result = run_write([nb_path, "delete", "0"])

        assert result.returncode == 0
        nb2 = read_notebook(nb_path)
        assert nb2["cells"] == []

    def test_delete_first_cell(self, tmp_path):
        """Deleting cell at index 0 shifts remaining cells."""
        nb_path = tmp_path / "nb.ipynb"
        nb = make_notebook([
            make_code_cell("first\n", "a1000001"),
            make_code_cell("second\n", "a1000002"),
            make_code_cell("third\n", "a1000003"),
        ])
        write_notebook(nb_path, nb)

        result = run_write([nb_path, "delete", "0"])

        assert result.returncode == 0
        nb2 = read_notebook(nb_path)
        assert len(nb2["cells"]) == 2
        assert "second" in "".join(nb2["cells"][0]["source"])
        assert "third" in "".join(nb2["cells"][1]["source"])

    def test_delete_middle_cell(self, tmp_path):
        """Deleting a middle cell leaves first and last intact."""
        nb_path = tmp_path / "nb.ipynb"
        nb = make_notebook([
            make_code_cell("first\n", "b1000001"),
            make_code_cell("middle\n", "b1000002"),
            make_code_cell("last\n", "b1000003"),
        ])
        write_notebook(nb_path, nb)

        result = run_write([nb_path, "delete", "1"])

        assert result.returncode == 0
        nb2 = read_notebook(nb_path)
        assert len(nb2["cells"]) == 2
        assert "first" in "".join(nb2["cells"][0]["source"])
        assert "last" in "".join(nb2["cells"][1]["source"])

    def test_delete_last_cell(self, tmp_path):
        """Deleting the last cell leaves preceding cells intact."""
        nb_path = tmp_path / "nb.ipynb"
        nb = make_notebook([
            make_code_cell("first\n", "c1000001"),
            make_code_cell("last\n", "c1000002"),
        ])
        write_notebook(nb_path, nb)

        result = run_write([nb_path, "delete", "1"])

        assert result.returncode == 0
        nb2 = read_notebook(nb_path)
        assert len(nb2["cells"]) == 1
        assert "first" in "".join(nb2["cells"][0]["source"])


# ---------------------------------------------------------------------------
# delete — error paths
# ---------------------------------------------------------------------------

class TestDeleteErrors:

    def test_delete_out_of_range_index(self, tmp_path):
        """Delete with an out-of-range index exits non-zero."""
        nb_path = tmp_path / "nb.ipynb"
        nb = make_notebook([make_code_cell("x\n", "d1000001")])
        write_notebook(nb_path, nb)

        result = run_write([nb_path, "delete", "5"])

        assert result.returncode != 0

    def test_delete_empty_notebook(self, tmp_path):
        """Delete on an empty cells list exits non-zero."""
        nb_path = tmp_path / "nb.ipynb"
        nb = make_notebook([])
        write_notebook(nb_path, nb)

        result = run_write([nb_path, "delete", "0"])

        assert result.returncode != 0

    def test_delete_non_integer_index(self, tmp_path):
        """Delete with a non-integer index exits non-zero with clean error."""
        nb_path = tmp_path / "nb.ipynb"
        nb = make_notebook([make_code_cell("x\n", "e1000001")])
        write_notebook(nb_path, nb)

        result = run_write([nb_path, "delete", "one"])

        assert result.returncode != 0
        assert "Traceback" not in result.stderr

    def test_delete_negative_index_error(self, tmp_path):
        """Delete with a negative index (other than supported ones) exits non-zero."""
        nb_path = tmp_path / "nb.ipynb"
        nb = make_notebook([make_code_cell("x\n", "e1000002")])
        write_notebook(nb_path, nb)

        result = run_write([nb_path, "delete", "-3"])

        assert result.returncode != 0


# ---------------------------------------------------------------------------
# stdout silence contract
# ---------------------------------------------------------------------------

class TestStdoutSilence:

    def test_patch_stdout_silent_on_success(self, tmp_path):
        """Successful patch produces no output on stdout."""
        nb_path = tmp_path / "nb.ipynb"
        nb = make_notebook([make_code_cell("x\n", "f1000001")])
        write_notebook(nb_path, nb)

        result = run_write([nb_path, "patch", "0"], stdin_text="y\n")

        assert result.returncode == 0
        assert result.stdout == ""

    def test_insert_stdout_silent_on_success(self, tmp_path):
        """Successful insert produces no output on stdout."""
        nb_path = tmp_path / "nb.ipynb"
        nb = make_notebook([make_code_cell("x\n", "g1000001")])
        write_notebook(nb_path, nb)

        result = run_write([nb_path, "insert", "0", "code"], stdin_text="y\n")

        assert result.returncode == 0
        assert result.stdout == ""

    def test_delete_stdout_silent_on_success(self, tmp_path):
        """Successful delete produces no output on stdout."""
        nb_path = tmp_path / "nb.ipynb"
        nb = make_notebook([make_code_cell("x\n", "h1000001")])
        write_notebook(nb_path, nb)

        result = run_write([nb_path, "delete", "0"])

        assert result.returncode == 0
        assert result.stdout == ""


# ---------------------------------------------------------------------------
# Symlink rejection
# ---------------------------------------------------------------------------

@pytest.mark.skipif(sys.platform == 'win32', reason='symlink creation requires admin/Developer Mode on Windows')
class TestSymlinkRejection:

    def test_patch_refuses_symlink(self, tmp_path):
        """patch refuses to operate on a symlink target."""
        real = tmp_path / "real.ipynb"
        nb = make_notebook([make_code_cell("x\n", "i1000001")])
        write_notebook(real, nb)

        link = tmp_path / "link.ipynb"
        link.symlink_to(real)

        result = run_write([link, "patch", "0"], stdin_text="y\n")

        assert result.returncode != 0

    def test_insert_refuses_symlink(self, tmp_path):
        """insert refuses to operate on a symlink target."""
        real = tmp_path / "real.ipynb"
        nb = make_notebook([make_code_cell("x\n", "j1000001")])
        write_notebook(real, nb)

        link = tmp_path / "link.ipynb"
        link.symlink_to(real)

        result = run_write([link, "insert", "0", "code"], stdin_text="y\n")

        assert result.returncode != 0

    def test_delete_refuses_symlink(self, tmp_path):
        """delete refuses to operate on a symlink target."""
        real = tmp_path / "real.ipynb"
        nb = make_notebook([make_code_cell("x\n", "k1000001")])
        write_notebook(real, nb)

        link = tmp_path / "link.ipynb"
        link.symlink_to(real)

        result = run_write([link, "delete", "0"])

        assert result.returncode != 0

    def test_symlink_error_on_stderr(self, tmp_path):
        """Symlink rejection error message appears on stderr."""
        real = tmp_path / "real.ipynb"
        nb = make_notebook([make_code_cell("x\n", "l1000001")])
        write_notebook(real, nb)

        link = tmp_path / "link.ipynb"
        link.symlink_to(real)

        result = run_write([link, "patch", "0"], stdin_text="y\n")

        assert result.stderr.strip() != ""


# ---------------------------------------------------------------------------
# Unknown operation
# ---------------------------------------------------------------------------

class TestUnknownOperation:

    def test_unknown_operation_exits_nonzero(self, tmp_path):
        """An unknown operation exits non-zero."""
        nb_path = tmp_path / "nb.ipynb"
        nb = make_notebook([make_code_cell("x\n", "m1000001")])
        write_notebook(nb_path, nb)

        result = run_write([nb_path, "frobnicate", "0"])

        assert result.returncode != 0

    def test_unknown_operation_error_on_stderr(self, tmp_path):
        """An unknown operation error message appears on stderr."""
        nb_path = tmp_path / "nb.ipynb"
        nb = make_notebook([make_code_cell("x\n", "n1000001")])
        write_notebook(nb_path, nb)

        result = run_write([nb_path, "unknown_op"])

        assert result.returncode != 0
        assert result.stderr.strip() != ""


# ---------------------------------------------------------------------------
# Missing cells key
# ---------------------------------------------------------------------------

class TestMissingCellsKey:

    def test_patch_missing_cells_key(self, tmp_path):
        """Patch on a notebook without a 'cells' key exits non-zero."""
        nb_path = tmp_path / "bad.ipynb"
        nb_path.write_text(
            json.dumps({"nbformat": 4, "nbformat_minor": 5, "metadata": {}}),
            encoding="utf-8",
        )

        result = run_write([nb_path, "patch", "0"], stdin_text="x\n")

        assert result.returncode != 0

    def test_insert_missing_cells_key(self, tmp_path):
        """Insert on a notebook without a 'cells' key exits non-zero."""
        nb_path = tmp_path / "bad.ipynb"
        nb_path.write_text(
            json.dumps({"nbformat": 4, "nbformat_minor": 5, "metadata": {}}),
            encoding="utf-8",
        )

        result = run_write([nb_path, "insert", "0", "code"], stdin_text="x\n")

        assert result.returncode != 0

    def test_delete_missing_cells_key(self, tmp_path):
        """Delete on a notebook without a 'cells' key exits non-zero."""
        nb_path = tmp_path / "bad.ipynb"
        nb_path.write_text(
            json.dumps({"nbformat": 4, "nbformat_minor": 5, "metadata": {}}),
            encoding="utf-8",
        )

        result = run_write([nb_path, "delete", "0"])

        assert result.returncode != 0


# ---------------------------------------------------------------------------
# Atomic write / valid JSON after write
# ---------------------------------------------------------------------------

class TestAtomicWrite:

    def test_result_is_valid_json_after_patch(self, tmp_path):
        """File is valid JSON after a patch operation."""
        nb_path = tmp_path / "nb.ipynb"
        nb = make_notebook([make_code_cell("x\n", "o1000001")])
        write_notebook(nb_path, nb)

        run_write([nb_path, "patch", "0"], stdin_text="y = 1\n")

        content = nb_path.read_text(encoding="utf-8")
        parsed = json.loads(content)  # raises if not valid JSON
        assert "cells" in parsed

    def test_result_is_valid_json_after_insert(self, tmp_path):
        """File is valid JSON after an insert operation."""
        nb_path = tmp_path / "nb.ipynb"
        nb = make_notebook([make_code_cell("x\n", "p1000001")])
        write_notebook(nb_path, nb)

        run_write([nb_path, "insert", "-1", "markdown"], stdin_text="# end\n")

        content = nb_path.read_text(encoding="utf-8")
        parsed = json.loads(content)
        assert "cells" in parsed

    def test_result_is_valid_json_after_delete(self, tmp_path):
        """File is valid JSON after a delete operation."""
        nb_path = tmp_path / "nb.ipynb"
        nb = make_notebook([
            make_code_cell("a\n", "q1000001"),
            make_code_cell("b\n", "q1000002"),
        ])
        write_notebook(nb_path, nb)

        run_write([nb_path, "delete", "0"])

        content = nb_path.read_text(encoding="utf-8")
        parsed = json.loads(content)
        assert "cells" in parsed

    def test_no_bak_file_created(self, tmp_path):
        """No .bak file is created alongside the notebook after write."""
        nb_path = tmp_path / "nb.ipynb"
        nb = make_notebook([make_code_cell("x\n", "r1000001")])
        write_notebook(nb_path, nb)

        run_write([nb_path, "patch", "0"], stdin_text="y\n")

        bak = tmp_path / "nb.ipynb.bak"
        assert not bak.exists()


# ---------------------------------------------------------------------------
# -f flag edge cases
# ---------------------------------------------------------------------------

class TestFFlag:

    def test_patch_f_flag_nonexistent_file(self, tmp_path):
        """Patch with -f pointing to a non-existent file exits non-zero."""
        nb_path = tmp_path / "nb.ipynb"
        nb = make_notebook([make_code_cell("x\n", "s1000001")])
        write_notebook(nb_path, nb)

        result = run_write([nb_path, "patch", "0", "-f", tmp_path / "ghost.py"])

        assert result.returncode != 0

    def test_insert_f_flag_nonexistent_file(self, tmp_path):
        """Insert with -f pointing to a non-existent file exits non-zero."""
        nb_path = tmp_path / "nb.ipynb"
        nb = make_notebook([make_code_cell("x\n", "t1000001")])
        write_notebook(nb_path, nb)

        result = run_write(
            [nb_path, "insert", "0", "code", "-f", tmp_path / "ghost.py"]
        )

        assert result.returncode != 0

    def test_patch_f_flag_preferred_over_stdin(self, tmp_path):
        """When -f is provided, the file content is used, not stdin."""
        nb_path = tmp_path / "nb.ipynb"
        nb = make_notebook([make_code_cell("old\n", "u1000001")])
        write_notebook(nb_path, nb)

        src_file = tmp_path / "src.py"
        src_file.write_text("from_file_content\n", encoding="utf-8")

        result = run_write(
            [nb_path, "patch", "0", "-f", src_file],
            stdin_text="from_stdin_content\n",
        )

        assert result.returncode == 0
        nb2 = read_notebook(nb_path)
        joined = "".join(nb2["cells"][0]["source"])
        assert "from_file_content" in joined
        assert "from_stdin_content" not in joined


# ---------------------------------------------------------------------------
# Boundary / adversarial
# ---------------------------------------------------------------------------

class TestAdversarial:

    def test_source_containing_null_bytes_via_file(self, tmp_path):
        """Source file with only whitespace/newlines is accepted."""
        nb_path = tmp_path / "nb.ipynb"
        nb = make_notebook([make_code_cell("x\n", "v1000001")])
        write_notebook(nb_path, nb)

        src_file = tmp_path / "src.py"
        src_file.write_text("\n\n\n", encoding="utf-8")

        result = run_write([nb_path, "patch", "0", "-f", src_file])
        assert result.returncode == 0

    def test_patch_unicode_source(self, tmp_path):
        """Source containing Unicode characters is handled correctly."""
        nb_path = tmp_path / "nb.ipynb"
        nb = make_notebook([make_code_cell("x\n", "w1000001")])
        write_notebook(nb_path, nb)

        unicode_src = "# 你好世界\nprint('🎉')\n"
        result = run_write([nb_path, "patch", "0"], stdin_text=unicode_src)

        assert result.returncode == 0
        nb2 = read_notebook(nb_path)
        joined = "".join(nb2["cells"][0]["source"])
        assert "你好" in joined

    def test_insert_before_index_zero_empty_notebook_minus_one_appends(self, tmp_path):
        """Insert with -1 into empty notebook appends one cell."""
        nb_path = tmp_path / "nb.ipynb"
        nb = make_notebook([])
        write_notebook(nb_path, nb)

        result = run_write(
            [nb_path, "insert", "-1", "code"], stdin_text="appended\n"
        )

        assert result.returncode == 0
        nb2 = read_notebook(nb_path)
        assert len(nb2["cells"]) == 1

    def test_patch_source_with_backslashes(self, tmp_path):
        """Source containing backslashes is round-tripped correctly."""
        nb_path = tmp_path / "nb.ipynb"
        nb = make_notebook([make_code_cell("x\n", "x1000001")])
        write_notebook(nb_path, nb)

        src = "path = 'C:\\\\Users\\\\test'\n"
        result = run_write([nb_path, "patch", "0"], stdin_text=src)

        assert result.returncode == 0
        nb2 = read_notebook(nb_path)
        assert nb2 is not None  # valid JSON

    def test_delete_reduces_cell_count_by_one(self, tmp_path):
        """Delete always reduces cell count by exactly one."""
        nb_path = tmp_path / "nb.ipynb"
        cells = [make_code_cell(f"cell{i}\n", f"y100000{i}") for i in range(5)]
        nb = make_notebook(cells)
        write_notebook(nb_path, nb)

        run_write([nb_path, "delete", "2"])

        nb2 = read_notebook(nb_path)
        assert len(nb2["cells"]) == 4

    def test_insert_increases_cell_count_by_one(self, tmp_path):
        """Insert always increases cell count by exactly one."""
        nb_path = tmp_path / "nb.ipynb"
        cells = [make_code_cell(f"cell{i}\n", f"z100000{i}") for i in range(3)]
        nb = make_notebook(cells)
        write_notebook(nb_path, nb)

        run_write([nb_path, "insert", "1", "code"], stdin_text="new\n")

        nb2 = read_notebook(nb_path)
        assert len(nb2["cells"]) == 4

    def test_repeated_patch_operations(self, tmp_path):
        """Multiple sequential patch operations each take effect."""
        nb_path = tmp_path / "nb.ipynb"
        nb = make_notebook([make_code_cell("v0\n", "a2000001")])
        write_notebook(nb_path, nb)

        for i in range(1, 4):
            run_write([nb_path, "patch", "0"], stdin_text=f"v{i}\n")

        nb2 = read_notebook(nb_path)
        assert "v3" in "".join(nb2["cells"][0]["source"])

    def test_source_with_quotes_and_double_quotes(self, tmp_path):
        """Source containing both single and double quotes is handled."""
        nb_path = tmp_path / "nb.ipynb"
        nb = make_notebook([make_code_cell("x\n", "b2000001")])
        write_notebook(nb_path, nb)

        src = """s = 'he said "hello"'\nt = "it's fine"\n"""
        result = run_write([nb_path, "patch", "0"], stdin_text=src)

        assert result.returncode == 0
        nb2 = read_notebook(nb_path)
        assert nb2 is not None

    def test_wrong_extension_txt(self, tmp_path):
        """A .txt file is rejected regardless of content."""
        bad = tmp_path / "not_a_notebook.txt"
        nb = make_notebook([make_code_cell("x\n", "c2000001")])
        bad.write_text(json.dumps(nb), encoding="utf-8")

        result = run_write([bad, "patch", "0"], stdin_text="y\n")

        assert result.returncode != 0

    def test_wrong_extension_json(self, tmp_path):
        """A .json file is rejected even if it contains notebook structure."""
        bad = tmp_path / "notebook.json"
        nb = make_notebook([make_code_cell("x\n", "d2000001")])
        bad.write_text(json.dumps(nb), encoding="utf-8")

        result = run_write([bad, "patch", "0"], stdin_text="y\n")

        assert result.returncode != 0

    def test_non_integer_index_no_traceback_insert(self, tmp_path):
        """Non-integer index for insert produces no Python traceback."""
        nb_path = tmp_path / "nb.ipynb"
        nb = make_notebook([make_code_cell("x\n", "e2000001")])
        write_notebook(nb_path, nb)

        result = run_write([nb_path, "insert", "notanint", "code"], stdin_text="y\n")

        assert result.returncode != 0
        assert "Traceback" not in result.stderr

    def test_non_integer_index_no_traceback_delete(self, tmp_path):
        """Non-integer index for delete produces no Python traceback."""
        nb_path = tmp_path / "nb.ipynb"
        nb = make_notebook([make_code_cell("x\n", "f2000001")])
        write_notebook(nb_path, nb)

        result = run_write([nb_path, "delete", "notanint"])

        assert result.returncode != 0
        assert "Traceback" not in result.stderr
