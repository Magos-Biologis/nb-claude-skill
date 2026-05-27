"""
pytest test suite for nb-read.py and nb-write.py.

Run with:  pytest skills/nb/tests/test_scripts.py -v
"""

import json
import os
import subprocess
import sys
import textwrap
import string
import secrets
import pytest
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

SCRIPTS = Path(__file__).parent.parent / "scripts"
NB_READ  = str(SCRIPTS / "nb-read.py")
NB_WRITE = str(SCRIPTS / "nb-write.py")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _cell_id():
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(8))


def make_notebook(cells, tmp_path, name="test.ipynb"):
    """Write a minimal nbformat 4 notebook to a temp file and return its path."""
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
    path = tmp_path / name
    path.write_text(json.dumps(nb, indent=1), encoding="utf-8")
    return str(path)


def read_nb(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def run_read(args, **kwargs):
    return subprocess.run(
        [sys.executable, NB_READ] + args,
        capture_output=True, text=True, **kwargs,
    )


def run_write(args, stdin=None, **kwargs):
    return subprocess.run(
        [sys.executable, NB_WRITE] + args,
        input=stdin, capture_output=True, text=True, **kwargs,
    )


# ---------------------------------------------------------------------------
# nb-read: basic output
# ---------------------------------------------------------------------------

class TestNbRead:

    def test_full_read(self, tmp_path):
        p = make_notebook([
            {"cell_type": "markdown", "source": ["# Title"]},
            {"cell_type": "code",     "source": ["x = 1\n", "print(x)"]},
        ], tmp_path)
        r = run_read([p])
        assert r.returncode == 0
        assert "[0:markdown]" in r.stdout
        assert "# Title" in r.stdout
        assert "[1:code]" in r.stdout
        assert "x = 1" in r.stdout

    def test_header_shows_cell_count_and_kernel(self, tmp_path):
        p = make_notebook([
            {"cell_type": "code", "source": ["pass"]},
        ], tmp_path)
        r = run_read([p])
        assert "1 cell" in r.stdout
        assert "python3" in r.stdout

    def test_filter_by_type_code(self, tmp_path):
        p = make_notebook([
            {"cell_type": "markdown", "source": ["## Heading"]},
            {"cell_type": "code",     "source": ["x = 1"]},
            {"cell_type": "markdown", "source": ["## Outro"]},
        ], tmp_path)
        r = run_read([p, "--type", "code"])
        assert "[0:markdown]" not in r.stdout
        assert "[1:code]" in r.stdout
        assert "[2:markdown]" not in r.stdout

    def test_filter_single_cell(self, tmp_path):
        p = make_notebook([
            {"cell_type": "code", "source": ["a = 1"]},
            {"cell_type": "code", "source": ["b = 2"]},
            {"cell_type": "code", "source": ["c = 3"]},
        ], tmp_path)
        r = run_read([p, "--cells", "1"])
        assert "a = 1" not in r.stdout
        assert "b = 2" in r.stdout
        assert "c = 3" not in r.stdout

    def test_filter_range(self, tmp_path):
        p = make_notebook([
            {"cell_type": "code", "source": [f"x = {i}"]} for i in range(5)
        ], tmp_path)
        r = run_read([p, "--cells", "1-3"])
        assert "x = 0" not in r.stdout
        assert "x = 1" in r.stdout
        assert "x = 3" in r.stdout
        assert "x = 4" not in r.stdout

    def test_filter_comma_list(self, tmp_path):
        p = make_notebook([
            {"cell_type": "code", "source": [f"x = {i}"]} for i in range(5)
        ], tmp_path)
        r = run_read([p, "--cells", "0,2,4"])
        assert "x = 0" in r.stdout
        assert "x = 1" not in r.stdout
        assert "x = 2" in r.stdout
        assert "x = 3" not in r.stdout
        assert "x = 4" in r.stdout

    def test_truncation_goes_to_stderr_not_stdout(self, tmp_path):
        source = [f"line_{i}\n" for i in range(100)]
        p = make_notebook([{"cell_type": "code", "source": source}], tmp_path)
        r = run_read([p, "--truncate", "10"])
        assert r.returncode == 0
        assert "TRUNCATED" not in r.stdout, "truncation notice must not appear on stdout"
        assert "TRUNCATED" in r.stderr

    def test_truncate_zero_shows_all(self, tmp_path):
        source = [f"line_{i}\n" for i in range(100)]
        p = make_notebook([{"cell_type": "code", "source": source}], tmp_path)
        r = run_read([p, "--truncate", "0"])
        assert r.returncode == 0
        assert "line_99" in r.stdout
        assert "TRUNCATED" not in r.stderr

    def test_empty_cell_shows_empty_marker(self, tmp_path):
        p = make_notebook([{"cell_type": "code", "source": []}], tmp_path)
        r = run_read([p])
        assert "(empty)" in r.stdout

    def test_string_source_handled(self, tmp_path):
        """Some old notebooks store source as a plain string, not a list."""
        nb_path = tmp_path / "old.ipynb"
        nb = {
            "nbformat": 4, "nbformat_minor": 5,
            "metadata": {"kernelspec": {"name": "python3", "language": "python", "display_name": "Python 3"}},
            "cells": [{"id": _cell_id(), "cell_type": "code", "metadata": {},
                       "source": "x = 1\nprint(x)", "outputs": [], "execution_count": None}],
        }
        nb_path.write_text(json.dumps(nb), encoding="utf-8")
        r = run_read([str(nb_path)])
        assert r.returncode == 0
        assert "x = 1" in r.stdout

    def test_ansi_injection_in_cell_type_stripped(self, tmp_path):
        nb_path = tmp_path / "ansi.ipynb"
        nb = {
            "nbformat": 4, "nbformat_minor": 5,
            "metadata": {"kernelspec": {"name": "\x1b[2J\x1b[Hinjected", "language": "python", "display_name": "X"}},
            "cells": [{"id": _cell_id(), "cell_type": "code", "metadata": {},
                       "source": ["pass"], "outputs": [], "execution_count": None}],
        }
        nb_path.write_text(json.dumps(nb), encoding="utf-8")
        r = run_read([str(nb_path)])
        assert r.returncode == 0
        assert "\x1b" not in r.stdout, "ANSI escape leaked to stdout"

    def test_bom_notebook(self, tmp_path):
        nb = {
            "nbformat": 4, "nbformat_minor": 5,
            "metadata": {"kernelspec": {"name": "python3", "language": "python", "display_name": "Python 3"}},
            "cells": [{"id": _cell_id(), "cell_type": "code", "metadata": {},
                       "source": ["x = 1"], "outputs": [], "execution_count": None}],
        }
        nb_path = tmp_path / "bom.ipynb"
        nb_path.write_bytes(b"\xef\xbb\xbf" + json.dumps(nb).encode("utf-8"))
        r = run_read([str(nb_path)])
        assert r.returncode == 0
        assert "x = 1" in r.stdout

    # --- Error cases ---

    def test_rejects_non_ipynb(self, tmp_path):
        p = tmp_path / "file.txt"
        p.write_text("hello")
        r = run_read([str(p)])
        assert r.returncode != 0
        assert "Error" in r.stderr or "Error" in r.stdout

    def test_missing_file(self, tmp_path):
        r = run_read([str(tmp_path / "nope.ipynb")])
        assert r.returncode != 0

    def test_malformed_json(self, tmp_path):
        p = tmp_path / "bad.ipynb"
        p.write_text("not json at all")
        r = run_read([str(p)])
        assert r.returncode != 0

    def test_missing_cells_key(self, tmp_path):
        p = tmp_path / "broken.ipynb"
        p.write_text(json.dumps({"nbformat": 4, "metadata": {}}))
        r = run_read([str(p)])
        assert r.returncode != 0
        assert "cells" in r.stdout + r.stderr

    def test_invalid_cells_spec_alpha(self, tmp_path):
        p = make_notebook([{"cell_type": "code", "source": ["x"]}], tmp_path)
        r = run_read([p, "--cells", "abc"])
        assert r.returncode != 0

    def test_invalid_cells_spec_negative(self, tmp_path):
        p = make_notebook([{"cell_type": "code", "source": ["x"]}], tmp_path)
        r = run_read([p, "--cells", "-1"])
        assert r.returncode != 0

    def test_invalid_cells_spec_open_range(self, tmp_path):
        p = make_notebook([{"cell_type": "code", "source": ["x"]}], tmp_path)
        r = run_read([p, "--cells", "0-"])
        assert r.returncode != 0

    def test_range_dos_guard(self, tmp_path):
        p = make_notebook([{"cell_type": "code", "source": ["x"]}], tmp_path)
        r = run_read([p, "--cells", "0-99999"])
        assert r.returncode != 0
        assert "10000" in r.stdout + r.stderr


# ---------------------------------------------------------------------------
# nb-write: operations
# ---------------------------------------------------------------------------

class TestNbWrite:

    def test_patch_replaces_source(self, tmp_path):
        p = make_notebook([
            {"cell_type": "code", "source": ["old code"], "execution_count": 5,
             "outputs": [{"output_type": "stream", "text": ["old output"]}]},
        ], tmp_path)
        src = tmp_path / "src.txt"
        src.write_text("new code\n")
        r = run_write([p, "patch", "0", "-f", str(src)])
        assert r.returncode == 0
        nb = read_nb(p)
        assert "".join(nb["cells"][0]["source"]) == "new code\n"

    def test_patch_clears_outputs_and_exec_count(self, tmp_path):
        p = make_notebook([
            {"cell_type": "code", "source": ["x = 1"], "execution_count": 7,
             "outputs": [{"output_type": "stream", "text": ["hi"]}]},
        ], tmp_path)
        src = tmp_path / "src.txt"
        src.write_text("x = 2\n")
        run_write([p, "patch", "0", "-f", str(src)])
        nb = read_nb(p)
        assert nb["cells"][0]["outputs"] == []
        assert nb["cells"][0]["execution_count"] is None

    def test_patch_markdown_cell_no_outputs_key(self, tmp_path):
        p = make_notebook([{"cell_type": "markdown", "source": ["## Old"]}], tmp_path)
        src = tmp_path / "src.txt"
        src.write_text("## New\n")
        r = run_write([p, "patch", "0", "-f", str(src)])
        assert r.returncode == 0
        nb = read_nb(p)
        assert "".join(nb["cells"][0]["source"]) == "## New\n"
        assert "outputs" not in nb["cells"][0]

    def test_patch_via_stdin(self, tmp_path):
        p = make_notebook([{"cell_type": "code", "source": ["old"]}], tmp_path)
        r = run_write([p, "patch", "0"], stdin="from stdin\n")
        assert r.returncode == 0
        nb = read_nb(p)
        assert "from stdin" in "".join(nb["cells"][0]["source"])

    def test_patch_out_of_range(self, tmp_path):
        p = make_notebook([{"cell_type": "code", "source": ["x"]}], tmp_path)
        r = run_write([p, "patch", "5"], stdin="y\n")
        assert r.returncode != 0

    def test_insert_before_index(self, tmp_path):
        p = make_notebook([
            {"cell_type": "code", "source": ["a = 1"]},
            {"cell_type": "code", "source": ["c = 3"]},
        ], tmp_path)
        src = tmp_path / "src.txt"
        src.write_text("b = 2\n")
        r = run_write([p, "insert", "1", "code", "-f", str(src)])
        assert r.returncode == 0
        nb = read_nb(p)
        assert len(nb["cells"]) == 3
        assert "b = 2" in "".join(nb["cells"][1]["source"])
        assert "c = 3" in "".join(nb["cells"][2]["source"])

    def test_insert_append_minus_one(self, tmp_path):
        p = make_notebook([{"cell_type": "code", "source": ["a"]}], tmp_path)
        src = tmp_path / "src.txt"
        src.write_text("## End\n")
        r = run_write([p, "insert", "-1", "markdown", "-f", str(src)])
        assert r.returncode == 0
        nb = read_nb(p)
        assert len(nb["cells"]) == 2
        assert nb["cells"][-1]["cell_type"] == "markdown"

    def test_insert_gives_cell_id(self, tmp_path):
        p = make_notebook([], tmp_path)
        src = tmp_path / "src.txt"
        src.write_text("x = 1\n")
        run_write([p, "insert", "-1", "code", "-f", str(src)])
        nb = read_nb(p)
        assert "id" in nb["cells"][0]
        assert len(nb["cells"][0]["id"]) == 8

    def test_delete_removes_correct_cell(self, tmp_path):
        p = make_notebook([
            {"cell_type": "code", "source": ["a"]},
            {"cell_type": "code", "source": ["b"]},
            {"cell_type": "code", "source": ["c"]},
        ], tmp_path)
        r = run_write([p, "delete", "1"])
        assert r.returncode == 0
        nb = read_nb(p)
        assert len(nb["cells"]) == 2
        assert "a" in "".join(nb["cells"][0]["source"])
        assert "c" in "".join(nb["cells"][1]["source"])

    def test_delete_out_of_range(self, tmp_path):
        p = make_notebook([{"cell_type": "code", "source": ["x"]}], tmp_path)
        r = run_write([p, "delete", "5"])
        assert r.returncode != 0

    def test_atomic_write_no_bak_file(self, tmp_path):
        p = make_notebook([{"cell_type": "code", "source": ["x"]}], tmp_path)
        bak = p + ".bak"
        run_write([p, "delete", "0"], stdin="")
        assert not os.path.exists(bak), ".bak must not be created"

    def test_write_is_valid_json(self, tmp_path):
        p = make_notebook([{"cell_type": "code", "source": ["x = 1"]}], tmp_path)
        src = tmp_path / "src.txt"
        src.write_text("x = 999\n")
        run_write([p, "patch", "0", "-f", str(src)])
        nb = read_nb(p)  # would raise if JSON is invalid
        assert nb["cells"][0]["source"]

    def test_all_status_to_stderr_not_stdout(self, tmp_path):
        p = make_notebook([{"cell_type": "code", "source": ["x"]}], tmp_path)
        src = tmp_path / "src.txt"
        src.write_text("y\n")
        r = run_write([p, "patch", "0", "-f", str(src)])
        assert r.stdout.strip() == "", "stdout must be empty on success"
        assert "✓" in r.stderr

    # --- Security ---

    def test_rejects_symlink(self, tmp_path):
        real = make_notebook([{"cell_type": "code", "source": ["x"]}], tmp_path)
        link = str(tmp_path / "link.ipynb")
        os.symlink(real, link)
        r = run_write([link, "patch", "0"], stdin="y\n")
        assert r.returncode != 0
        assert "symlink" in r.stderr

    def test_rejects_non_ipynb(self, tmp_path):
        p = tmp_path / "file.txt"
        p.write_text("hello")
        r = run_write([str(p), "patch", "0"], stdin="x\n")
        assert r.returncode != 0

    # --- Error handling ---

    def test_bad_index_string(self, tmp_path):
        p = make_notebook([{"cell_type": "code", "source": ["x"]}], tmp_path)
        r = run_write([p, "patch", "abc"], stdin="y\n")
        assert r.returncode != 0
        assert "integer" in r.stderr

    def test_bad_cell_type(self, tmp_path):
        p = make_notebook([{"cell_type": "code", "source": ["x"]}], tmp_path)
        r = run_write([p, "insert", "0", "script"], stdin="y\n")
        assert r.returncode != 0

    def test_unknown_operation(self, tmp_path):
        p = make_notebook([{"cell_type": "code", "source": ["x"]}], tmp_path)
        r = run_write([p, "clone", "0"], stdin="y\n")
        assert r.returncode != 0

    def test_missing_cells_key(self, tmp_path):
        p = tmp_path / "broken.ipynb"
        p.write_text(json.dumps({"nbformat": 4, "metadata": {}}))
        r = run_write([str(p), "delete", "0"])
        assert r.returncode != 0
        assert "cells" in r.stderr

    def test_source_with_eof_string_via_f_flag(self, tmp_path):
        """The word EOF on its own line must not truncate when using -f."""
        p = make_notebook([{"cell_type": "code", "source": ["old"]}], tmp_path)
        src = tmp_path / "src.txt"
        src.write_text("line1\nEOF\nline3\n")
        run_write([p, "patch", "0", "-f", str(src)])
        nb = read_nb(p)
        source_str = "".join(nb["cells"][0]["source"])
        assert "EOF" in source_str, "EOF in cell source must be preserved"
        assert "line3" in source_str, "content after EOF must be preserved"
