"""
Tests for new/fixed nb-write.py features:
  - `create` subcommand
  - `patch -1` error message (clear "negative index" wording)
  - PermissionError on os.replace gives a useful message
  - File locking serialises concurrent writers (POSIX only)

All tests red until the corresponding changes are made to nb-write.py.
"""

import json
import os
import platform
import subprocess
import sys
import threading
import string
import secrets
from pathlib import Path
from unittest import mock

import pytest

SCRIPTS  = Path(__file__).parent.parent / "scripts"
NB_WRITE = str(SCRIPTS / "nb-write.py")


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


def run_write(args, stdin=None, **kw):
    return subprocess.run(
        [sys.executable, NB_WRITE] + args,
        input=stdin, capture_output=True, text=True, **kw,
    )


def read_nb(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# § create subcommand
# ---------------------------------------------------------------------------

class TestCreate:

    def test_create_produces_valid_ipynb(self, tmp_path):
        """create must write a valid nbformat 4 notebook at the given path."""
        p = str(tmp_path / "new.ipynb")
        r = run_write([p, "create"])
        assert r.returncode == 0, f"create exited {r.returncode}: {r.stderr}"
        assert Path(p).exists(), "create must write the file"
        nb = read_nb(p)
        assert nb.get("nbformat") == 4
        assert "cells" in nb
        assert isinstance(nb["cells"], list)

    def test_create_notebook_has_empty_cells(self, tmp_path):
        """The created notebook must start with zero cells."""
        p = str(tmp_path / "empty.ipynb")
        run_write([p, "create"])
        nb = read_nb(p)
        assert nb["cells"] == []

    def test_create_fails_if_file_exists(self, tmp_path):
        """create must refuse to overwrite an existing notebook."""
        p = _make_nb([{"cell_type": "code", "source": ["x = 1"]}], tmp_path)
        r = run_write([p, "create"])
        assert r.returncode != 0
        assert "exist" in r.stderr.lower() or "already" in r.stderr.lower(), (
            f"Expected 'exists'/'already' in error, got: {r.stderr!r}"
        )

    def test_create_new_notebook_can_be_appended_to(self, tmp_path):
        """After create, inserting a cell must work."""
        p = str(tmp_path / "fresh.ipynb")
        run_write([p, "create"])
        src = tmp_path / "src.txt"
        src.write_text("x = 1\n")
        r = run_write([p, "insert", "-1", "code", "-f", str(src)])
        assert r.returncode == 0
        nb = read_nb(p)
        assert len(nb["cells"]) == 1
        assert "x = 1" in "".join(nb["cells"][0]["source"])

    def test_create_only_accepted_for_ipynb(self, tmp_path):
        """create must reject non-.ipynb paths."""
        p = str(tmp_path / "new.txt")
        r = run_write([p, "create"])
        assert r.returncode != 0

    def test_create_output_on_stderr_not_stdout(self, tmp_path):
        """create success messages must go to stderr; stdout must be silent."""
        p = str(tmp_path / "nb2.ipynb")
        r = run_write([p, "create"])
        assert r.returncode == 0
        assert r.stdout.strip() == "", f"Expected empty stdout, got: {r.stdout!r}"


# ---------------------------------------------------------------------------
# § patch -1 error message
# ---------------------------------------------------------------------------

class TestPatchNegativeOneError:

    def test_patch_minus_one_mentions_negative_index(self, tmp_path):
        """patch -1 must produce an error mentioning 'negative index', not the
        generic 'patch requires: <index>' message."""
        p = _make_nb([{"cell_type": "code", "source": ["x"]}], tmp_path)
        r = run_write([p, "patch", "-1"], stdin="y\n")
        assert r.returncode != 0
        stderr_lower = r.stderr.lower()
        # Must NOT give the misleading generic message
        assert "patch requires:" not in r.stderr, (
            "Expected specific 'negative index' message, got generic: "
            + r.stderr
        )
        # Must mention negative
        assert "negative" in stderr_lower or "not supported" in stderr_lower, (
            f"Expected 'negative' or 'not supported' in error, got: {r.stderr!r}"
        )

    def test_patch_minus_five_also_clear_error(self, tmp_path):
        """patch -5 should likewise give a clear negative-index error."""
        p = _make_nb([{"cell_type": "code", "source": ["x"]}], tmp_path)
        r = run_write([p, "patch", "-5"], stdin="y\n")
        assert r.returncode != 0
        assert "negative" in r.stderr.lower() or "not supported" in r.stderr.lower()


# ---------------------------------------------------------------------------
# § PermissionError on os.replace
# ---------------------------------------------------------------------------

class TestPermissionErrorMessage:

    def test_permission_error_gives_useful_message(self, tmp_path):
        """When os.replace raises PermissionError, the error must mention
        'locked' or 'another process' or 'Jupyter'."""
        import importlib.util, types

        # Load nb-write as a module to monkey-patch os.replace
        spec = importlib.util.spec_from_file_location("nb_write", NB_WRITE)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        p = _make_nb([{"cell_path": "code", "source": ["x"]}], tmp_path)

        orig_replace = os.replace

        def raise_permission(*a, **kw):
            raise PermissionError("Access is denied")

        p_path = _make_nb([{"cell_type": "code", "source": ["x"]}], tmp_path,
                           name="locked.ipynb")
        nb = mod.load(p_path)
        with mock.patch("os.replace", side_effect=raise_permission):
            with pytest.raises(SystemExit) as exc_info:
                mod.save(p_path, nb)
        # The error message (on stderr via die()) must mention lock/process/Jupyter
        # We capture it via a StringIO redirect:
        import io
        stderr_capture = io.StringIO()
        with mock.patch("sys.stderr", stderr_capture):
            with mock.patch("os.replace", side_effect=raise_permission):
                try:
                    mod.save(p_path, nb)
                except SystemExit:
                    pass
        msg = stderr_capture.getvalue().lower()
        assert ("locked" in msg or "another process" in msg or
                "jupyter" in msg or "permission" in msg), (
            f"Expected lock-related message, got: {msg!r}"
        )


# ---------------------------------------------------------------------------
# § Concurrent write serialisation (POSIX only)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(
    platform.system() == "Windows",
    reason="fcntl file locking not available on Windows",
)
class TestConcurrentWriteSerialisation:

    def test_concurrent_patches_both_succeed(self, tmp_path):
        """Two concurrent nb-write.py patch processes must both exit 0."""
        p = _make_nb([
            {"cell_type": "code", "source": ["original_0"]},
            {"cell_type": "code", "source": ["original_1"]},
        ], tmp_path)

        src0 = tmp_path / "src0.txt"
        src0.write_text("patched_cell_0\n")
        src1 = tmp_path / "src1.txt"
        src1.write_text("patched_cell_1\n")

        results = [None, None]

        def patch_cell(idx, src, slot):
            results[slot] = run_write([p, "patch", str(idx), "-f", str(src)])

        t0 = threading.Thread(target=patch_cell, args=(0, src0, 0))
        t1 = threading.Thread(target=patch_cell, args=(1, src1, 1))
        t0.start(); t1.start()
        t0.join(); t1.join()

        assert results[0].returncode == 0, f"Thread 0 failed: {results[0].stderr}"
        assert results[1].returncode == 0, f"Thread 1 failed: {results[1].stderr}"

    def test_concurrent_patches_no_data_loss(self, tmp_path):
        """After two concurrent patches, both cell changes must be present."""
        p = _make_nb([
            {"cell_type": "code", "source": ["original_0"]},
            {"cell_type": "code", "source": ["original_1"]},
        ], tmp_path)

        src0 = tmp_path / "src0.txt"
        src0.write_text("final_cell_0\n")
        src1 = tmp_path / "src1.txt"
        src1.write_text("final_cell_1\n")

        threads = []
        for idx, src in [(0, src0), (1, src1)]:
            t = threading.Thread(
                target=lambda i=idx, s=src: run_write([p, "patch", str(i), "-f", str(s)])
            )
            threads.append(t)

        for t in threads: t.start()
        for t in threads: t.join()

        nb = read_nb(p)
        src_0 = "".join(nb["cells"][0]["source"])
        src_1 = "".join(nb["cells"][1]["source"])
        assert "final_cell_0" in src_0, f"Cell 0 lost: {src_0!r}"
        assert "final_cell_1" in src_1, f"Cell 1 lost: {src_1!r}"
