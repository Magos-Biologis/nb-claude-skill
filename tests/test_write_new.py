"""
Tests for new/fixed nb-write.py features:
  - `create` subcommand
  - `patch -1` error message (clear "negative index" wording)
  - PermissionError on os.replace gives a useful message
  - File locking serialises concurrent writers (POSIX only)
  - no-op patch leaves the file byte-identical and skips the reindex
  - cell ids follow the notebook's nbformat_minor (>= 4.5 only)
  - duplicate pre-existing cell ids produce a stderr warning
  - 100 MB policy: patch/delete may shrink an oversized file; growth refused
  - nb["cells"] of wrong type dies cleanly
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
        src.write_text("x = 1\n", encoding="utf-8")
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

        def raise_permission(*a, **kw):
            raise PermissionError("Access is denied")

        p_path = _make_nb([{"cell_type": "code", "source": ["x"]}], tmp_path,
                           name="locked.ipynb")
        nb, _lock = mod.load(p_path)
        if _lock is not None:
            _lock.close()  # release flock before we re-open for the mock test
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
        src0.write_text("patched_cell_0\n", encoding="utf-8")
        src1 = tmp_path / "src1.txt"
        src1.write_text("patched_cell_1\n", encoding="utf-8")

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
        src0.write_text("final_cell_0\n", encoding="utf-8")
        src1 = tmp_path / "src1.txt"
        src1.write_text("final_cell_1\n", encoding="utf-8")

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


# ---------------------------------------------------------------------------
# § no-op patch (source identical → no write, no reindex)
# ---------------------------------------------------------------------------

class TestNoOpPatch:

    def _nb_with_outputs(self, tmp_path):
        """Notebook whose cell 0 has outputs and an execution_count."""
        return _make_nb([{
            "cell_type": "code",
            "source": ["x = 1\n", "print(x)\n"],
            "outputs": [{"output_type": "stream", "name": "stdout", "text": ["1\n"]}],
            "execution_count": 7,
        }], tmp_path)

    def test_noop_patch_exits_zero_and_prints_notice(self, tmp_path):
        p = self._nb_with_outputs(tmp_path)
        src = tmp_path / "same.py"
        src.write_text("x = 1\nprint(x)\n", encoding="utf-8")

        r = run_write([p, "patch", "0", "-f", str(src)])

        assert r.returncode == 0, f"no-op patch must exit 0: {r.stderr}"
        assert "unchanged" in r.stderr and "no write" in r.stderr, (
            f"Expected 'cell 0 unchanged — no write' notice, got: {r.stderr!r}"
        )
        assert r.stdout == ""

    def test_noop_patch_leaves_file_byte_identical(self, tmp_path):
        p = self._nb_with_outputs(tmp_path)
        before_bytes = Path(p).read_bytes()
        before_mtime = os.stat(p).st_mtime_ns
        src = tmp_path / "same.py"
        src.write_text("x = 1\nprint(x)\n", encoding="utf-8")

        r = run_write([p, "patch", "0", "-f", str(src)])

        assert r.returncode == 0
        assert Path(p).read_bytes() == before_bytes, (
            "No-op patch must not rewrite the file (outputs/execution_count "
            "must be preserved)"
        )
        assert os.stat(p).st_mtime_ns == before_mtime, (
            "No-op patch must not touch the file at all (mtime changed)"
        )

    def test_noop_patch_preserves_outputs(self, tmp_path):
        p = self._nb_with_outputs(tmp_path)
        src = tmp_path / "same.py"
        src.write_text("x = 1\nprint(x)\n", encoding="utf-8")

        run_write([p, "patch", "0", "-f", str(src)])

        nb = read_nb(p)
        assert nb["cells"][0]["outputs"] != [], "Outputs must survive a no-op patch"
        assert nb["cells"][0]["execution_count"] == 7

    def test_noop_patch_skips_reindex(self, tmp_path):
        p = self._nb_with_outputs(tmp_path)
        src = tmp_path / "same.py"
        src.write_text("x = 1\nprint(x)\n", encoding="utf-8")

        r = run_write([p, "patch", "0", "-f", str(src)])

        assert r.returncode == 0
        assert not (tmp_path / ".nb_index").exists(), (
            "No-op patch must not trigger indexing"
        )

    def test_non_identical_patch_still_clears_outputs(self, tmp_path):
        """Sanity: a real patch keeps the clear-outputs behaviour."""
        p = self._nb_with_outputs(tmp_path)
        src = tmp_path / "diff.py"
        src.write_text("x = 2\n", encoding="utf-8")

        r = run_write([p, "patch", "0", "-f", str(src)])

        assert r.returncode == 0
        nb = read_nb(p)
        assert nb["cells"][0]["outputs"] == []
        assert nb["cells"][0]["execution_count"] is None


# ---------------------------------------------------------------------------
# § cell ids follow nbformat_minor (ids only for >= 4.5; never bump version)
# ---------------------------------------------------------------------------

class TestCellIdNbformatMinor:

    def _make_nb_minor(self, tmp_path, minor, with_ids):
        nb = {
            "nbformat": 4, "nbformat_minor": minor,
            "metadata": {"kernelspec": {"name": "python3", "language": "python",
                                        "display_name": "Python 3"}},
            "cells": [],
        }
        cell = {"cell_type": "code", "metadata": {}, "source": ["x = 1\n"],
                "outputs": [], "execution_count": None}
        if with_ids:
            cell["id"] = _cell_id()
        nb["cells"].append(cell)
        p = tmp_path / "minor.ipynb"
        p.write_text(json.dumps(nb, indent=1), encoding="utf-8")
        return str(p)

    def test_insert_into_minor4_emits_no_id(self, tmp_path):
        p = self._make_nb_minor(tmp_path, minor=4, with_ids=False)
        src = tmp_path / "src.py"
        src.write_text("y = 2\n", encoding="utf-8")

        r = run_write([p, "insert", "0", "code", "-f", str(src)])

        assert r.returncode == 0, r.stderr
        nb = read_nb(p)
        new_cell = nb["cells"][0]
        assert "id" not in new_cell, (
            f"nbformat 4.4 notebooks must not gain cell ids, got: {new_cell.get('id')!r}"
        )

    def test_insert_into_minor4_does_not_bump_version(self, tmp_path):
        p = self._make_nb_minor(tmp_path, minor=4, with_ids=False)
        src = tmp_path / "src.py"
        src.write_text("y = 2\n", encoding="utf-8")

        run_write([p, "insert", "0", "code", "-f", str(src)])

        nb = read_nb(p)
        assert nb["nbformat"] == 4
        assert nb["nbformat_minor"] == 4, "nbformat_minor must never be bumped"

    def test_insert_into_minor5_emits_id(self, tmp_path):
        p = self._make_nb_minor(tmp_path, minor=5, with_ids=True)
        src = tmp_path / "src.py"
        src.write_text("y = 2\n", encoding="utf-8")

        r = run_write([p, "insert", "0", "code", "-f", str(src)])

        assert r.returncode == 0, r.stderr
        nb = read_nb(p)
        cid = nb["cells"][0].get("id", "")
        assert len(cid) == 8 and cid.isalnum(), (
            f"nbformat 4.5 notebooks must get an 8-char alnum id, got: {cid!r}"
        )


# ---------------------------------------------------------------------------
# § duplicate pre-existing cell ids → one-line warning (no repair)
# ---------------------------------------------------------------------------

class TestPatchIdAutofill:
    """nbformat 4.5+ requires an id on every cell (JEP-62); patching a 4.5
    cell that lacks one must auto-fill it. Pre-4.5 cells must NOT gain ids."""

    def _nb(self, tmp_path, minor):
        nb = {
            "nbformat": 4, "nbformat_minor": minor,
            "metadata": {"kernelspec": {"name": "python3", "language": "python",
                                        "display_name": "Python 3"}},
            "cells": [{"cell_type": "code", "metadata": {}, "source": ["a = 1\n"],
                       "outputs": [], "execution_count": None}],  # no id
        }
        path = tmp_path / "t.ipynb"
        path.write_text(json.dumps(nb), encoding="utf-8")
        return path

    def test_patch_45_cell_missing_id_gets_one(self, tmp_path):
        nb_path = self._nb(tmp_path, 5)
        src = tmp_path / "s.txt"; src.write_text("b = 2\n", encoding="utf-8")
        r = run_write([str(nb_path), "patch", "0", "-f", str(src)])
        assert r.returncode == 0, r.stderr
        cell = json.loads(nb_path.read_text(encoding="utf-8"))["cells"][0]
        assert "id" in cell and 1 <= len(cell["id"]) <= 64
        assert "had no id" in r.stderr

    def test_patch_44_cell_does_not_gain_id(self, tmp_path):
        nb_path = self._nb(tmp_path, 4)
        src = tmp_path / "s.txt"; src.write_text("b = 2\n", encoding="utf-8")
        r = run_write([str(nb_path), "patch", "0", "-f", str(src)])
        assert r.returncode == 0, r.stderr
        cell = json.loads(nb_path.read_text(encoding="utf-8"))["cells"][0]
        assert "id" not in cell

    def test_noop_patch_does_not_add_id(self, tmp_path):
        """No-op patches must stay true no-ops even when an id is missing."""
        nb_path = self._nb(tmp_path, 5)
        src = tmp_path / "s.txt"; src.write_text("a = 1\n", encoding="utf-8")
        before = nb_path.read_bytes()
        r = run_write([str(nb_path), "patch", "0", "-f", str(src)])
        assert r.returncode == 0
        assert nb_path.read_bytes() == before


class TestDuplicateIdWarning:

    def _nb_with_dup_ids(self, tmp_path):
        nb = {
            "nbformat": 4, "nbformat_minor": 5,
            "metadata": {},
            "cells": [
                {"id": "dupdupd1", "cell_type": "code", "metadata": {},
                 "source": ["a = 1\n"], "outputs": [], "execution_count": None},
                {"id": "dupdupd1", "cell_type": "code", "metadata": {},
                 "source": ["b = 2\n"], "outputs": [], "execution_count": None},
            ],
        }
        p = tmp_path / "dups.ipynb"
        p.write_text(json.dumps(nb, indent=1), encoding="utf-8")
        return str(p)

    def test_duplicate_ids_warn_on_stderr(self, tmp_path):
        p = self._nb_with_dup_ids(tmp_path)
        src = tmp_path / "src.py"
        src.write_text("c = 3\n", encoding="utf-8")

        r = run_write([p, "patch", "0", "-f", str(src)])

        assert r.returncode == 0, r.stderr
        assert "duplicate" in r.stderr.lower(), (
            f"Expected duplicate-id warning on stderr, got: {r.stderr!r}"
        )
        assert "dupdupd1" in r.stderr

    def test_duplicate_ids_not_repaired(self, tmp_path):
        p = self._nb_with_dup_ids(tmp_path)
        src = tmp_path / "src.py"
        src.write_text("c = 3\n", encoding="utf-8")

        run_write([p, "patch", "0", "-f", str(src)])

        nb = read_nb(p)
        assert nb["cells"][0]["id"] == "dupdupd1"
        assert nb["cells"][1]["id"] == "dupdupd1", (
            "Duplicate ids must be warned about, not repaired"
        )

    def test_no_warning_for_unique_ids(self, tmp_path):
        p = _make_nb([{"cell_type": "code", "source": ["a\n"]},
                      {"cell_type": "code", "source": ["b\n"]}], tmp_path)
        src = tmp_path / "src.py"
        src.write_text("c = 3\n", encoding="utf-8")

        r = run_write([p, "patch", "0", "-f", str(src)])

        assert r.returncode == 0
        assert "duplicate" not in r.stderr.lower()


# ---------------------------------------------------------------------------
# § 100 MB policy: shrink allowed on oversized files, growth refused
# ---------------------------------------------------------------------------

MAX_FILE_SIZE = 100 * 1024 * 1024


def _make_oversized_nb(tmp_path, name="big.ipynb"):
    """Write a notebook just over the 100 MB limit (one huge cell + one small)."""
    huge_source = "a" * (MAX_FILE_SIZE + 2 * 1024 * 1024)  # ~102 MB cell
    nb = {
        "nbformat": 4, "nbformat_minor": 5,
        "metadata": {},
        "cells": [
            {"id": "hugecell", "cell_type": "code", "metadata": {},
             "source": [huge_source], "outputs": [], "execution_count": None},
            {"id": "tinycell", "cell_type": "code", "metadata": {},
             "source": ["x = 1\n"], "outputs": [], "execution_count": None},
        ],
    }
    p = tmp_path / name
    p.write_text(json.dumps(nb), encoding="utf-8")
    assert p.stat().st_size > MAX_FILE_SIZE
    return str(p)


class TestOversizePolicy:

    def test_delete_allowed_on_oversized_file(self, tmp_path):
        p = _make_oversized_nb(tmp_path)

        r = run_write([p, "delete", "0"])

        assert r.returncode == 0, (
            f"delete must be allowed on an oversized file (it shrinks it): {r.stderr}"
        )
        assert Path(p).stat().st_size < MAX_FILE_SIZE
        nb = read_nb(p)
        assert len(nb["cells"]) == 1
        assert nb["cells"][0]["id"] == "tinycell"

    def test_patch_shrink_allowed_on_oversized_file(self, tmp_path):
        p = _make_oversized_nb(tmp_path)
        src = tmp_path / "small.py"
        src.write_text("tiny = True\n", encoding="utf-8")

        r = run_write([p, "patch", "0", "-f", str(src)])

        assert r.returncode == 0, (
            f"shrinking patch must be allowed on an oversized file: {r.stderr}"
        )
        assert Path(p).stat().st_size < MAX_FILE_SIZE
        nb = read_nb(p)
        assert "tiny = True" in "".join(nb["cells"][0]["source"])

    def test_patch_growth_refused_on_oversized_file(self, tmp_path):
        p = _make_oversized_nb(tmp_path)
        size_before = Path(p).stat().st_size
        src = tmp_path / "grow.py"
        src.write_text("g = '" + "b" * (4 * 1024 * 1024) + "'\n", encoding="utf-8")

        r = run_write([p, "patch", "1", "-f", str(src)])

        assert r.returncode != 0, (
            "a patch that grows an already-oversized file past the limit must be refused"
        )
        assert "Traceback" not in r.stderr
        assert Path(p).stat().st_size == size_before, (
            "Refused write must leave the file untouched"
        )

    def test_insert_into_oversized_file_refused(self, tmp_path):
        p = _make_oversized_nb(tmp_path)
        src = tmp_path / "src.py"
        src.write_text("y = 2\n", encoding="utf-8")

        r = run_write([p, "insert", "0", "code", "-f", str(src)])

        assert r.returncode != 0, "insert keeps the hard 100 MB load limit"
        assert "too large" in r.stderr.lower()


# ---------------------------------------------------------------------------
# § nb["cells"] of wrong type dies cleanly
# ---------------------------------------------------------------------------

class TestCellsTypeCheck:

    def _nb_with_dict_cells(self, tmp_path):
        nb = {"nbformat": 4, "nbformat_minor": 5, "metadata": {}, "cells": {}}
        p = tmp_path / "badcells.ipynb"
        p.write_text(json.dumps(nb), encoding="utf-8")
        return str(p)

    @pytest.mark.parametrize("op_args", [
        ["patch", "0"], ["insert", "0", "code"], ["delete", "0"],
    ], ids=["patch", "insert", "delete"])
    def test_cells_as_dict_dies_cleanly(self, tmp_path, op_args):
        p = self._nb_with_dict_cells(tmp_path)
        stdin = "x\n" if op_args[0] in ("patch", "insert") else None

        r = run_write([p] + op_args, stdin=stdin)

        assert r.returncode != 0
        assert "Traceback" not in r.stderr, (
            f"cells-as-dict must die with a clean message, got:\n{r.stderr}"
        )
        assert "list" in r.stderr.lower(), (
            f"Error must mention that 'cells' should be a list: {r.stderr!r}"
        )
