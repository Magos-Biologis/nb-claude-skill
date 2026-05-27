"""
TDD tests for non-UTF-8 source file handling in nb-write.py.

These tests were written BEFORE the fix. Run them first to confirm they fail,
then implement the fix, then re-run to confirm green.

Desired behaviour (spec):
  - Source files are read as UTF-8 by default.
  - If the file contains bytes that are not valid UTF-8, fall back to latin-1
    (which accepts every possible byte value).
  - Emit a warning on stderr about the encoding fallback.
  - Still exit 0 and write the notebook — never crash with a raw traceback.
  - Content must survive the round-trip: characters decoded from latin-1 must
    appear verbatim in the patched cell source.
  - A raw Python traceback (lines starting with "Traceback") must never appear
    in stderr output.
"""

import json
import os
import string
import secrets
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS  = Path(__file__).parent.parent / "scripts"
NB_WRITE = str(SCRIPTS / "nb-write.py")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cell_id():
    return "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(8))


def make_notebook(cells, tmp_path, name="nb.ipynb"):
    nb = {
        "nbformat": 4, "nbformat_minor": 5,
        "metadata": {"kernelspec": {"name": "python3", "language": "python",
                                    "display_name": "Python 3"}},
        "cells": [],
    }
    for c in cells:
        cell = {"id": _cell_id(), "cell_type": c.get("cell_type", "code"),
                "metadata": {}, "source": c.get("source", [])}
        if cell["cell_type"] == "code":
            cell["outputs"] = c.get("outputs", [])
            cell["execution_count"] = None
        nb["cells"].append(cell)
    path = tmp_path / name
    path.write_text(json.dumps(nb, indent=1), encoding="utf-8")
    return str(path)


def read_nb(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def run_write(args, stdin=None):
    return subprocess.run(
        [sys.executable, NB_WRITE] + args,
        input=stdin, capture_output=True, text=True,
    )


# ---------------------------------------------------------------------------
# Tests: latin-1 source files
# ---------------------------------------------------------------------------

class TestLatin1SourceFile:

    def test_latin1_file_exits_zero(self, tmp_path):
        """A latin-1 encoded source file must not cause a non-zero exit."""
        p = make_notebook([{"cell_type": "code", "source": ["old"]}], tmp_path)
        src = tmp_path / "src.py"
        src.write_bytes("# Réseau d'eau\nprint('café')\n".encode("latin-1"))

        r = run_write([p, "patch", "0", "-f", str(src)])

        assert r.returncode == 0, (
            f"Expected exit 0 for latin-1 source file, got {r.returncode}.\n"
            f"stderr: {r.stderr}"
        )

    def test_latin1_content_preserved_in_notebook(self, tmp_path):
        """Characters decoded from latin-1 must appear verbatim in the patched cell."""
        p = make_notebook([{"cell_type": "code", "source": ["old"]}], tmp_path)
        src = tmp_path / "src.py"
        src.write_bytes("# naïve\nprint('ñoño')\n".encode("latin-1"))

        run_write([p, "patch", "0", "-f", str(src)])

        nb = read_nb(p)
        source_str = "".join(nb["cells"][0]["source"])
        assert "naïve" in source_str, f"'naïve' missing from patched source: {source_str!r}"
        assert "ñoño" in source_str, f"'ñoño' missing from patched source: {source_str!r}"

    def test_latin1_fallback_emits_warning_on_stderr(self, tmp_path):
        """When falling back to latin-1, a warning must appear on stderr."""
        p = make_notebook([{"cell_type": "code", "source": ["old"]}], tmp_path)
        src = tmp_path / "src.py"
        src.write_bytes("# café\n".encode("latin-1"))

        r = run_write([p, "patch", "0", "-f", str(src)])

        assert r.returncode == 0
        stderr_lower = r.stderr.lower()
        assert any(word in stderr_lower for word in ("encoding", "latin", "utf", "fallback")), (
            f"Expected an encoding-related warning on stderr, got: {r.stderr!r}"
        )

    def test_latin1_no_raw_traceback_on_stderr(self, tmp_path):
        """A UnicodeDecodeError must never surface as a raw Python traceback."""
        p = make_notebook([{"cell_type": "code", "source": ["old"]}], tmp_path)
        src = tmp_path / "src.py"
        src.write_bytes("# Ärger mit Ümlauten\n".encode("latin-1"))

        r = run_write([p, "patch", "0", "-f", str(src)])

        assert "Traceback" not in r.stderr, (
            f"Raw Python traceback must not appear in stderr:\n{r.stderr}"
        )
        assert "UnicodeDecodeError" not in r.stderr, (
            f"Raw exception class must not appear in stderr:\n{r.stderr}"
        )

    def test_latin1_insert_works(self, tmp_path):
        """latin-1 fallback must also work for the insert operation."""
        p = make_notebook([{"cell_type": "code", "source": ["a"]}], tmp_path)
        src = tmp_path / "src.py"
        src.write_bytes("# Ümlaute: äöü\npass\n".encode("latin-1"))

        r = run_write([p, "insert", "0", "code", "-f", str(src)])

        assert r.returncode == 0
        nb = read_nb(p)
        assert len(nb["cells"]) == 2
        source_str = "".join(nb["cells"][0]["source"])
        assert "äöü" in source_str

    def test_pure_ascii_file_unaffected(self, tmp_path):
        """Pure ASCII source files (valid UTF-8 subset) must still work as before."""
        p = make_notebook([{"cell_type": "code", "source": ["old"]}], tmp_path)
        src = tmp_path / "src.py"
        src.write_bytes(b"x = 42\nprint(x)\n")

        r = run_write([p, "patch", "0", "-f", str(src)])

        assert r.returncode == 0
        nb = read_nb(p)
        assert "x = 42" in "".join(nb["cells"][0]["source"])

    def test_utf8_file_unaffected(self, tmp_path):
        """Valid UTF-8 files (including multibyte CJK/emoji) must still work without warning."""
        p = make_notebook([{"cell_type": "code", "source": ["old"]}], tmp_path)
        src = tmp_path / "src.py"
        src.write_text("# 日本語\ndata = {'emoji': '🚀'}\n", encoding="utf-8")

        r = run_write([p, "patch", "0", "-f", str(src)])

        assert r.returncode == 0
        nb = read_nb(p)
        source_str = "".join(nb["cells"][0]["source"])
        assert "日本語" in source_str
        assert "🚀" in source_str

    def test_utf8_file_emits_no_encoding_warning(self, tmp_path):
        """Valid UTF-8 files must not trigger a spurious encoding fallback warning."""
        p = make_notebook([{"cell_type": "code", "source": ["old"]}], tmp_path)
        src = tmp_path / "src.py"
        src.write_text("x = 1\n", encoding="utf-8")

        r = run_write([p, "patch", "0", "-f", str(src)])

        assert r.returncode == 0
        # The only stderr output should be the success confirmation, not an encoding warning
        stderr_lower = r.stderr.lower()
        assert "encoding" not in stderr_lower, (
            f"Unexpected encoding warning for valid UTF-8 file: {r.stderr!r}"
        )

    def test_latin1_notebook_file_itself(self, tmp_path):
        """
        The notebook file itself is read with utf-8-sig. A latin-1 encoded
        notebook (unusual but possible) should give a clear JSON error, not a traceback.
        """
        nb_path = tmp_path / "latin1_nb.ipynb"
        # Write a syntactically invalid latin-1 file (not a valid notebook)
        nb_path.write_bytes(b"# not json \xe9\n")

        r = run_write([str(nb_path), "patch", "0"], stdin="x\n")

        assert r.returncode != 0
        assert "Traceback" not in r.stderr
        assert "UnicodeDecodeError" not in r.stderr
