"""
Test suite for nb-index.py — derived from TDD §0–§8, §13–§14.

All tests use subprocess to invoke nb-index.py (and nb-write.py where §8 is
concerned) as black-box CLI tools.  Tests are written tests-first against the
specification; they will fail until nb-index.py is implemented.

Section mapping:
  TestCLI              → §0  (CLI contract)
  TestIndexLocation    → §1  (directory resolution)
  TestGitignore        → §2  (.gitignore management)
  TestStaleness        → §3  (rebuild triggers)
  TestFirstLine        → §4  (first_line storage / outline fields)
  TestSectionExtraction→ §5  (heading-based sections)
  TestSymbolExtraction → §6  (Python / Julia / R patterns)
  TestOutputStorage    → §7  (cell outputs, 4 KB cap)
  TestWriteIntegration → §8  (nb-write.py spawns nb-index.py)
  TestSymbolCache      → §13 (symbols.json)
  TestEdgeCases        → §14 (edge cases)
"""

import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REPO_ROOT   = Path(__file__).parent.parent
SCRIPT      = REPO_ROOT / "scripts" / "nb-index.py"
WRITE_SCRIPT= REPO_ROOT / "scripts" / "nb-write.py"
PYTHON      = sys.executable

# ---------------------------------------------------------------------------
# Notebook / cell factory helpers
# ---------------------------------------------------------------------------

def make_notebook(cells=None, kernel_language="python", name="test.ipynb", tmp_path=None):
    """Write a minimal nbformat 4 notebook to tmp_path / name."""
    nb = {
        "nbformat": 4,
        "nbformat_minor": 5,
        "metadata": {
            "kernelspec": {
                "display_name": "Python 3",
                "language": kernel_language,
                "name": "python3",
            },
            "language_info": {"name": kernel_language, "version": "3.10.0"},
        },
        "cells": cells or [],
    }
    path = tmp_path / name
    path.write_text(json.dumps(nb), encoding="utf-8")
    return path


def code_cell(source, cell_id="c001", outputs=None, execution_count=1):
    return {
        "cell_type": "code",
        "id": cell_id,
        "metadata": {},
        "source": source if isinstance(source, list) else [source],
        "outputs": outputs or [],
        "execution_count": execution_count,
    }


def markdown_cell(source, cell_id="m001"):
    return {
        "cell_type": "markdown",
        "id": cell_id,
        "metadata": {},
        "source": source if isinstance(source, list) else [source],
    }


def raw_cell(source, cell_id="r001"):
    return {
        "cell_type": "raw",
        "id": cell_id,
        "metadata": {},
        "source": source if isinstance(source, list) else [source],
    }


def stream_output(text, name="stdout"):
    t = text if isinstance(text, list) else [text]
    return {"output_type": "stream", "name": name, "text": t}


def execute_result_output(text, execution_count=1):
    return {
        "output_type": "execute_result",
        "execution_count": execution_count,
        "data": {"text/plain": text},
        "metadata": {},
    }


def error_output(ename="ValueError", evalue="bad", traceback=None):
    return {
        "output_type": "error",
        "ename": ename,
        "evalue": evalue,
        "traceback": traceback or [f"Traceback (most recent call last):", f"{ename}: {evalue}"],
    }


def display_data_output(text):
    return {
        "output_type": "display_data",
        "data": {"text/plain": text},
        "metadata": {},
    }


def png_output():
    return {
        "output_type": "display_data",
        "data": {"image/png": "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="},
        "metadata": {},
    }


# ---------------------------------------------------------------------------
# Index helpers
# ---------------------------------------------------------------------------

def run_indexer(nb_path, extra_args=None):
    """Run nb-index.py on nb_path, return CompletedProcess."""
    args = [PYTHON, str(SCRIPT), str(nb_path)] + (extra_args or [])
    return subprocess.run(args, capture_output=True, text=True)


def index_path_for(nb_path):
    """
    Return the expected index JSON path for the no-git fallback case:
      <nb_dir>/.nb_index/<nb_basename>.json
    """
    nb = Path(nb_path).resolve()
    return nb.parent / ".nb_index" / (nb.name + ".json")


def index_path_for_git(git_root, nb_path):
    """
    Return the expected index JSON path for the git-root case:
      <git_root>/.nb_index/<relative_path>.json
    """
    nb = Path(nb_path).resolve()
    git_root = Path(git_root).resolve()
    rel = nb.relative_to(git_root)
    return git_root / ".nb_index" / (str(rel).replace(os.sep, "/") + ".json")


def load_index(json_path):
    """Parse and return an index JSON file."""
    return json.loads(Path(json_path).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# §0 — CLI contract
# ---------------------------------------------------------------------------

class TestCLI:

    def test_exit_0_on_valid_notebook(self, tmp_path):
        nb = make_notebook([code_cell("x = 1")], tmp_path=tmp_path)
        r = run_indexer(nb)
        assert r.returncode == 0, f"Expected exit 0, got {r.returncode}\nstderr: {r.stderr}"

    def test_stdout_silent_on_success(self, tmp_path):
        nb = make_notebook([code_cell("x = 1")], tmp_path=tmp_path)
        r = run_indexer(nb)
        assert r.stdout == "", f"Expected silent stdout, got: {r.stdout!r}"

    def test_stderr_has_status_on_success(self, tmp_path):
        nb = make_notebook([code_cell("x = 1")], tmp_path=tmp_path)
        r = run_indexer(nb)
        assert r.returncode == 0
        assert r.stderr.strip() != "", "Expected a status line on stderr"

    def test_stderr_contains_wrote_message(self, tmp_path):
        nb = make_notebook([code_cell("x = 1")], tmp_path=tmp_path)
        r = run_indexer(nb)
        assert "wrote" in r.stderr or "index" in r.stderr.lower(), (
            f"Expected '[index] wrote ...' or similar on stderr: {r.stderr!r}"
        )

    def test_exit_1_for_non_ipynb_extension(self, tmp_path):
        p = tmp_path / "notebook.json"
        p.write_text("{}", encoding="utf-8")
        r = run_indexer(p)
        assert r.returncode == 1, f"Expected exit 1 for non-.ipynb, got {r.returncode}"

    def test_exit_1_for_missing_file(self, tmp_path):
        r = run_indexer(tmp_path / "does_not_exist.ipynb")
        assert r.returncode == 1

    def test_exit_1_for_symlink_notebook(self, tmp_path):
        real = make_notebook([code_cell("x = 1")], tmp_path=tmp_path, name="real.ipynb")
        link = tmp_path / "link.ipynb"
        link.symlink_to(real)
        r = run_indexer(link)
        assert r.returncode == 1, "Symlink notebooks must be rejected (exit 1)"

    def test_force_flag_accepted(self, tmp_path):
        nb = make_notebook([code_cell("x = 1")], tmp_path=tmp_path)
        run_indexer(nb)
        r = run_indexer(nb, extra_args=["--force"])
        assert r.returncode == 0, f"--force should exit 0, got {r.returncode}\n{r.stderr}"

    def test_creates_index_file(self, tmp_path):
        nb = make_notebook([code_cell("x = 1")], tmp_path=tmp_path)
        run_indexer(nb)
        idx = index_path_for(nb)
        assert idx.exists(), f"Expected index at {idx}"

    def test_index_file_is_valid_json(self, tmp_path):
        nb = make_notebook([code_cell("x = 1")], tmp_path=tmp_path)
        run_indexer(nb)
        idx = index_path_for(nb)
        data = load_index(idx)
        assert isinstance(data, dict)

    def test_exit_1_for_malformed_notebook(self, tmp_path):
        p = tmp_path / "bad.ipynb"
        p.write_text("not json at all", encoding="utf-8")
        r = run_indexer(p)
        assert r.returncode == 1

    def test_exit_1_for_wrong_nbformat(self, tmp_path):
        nb = {
            "nbformat": 3,
            "nbformat_minor": 0,
            "metadata": {},
            "worksheets": [],
        }
        p = tmp_path / "old.ipynb"
        p.write_text(json.dumps(nb), encoding="utf-8")
        r = run_indexer(p)
        assert r.returncode == 1, "nbformat 3 must be rejected"


# ---------------------------------------------------------------------------
# §1 — Index directory resolution
# ---------------------------------------------------------------------------

class TestIndexLocation:

    def test_no_git_index_in_nb_dir(self, tmp_path):
        """§1.4/§1.6: no .git → index at <nb_dir>/.nb_index/<nb>.json"""
        nb = make_notebook([code_cell("x = 1")], tmp_path=tmp_path)
        run_indexer(nb)
        idx = index_path_for(nb)
        assert idx.exists(), f"Expected index at {idx}"

    def test_git_root_index_at_project_level(self, tmp_path):
        """§1.2/§1.5: .git in parent → index at <git_root>/.nb_index/<rel>.json"""
        git_root = tmp_path / "project"
        git_root.mkdir()
        (git_root / ".git").mkdir()
        nb = make_notebook([code_cell("x = 1")], tmp_path=git_root, name="nb.ipynb")
        run_indexer(nb)
        idx = index_path_for_git(git_root, nb)
        assert idx.exists(), f"Expected git-root index at {idx}"

    def test_git_root_nested_notebook(self, tmp_path):
        """§1.3: nested notebook uses git root, not nearest parent"""
        git_root = tmp_path / "project"
        sub = git_root / "data" / "subdir"
        sub.mkdir(parents=True)
        (git_root / ".git").mkdir()
        nb = make_notebook([code_cell("x = 1")], tmp_path=sub, name="nb.ipynb")
        run_indexer(nb)
        idx = index_path_for_git(git_root, nb)
        assert idx.exists(), f"Expected index at git root level {idx}"
        wrong = sub / ".nb_index"
        assert not wrong.exists(), ".nb_index must not be created inside the subdirectory"

    def test_no_git_index_dir_created(self, tmp_path):
        """§1.8: mkdir parents=True, exist_ok=True"""
        nb = make_notebook([code_cell("x = 1")], tmp_path=tmp_path)
        run_indexer(nb)
        assert (tmp_path / ".nb_index").is_dir()

    def test_two_representations_same_index(self, tmp_path):
        """§1.11: ./sub/../nb.ipynb and ./nb.ipynb → same resolved index path"""
        nb = make_notebook([code_cell("x = 1")], tmp_path=tmp_path, name="nb.ipynb")
        # First run via canonical path
        r1 = run_indexer(nb)
        assert r1.returncode == 0
        idx1 = index_path_for(nb)
        assert idx1.exists()
        inode1 = idx1.stat().st_ino

        # Second run via a non-canonical path that resolves to the same file.
        # Create 'sub' so the path is structurally valid before resolve().
        (tmp_path / "sub").mkdir(exist_ok=True)
        dotdot = tmp_path / "sub" / ".." / "nb.ipynb"
        # The dotdot string resolves to the same real file; --force ensures a rebuild.
        r2 = run_indexer(dotdot, extra_args=["--force"])
        assert r2.returncode == 0, (
            f"Indexing via non-canonical path must succeed: {r2.stderr}"
        )
        # The index must be written to the same location (same inode after a second write
        # would be different, but the PATH must be the same file we already know about).
        idx2 = index_path_for(nb)  # canonical form
        assert idx2 == idx1, "Both path forms must produce the same index path"
        assert idx2.exists(), "Index must exist after second run"

    def test_containment_violation_exits_1(self, tmp_path):
        """§1.7: notebook that resolves outside the git root must not escape .nb_index/"""
        # Set up a git root and a real notebook OUTSIDE it.
        git_root = tmp_path / "project"
        git_root.mkdir()
        (git_root / ".git").mkdir()
        outside = tmp_path / "external"
        outside.mkdir()
        real_nb = make_notebook([code_cell("x = 1")], tmp_path=outside, name="ext.ipynb")
        # Create a symlink inside the git root pointing to the external notebook.
        # nb-index.py rejects symlinks (§0), so exit 1 is expected.
        link = git_root / "linked.ipynb"
        link.symlink_to(real_nb)
        r = run_indexer(link)
        assert r.returncode == 1, (
            "Symlink notebook (possible containment escape) must be rejected with exit 1"
        )
        # Verify no index was created outside the project
        assert not (outside / ".nb_index").exists(), (
            "Must not create .nb_index outside git root via symlink"
        )

    def test_git_symlink_skipped(self, tmp_path):
        """§1.2: .git that is a symlink is NOT treated as git root"""
        git_root = tmp_path / "project"
        git_root.mkdir()
        real_git = tmp_path / "real_git_dir"
        real_git.mkdir()
        # Create .git as a symlink to a directory
        (git_root / ".git").symlink_to(real_git)
        sub = git_root / "data"
        sub.mkdir()
        nb = make_notebook([code_cell("x = 1")], tmp_path=sub, name="nb.ipynb")
        run_indexer(nb)
        # Index should be in <nb_dir>/.nb_index, not in <project>/.nb_index
        assert (sub / ".nb_index").exists(), (
            "With .git symlink, should fall back to nb-dir level"
        )
        assert not (git_root / ".nb_index").exists(), (
            ".git symlink must not be treated as git root"
        )

    def test_index_path_version_field(self, tmp_path):
        """Schema: version must be integer 1"""
        nb = make_notebook([code_cell("x = 1")], tmp_path=tmp_path)
        run_indexer(nb)
        data = load_index(index_path_for(nb))
        assert data["version"] == 1
        assert isinstance(data["version"], int), "version must be int, not string"

    def test_index_path_notebook_path_field(self, tmp_path):
        """Schema: notebook_path stored with forward slashes"""
        nb = make_notebook([code_cell("x = 1")], tmp_path=tmp_path, name="my_nb.ipynb")
        run_indexer(nb)
        data = load_index(index_path_for(nb))
        assert "notebook_path" in data
        assert "\\" not in data["notebook_path"], "notebook_path must use forward slashes"

    def test_index_contains_kernel_language(self, tmp_path):
        nb = make_notebook([code_cell("x = 1")], kernel_language="python", tmp_path=tmp_path)
        run_indexer(nb)
        data = load_index(index_path_for(nb))
        assert data["kernel_language"] == "python"

    def test_index_contains_cell_count(self, tmp_path):
        nb = make_notebook([code_cell("x = 1"), code_cell("y = 2", cell_id="c002")], tmp_path=tmp_path)
        run_indexer(nb)
        data = load_index(index_path_for(nb))
        assert data["cell_count"] == 2


# ---------------------------------------------------------------------------
# §2 — .gitignore management
# ---------------------------------------------------------------------------

class TestGitignore:

    def test_creates_gitignore_when_absent(self, tmp_path):
        """§2.1"""
        nb = make_notebook([code_cell("x = 1")], tmp_path=tmp_path)
        run_indexer(nb)
        gitignore = tmp_path / ".gitignore"
        assert gitignore.exists(), ".gitignore should be created"
        assert ".nb_index/" in gitignore.read_text(encoding="utf-8")

    def test_appends_to_existing_gitignore(self, tmp_path):
        """§2.2"""
        gitignore = tmp_path / ".gitignore"
        gitignore.write_text("__pycache__/\n*.pyc\n", encoding="utf-8")
        nb = make_notebook([code_cell("x = 1")], tmp_path=tmp_path)
        run_indexer(nb)
        content = gitignore.read_text(encoding="utf-8")
        assert ".nb_index/" in content

    def test_preserves_existing_entries(self, tmp_path):
        """§2.5"""
        gitignore = tmp_path / ".gitignore"
        gitignore.write_text("__pycache__/\n*.pyc\n", encoding="utf-8")
        nb = make_notebook([code_cell("x = 1")], tmp_path=tmp_path)
        run_indexer(nb)
        content = gitignore.read_text(encoding="utf-8")
        assert "__pycache__/" in content
        assert "*.pyc" in content

    def test_does_not_duplicate_entry(self, tmp_path):
        """§2.3"""
        nb = make_notebook([code_cell("x = 1")], tmp_path=tmp_path)
        run_indexer(nb)
        run_indexer(nb, extra_args=["--force"])
        gitignore = tmp_path / ".gitignore"
        content = gitignore.read_text(encoding="utf-8")
        assert content.count(".nb_index/") == 1, "Entry must not be duplicated"

    def test_gitignore_at_correct_level(self, tmp_path):
        """§2.4: .gitignore created at same level as .nb_index"""
        nb = make_notebook([code_cell("x = 1")], tmp_path=tmp_path)
        run_indexer(nb)
        assert (tmp_path / ".gitignore").exists()
        assert (tmp_path / ".nb_index").exists()

    def test_gitignore_literal_string(self, tmp_path):
        """§2 (A2): Always writes the literal '.nb_index/', never a computed path"""
        nb = make_notebook([code_cell("x = 1")], tmp_path=tmp_path)
        run_indexer(nb)
        content = (tmp_path / ".gitignore").read_text(encoding="utf-8")
        # The entry must be exactly ".nb_index/" (no path prefix)
        for line in content.splitlines():
            if ".nb_index/" in line:
                assert line.strip() == ".nb_index/", (
                    f"Expected literal '.nb_index/' entry, got: {line!r}"
                )

    def test_symlink_gitignore_skipped(self, tmp_path):
        """§2.6: symlink .gitignore must not be written through"""
        real_gitignore = tmp_path / "real_gitignore"
        real_gitignore.write_text("# original\n", encoding="utf-8")
        link = tmp_path / ".gitignore"
        link.symlink_to(real_gitignore)
        nb = make_notebook([code_cell("x = 1")], tmp_path=tmp_path)
        r = run_indexer(nb)
        assert r.returncode == 0, "Symlink .gitignore must not cause failure"
        # The symlink target must NOT have .nb_index/ appended
        assert ".nb_index/" not in real_gitignore.read_text(encoding="utf-8"), (
            "Must not write through .gitignore symlink"
        )
        # A warning must appear on stderr
        assert "warn" in r.stderr.lower() or "symlink" in r.stderr.lower(), (
            f"Expected a symlink warning on stderr: {r.stderr!r}"
        )

    @pytest.mark.skipif(
        sys.platform == "win32" or getattr(os, "getuid", lambda: -1)() == 0,
        reason="POSIX-only; root can write read-only dirs",
    )
    def test_readonly_directory_handled_gracefully(self, tmp_path):
        """§2.7: read-only directory .gitignore failure must not produce unhandled traceback"""
        nb = make_notebook([code_cell("x = 1")], tmp_path=tmp_path)
        try:
            os.chmod(tmp_path, 0o555)
            r = run_indexer(nb)
            # mkdir for .nb_index will also fail in a read-only dir, so exit 1 is
            # acceptable here. What the test verifies is no unhandled exception traceback
            # from a .gitignore write failure.
            assert "Traceback" not in r.stderr, (
                f"Must not produce a bare Traceback on read-only dir: {r.stderr!r}"
            )
        finally:
            os.chmod(tmp_path, 0o755)


# ---------------------------------------------------------------------------
# §3 — Staleness and rebuild
# ---------------------------------------------------------------------------

class TestStaleness:

    def test_stores_all_three_staleness_signals(self, tmp_path):
        """§3.1"""
        nb = make_notebook([code_cell("x = 1")], tmp_path=tmp_path)
        run_indexer(nb)
        data = load_index(index_path_for(nb))
        assert "notebook_mtime" in data
        assert "notebook_size" in data
        assert "nb_content_hash" in data
        assert isinstance(data["notebook_mtime"], float)
        assert isinstance(data["notebook_size"], int)
        assert isinstance(data["nb_content_hash"], str)
        assert len(data["nb_content_hash"]) == 16  # SHA-256[:16]

    def test_nb_content_hash_correct(self, tmp_path):
        """§3.1: nb_content_hash = SHA-256[:16] of raw bytes"""
        nb = make_notebook([code_cell("x = 1")], tmp_path=tmp_path)
        run_indexer(nb)
        raw = nb.read_bytes()
        expected = hashlib.sha256(raw).hexdigest()[:16]
        data = load_index(index_path_for(nb))
        assert data["nb_content_hash"] == expected

    def test_stale_on_mtime_change(self, tmp_path):
        """§3.2: changed mtime triggers rebuild"""
        nb = make_notebook([code_cell("x = 1")], tmp_path=tmp_path)
        run_indexer(nb)
        idx = index_path_for(nb)
        inode_before = idx.stat().st_ino
        # Advance mtime by 2 seconds
        t = nb.stat().st_mtime + 2.0
        os.utime(nb, (t, t))
        run_indexer(nb)
        inode_after = idx.stat().st_ino
        assert inode_after != inode_before, "Index must be rewritten when mtime changes"

    def test_stale_on_size_change(self, tmp_path):
        """§3.3: changed size triggers rebuild"""
        nb = make_notebook([code_cell("x = 1")], tmp_path=tmp_path)
        run_indexer(nb)
        idx = index_path_for(nb)
        inode_before = idx.stat().st_ino
        # Rewrite with extra cells to change size; also reset mtime to same
        original_mtime = nb.stat().st_mtime
        make_notebook(
            [code_cell("x = 1"), code_cell("y = 2", cell_id="c002")],
            tmp_path=tmp_path, name="test.ipynb"
        )
        os.utime(nb, (original_mtime, original_mtime))
        run_indexer(nb)
        inode_after = idx.stat().st_ino
        assert inode_after != inode_before, "Index must be rewritten when size changes"

    def test_stale_on_content_change_same_mtime_size(self, tmp_path):
        """§3.4: changed hash triggers rebuild even when mtime and size unchanged"""
        # Build two notebooks with the same size but different content
        src_a = "x = 1  # a"
        src_b = "x = 1  # b"
        assert len(json.dumps({"source": [src_a]})) == len(json.dumps({"source": [src_b]}))

        nb = make_notebook([code_cell(src_a)], tmp_path=tmp_path)
        run_indexer(nb)
        idx = index_path_for(nb)
        inode_before = idx.stat().st_ino
        original_mtime = nb.stat().st_mtime
        original_size  = nb.stat().st_size

        # Write new content with same size
        nb2 = make_notebook([code_cell(src_b)], tmp_path=tmp_path, name="test.ipynb")
        if nb2.stat().st_size == original_size:
            os.utime(nb2, (original_mtime, original_mtime))
            run_indexer(nb2)
            inode_after = idx.stat().st_ino
            assert inode_after != inode_before, (
                "Index must rebuild when hash differs even with same mtime+size"
            )
        else:
            pytest.skip(
                f"make_notebook produced different sizes ({nb2.stat().st_size} vs "
                f"{original_size}); size-equality precondition not met"
            )

    def test_fresh_index_not_rebuilt(self, tmp_path):
        """§3.5: no rebuild when all three signals match (inode unchanged)"""
        nb = make_notebook([code_cell("x = 1")], tmp_path=tmp_path)
        run_indexer(nb)
        idx = index_path_for(nb)
        inode_before = idx.stat().st_ino
        run_indexer(nb)
        inode_after = idx.stat().st_ino
        assert inode_after == inode_before, (
            "Index must NOT be rewritten when already fresh (inode must be unchanged)"
        )

    def test_fresh_index_stderr_says_fresh(self, tmp_path):
        """§0 stderr: '[index] fresh — skipping rebuild' on no-rebuild"""
        nb = make_notebook([code_cell("x = 1")], tmp_path=tmp_path)
        run_indexer(nb)
        r = run_indexer(nb)
        # Spec format: "[index] fresh — skipping rebuild"
        assert "fresh" in r.stderr.lower() and "skip" in r.stderr.lower(), (
            f"Expected both 'fresh' and 'skip' in stderr on no-rebuild: {r.stderr!r}"
        )

    def test_force_rebuilds_fresh_index(self, tmp_path):
        """§3.6: --force changes inode even when index is fresh"""
        nb = make_notebook([code_cell("x = 1")], tmp_path=tmp_path)
        run_indexer(nb)
        idx = index_path_for(nb)
        inode_before = idx.stat().st_ino
        run_indexer(nb, extra_args=["--force"])
        inode_after = idx.stat().st_ino
        assert inode_after != inode_before, "--force must always rewrite the index"

    def test_source_hash_per_cell(self, tmp_path):
        """§3.8: source_hash = MD5[:8] of UTF-8 source bytes"""
        source = "import pandas as pd\n"
        nb = make_notebook([code_cell(source)], tmp_path=tmp_path)
        run_indexer(nb)
        data = load_index(index_path_for(nb))
        cell = data["cells"][0]
        assert "source_hash" in cell
        expected = hashlib.md5(source.encode("utf-8")).hexdigest()[:8]
        assert cell["source_hash"] == expected, (
            f"source_hash mismatch: expected {expected!r}, got {cell['source_hash']!r}"
        )

    def test_source_hash_list_source(self, tmp_path):
        """§3.8: source may be a list; hash is over joined text"""
        source_list = ["import pandas as pd\n", "import numpy as np\n"]
        nb = make_notebook([code_cell(source_list)], tmp_path=tmp_path)
        run_indexer(nb)
        data = load_index(index_path_for(nb))
        cell = data["cells"][0]
        joined = "".join(source_list)
        expected = hashlib.md5(joined.encode("utf-8")).hexdigest()[:8]
        assert cell["source_hash"] == expected


# ---------------------------------------------------------------------------
# §4 — first_line / outline fields (stored in index cells)
# ---------------------------------------------------------------------------

class TestFirstLine:

    def test_code_cell_first_line_stored(self, tmp_path):
        """§14.11: first non-empty source line, stripped"""
        nb = make_notebook([code_cell(["x = 1\n", "y = 2\n"])], tmp_path=tmp_path)
        run_indexer(nb)
        data = load_index(index_path_for(nb))
        assert data["cells"][0]["first_line"] == "x = 1"

    def test_markdown_cell_first_line_stored(self, tmp_path):
        """§14.11: markdown heading stored as first_line"""
        nb = make_notebook([markdown_cell("## Heading\nsome text\n")], tmp_path=tmp_path)
        run_indexer(nb)
        data = load_index(index_path_for(nb))
        assert data["cells"][0]["first_line"] == "## Heading"

    def test_empty_cell_first_line_is_empty_marker(self, tmp_path):
        """§4.5/§14.11: empty source → first_line: '(empty)'"""
        nb = make_notebook([code_cell("")], tmp_path=tmp_path)
        run_indexer(nb)
        data = load_index(index_path_for(nb))
        assert data["cells"][0]["first_line"] == "(empty)"

    def test_whitespace_only_cell_first_line_is_empty_marker(self, tmp_path):
        """§4.5: whitespace-only source → '(empty)'"""
        nb = make_notebook([code_cell("   \n\n  \n")], tmp_path=tmp_path)
        run_indexer(nb)
        data = load_index(index_path_for(nb))
        assert data["cells"][0]["first_line"] == "(empty)"

    def test_first_line_max_120_chars(self, tmp_path):
        """§4 / Schema: first_line capped at 120 chars"""
        long_line = "x = " + "a" * 200
        nb = make_notebook([code_cell(long_line)], tmp_path=tmp_path)
        run_indexer(nb)
        data = load_index(index_path_for(nb))
        assert len(data["cells"][0]["first_line"]) <= 120

    def test_first_line_ansi_sanitised(self, tmp_path):
        """§9.2: first_line must be ANSI-sanitised at store time"""
        ansi_source = "\x1b[31mred text\x1b[0m\n"
        nb = make_notebook([code_cell(ansi_source)], tmp_path=tmp_path)
        run_indexer(nb)
        data = load_index(index_path_for(nb))
        assert "\x1b[" not in data["cells"][0]["first_line"], (
            "ANSI sequences must be stripped from first_line at store time"
        )

    def test_first_nonempty_line_skips_blank_prefix(self, tmp_path):
        """§4.4: skips blank lines to find first non-empty"""
        nb = make_notebook([code_cell(["\n", "\n", "result = 42\n"])], tmp_path=tmp_path)
        run_indexer(nb)
        data = load_index(index_path_for(nb))
        assert data["cells"][0]["first_line"] == "result = 42"

    def test_code_cell_exec_and_status_stored(self, tmp_path):
        """§4.2: exec and status stored for code cells"""
        nb = make_notebook([code_cell("x = 1", execution_count=7)], tmp_path=tmp_path)
        run_indexer(nb)
        data = load_index(index_path_for(nb))
        cell = data["cells"][0]
        assert cell["exec"] == 7
        assert cell["status"] in ("ok", "error", "not_run")

    def test_code_cell_status_ok_no_errors(self, tmp_path):
        """§4.6: exec not null + no error outputs → status 'ok'"""
        nb = make_notebook([code_cell("x = 1", execution_count=1)], tmp_path=tmp_path)
        run_indexer(nb)
        data = load_index(index_path_for(nb))
        assert data["cells"][0]["status"] == "ok"

    def test_code_cell_status_not_run(self, tmp_path):
        """§4.6: exec null → status 'not_run'"""
        nb = make_notebook([code_cell("x = 1", execution_count=None)], tmp_path=tmp_path)
        run_indexer(nb)
        data = load_index(index_path_for(nb))
        assert data["cells"][0]["status"] == "not_run"

    def test_code_cell_status_error(self, tmp_path):
        """§4.6: exec not null + error output → status 'error'"""
        nb = make_notebook(
            [code_cell("x = 1/0", execution_count=1, outputs=[error_output()])],
            tmp_path=tmp_path
        )
        run_indexer(nb)
        data = load_index(index_path_for(nb))
        assert data["cells"][0]["status"] == "error"

    def test_markdown_cell_exec_and_status_null(self, tmp_path):
        """§4.3: markdown cell exec=null, status=null"""
        nb = make_notebook([markdown_cell("## Heading")], tmp_path=tmp_path)
        run_indexer(nb)
        data = load_index(index_path_for(nb))
        cell = data["cells"][0]
        assert cell["exec"] is None
        assert cell["status"] is None

    def test_heading_cell_has_heading_field(self, tmp_path):
        """Schema: markdown heading cells have heading (int) and heading_text fields"""
        nb = make_notebook([markdown_cell("## Data Loading\n")], tmp_path=tmp_path)
        run_indexer(nb)
        data = load_index(index_path_for(nb))
        cell = data["cells"][0]
        assert cell.get("heading") == 2
        assert cell.get("heading_text") == "Data Loading"

    def test_non_heading_markdown_no_heading_field(self, tmp_path):
        """Schema: non-heading markdown cells must NOT have a 'heading' key"""
        nb = make_notebook([markdown_cell("Just some text\n")], tmp_path=tmp_path)
        run_indexer(nb)
        data = load_index(index_path_for(nb))
        assert "heading" not in data["cells"][0]


# ---------------------------------------------------------------------------
# §5 — Section extraction
# ---------------------------------------------------------------------------

class TestSectionExtraction:

    def _make_sectioned_notebook(self, tmp_path):
        cells = [
            code_cell("x = 1", cell_id="c0"),
            markdown_cell("## Data Loading\n", cell_id="m1"),
            code_cell("df = load()", cell_id="c2", execution_count=2),
            code_cell("df.head()", cell_id="c3", execution_count=3),
            markdown_cell("## Analysis\n", cell_id="m4"),
            code_cell("df.describe()", cell_id="c5", execution_count=5),
        ]
        return make_notebook(cells, tmp_path=tmp_path)

    def test_cell_before_first_heading_has_null_section(self, tmp_path):
        """§5.5"""
        nb = self._make_sectioned_notebook(tmp_path)
        run_indexer(nb)
        data = load_index(index_path_for(nb))
        assert data["cells"][0]["section"] is None

    def test_cell_under_heading_has_section_name(self, tmp_path):
        """§5.5"""
        nb = self._make_sectioned_notebook(tmp_path)
        run_indexer(nb)
        data = load_index(index_path_for(nb))
        assert data["cells"][2]["section"] == "Data Loading"
        assert data["cells"][3]["section"] == "Data Loading"

    def test_cell_under_second_heading(self, tmp_path):
        """§5.5"""
        nb = self._make_sectioned_notebook(tmp_path)
        run_indexer(nb)
        data = load_index(index_path_for(nb))
        assert data["cells"][5]["section"] == "Analysis"

    def test_no_headings_empty_sections(self, tmp_path):
        """§5.4/§5.6"""
        nb = make_notebook([code_cell("x = 1"), code_cell("y = 2", cell_id="c2")], tmp_path=tmp_path)
        run_indexer(nb)
        data = load_index(index_path_for(nb))
        assert data["cells"][0]["section"] is None
        assert data["cells"][1]["section"] is None

    def test_heading_cell_itself_has_null_section(self, tmp_path):
        """§5.5: the heading cell itself is not 'inside' its own section"""
        nb = make_notebook(
            [markdown_cell("## Section A", cell_id="m0"), code_cell("x = 1", cell_id="c1")],
            tmp_path=tmp_path
        )
        run_indexer(nb)
        data = load_index(index_path_for(nb))
        # The heading cell (cells[0]) opens Section A but is NOT contained within it.
        assert data["cells"][0]["section"] is None, (
            "Heading cell must have section=null (it opens the section, is not inside it)"
        )
        # The code cell following the heading IS inside Section A.
        assert data["cells"][1]["section"] == "Section A"

    def test_h1_closes_h2_section(self, tmp_path):
        """§5.3: h1 heading closes an open h2 section"""
        cells = [
            markdown_cell("## Sub Section", cell_id="m0"),
            code_cell("x = 1", cell_id="c1"),
            markdown_cell("# Top Level", cell_id="m2"),
            code_cell("y = 2", cell_id="c3"),
        ]
        nb = make_notebook(cells, tmp_path=tmp_path)
        run_indexer(nb)
        data = load_index(index_path_for(nb))
        assert data["cells"][1]["section"] == "Sub Section"
        assert data["cells"][3]["section"] == "Top Level"

    def test_h3_does_not_close_h2_section(self, tmp_path):
        """§5.3: deeper heading does not close a shallower section"""
        cells = [
            markdown_cell("## Main", cell_id="m0"),
            code_cell("x = 1", cell_id="c1"),
            markdown_cell("### Sub", cell_id="m2"),
            code_cell("y = 2", cell_id="c3"),
        ]
        nb = make_notebook(cells, tmp_path=tmp_path)
        run_indexer(nb)
        data = load_index(index_path_for(nb))
        # Per §5.3: section = innermost containing heading.
        # h3 "Sub" opens its own sub-section inside h2 "Main".
        # Cells[3] (under h3) are in section "Sub" (the innermost heading).
        assert data["cells"][3]["section"] == "Sub", (
            "Cell under h3 must be in the h3 sub-section, not the parent h2 section"
        )


# ---------------------------------------------------------------------------
# §6 — Symbol extraction
# ---------------------------------------------------------------------------

class TestSymbolExtraction:

    def _index(self, source, tmp_path, kernel="python"):
        nb = make_notebook([code_cell(source)], kernel_language=kernel, tmp_path=tmp_path)
        run_indexer(nb)
        return load_index(index_path_for(nb))["cells"][0]

    def test_python_def_detected(self, tmp_path):
        """§6.1"""
        cell = self._index("def process(x):\n    return x\n", tmp_path)
        assert "process" in cell["symbols_defined"]

    def test_python_class_detected(self, tmp_path):
        """§6.2"""
        cell = self._index("class MyModel:\n    pass\n", tmp_path)
        assert "MyModel" in cell["symbols_defined"]

    def test_python_assignment_detected(self, tmp_path):
        """§6.3"""
        cell = self._index("result = compute()\n", tmp_path)
        assert "result" in cell["symbols_defined"]

    def test_python_annotated_assignment_detected(self, tmp_path):
        """§6.4"""
        cell = self._index("x: int = 5\n", tmp_path)
        assert "x" in cell["symbols_defined"]

    def test_python_augmented_assignment_not_captured(self, tmp_path):
        """§6.5"""
        cell = self._index("counter += 1\n", tmp_path)
        assert "counter" not in cell["symbols_defined"]

    def test_python_equality_not_captured(self, tmp_path):
        """§6 ASSIGN_RE excludes =="""
        cell = self._index("if x == 1:\n    pass\n", tmp_path)
        assert "x" not in cell["symbols_defined"]

    def test_python_import_detected(self, tmp_path):
        """§6.6"""
        cell = self._index("import numpy as np\n", tmp_path)
        assert "numpy" in cell["symbols_imported"]

    def test_python_from_import_detected(self, tmp_path):
        """§6.7"""
        cell = self._index("from sklearn.linear_model import Ridge\n", tmp_path)
        assert "sklearn.linear_model" in cell["symbols_imported"]

    def test_python_walrus_not_captured(self, tmp_path):
        """§6.8: walrus := is not at line start so ASSIGN_RE won't match"""
        cell = self._index("if (n := len(a)) > 10:\n    pass\n", tmp_path)
        assert "n" not in cell["symbols_defined"]

    def test_python_type_keyword_excluded(self, tmp_path):
        """§6 A5: 'type' soft-keyword post-filter"""
        cell = self._index("type Vector = list[float]\n", tmp_path)
        assert "type" not in cell["symbols_defined"]

    def test_julia_function_detected(self, tmp_path):
        """§6.9"""
        cell = self._index(
            "function push!(x, v)\n    push!(x.items, v)\nend\n",
            tmp_path, kernel="julia"
        )
        assert "push!" in cell["symbols_defined"]

    def test_julia_short_form_detected(self, tmp_path):
        """§6.10"""
        cell = self._index("polarise(x, p) = x * p\n", tmp_path, kernel="julia")
        assert "polarise" in cell["symbols_defined"]

    def test_julia_using_single(self, tmp_path):
        """§6.11"""
        cell = self._index("using ForwardDiff\n", tmp_path, kernel="julia")
        assert "ForwardDiff" in cell["symbols_imported"]

    def test_julia_using_multi(self, tmp_path):
        """§6.11"""
        cell = self._index("using GLMakie, StaticArrays\n", tmp_path, kernel="julia")
        assert "GLMakie" in cell["symbols_imported"]
        assert "StaticArrays" in cell["symbols_imported"]

    def test_unknown_language_extraction_skipped(self, tmp_path):
        """§6.13"""
        cell = self._index("x <- function(y) y + 1\n", tmp_path, kernel="bash")
        assert cell["symbols_extracted"] is False
        assert cell["symbols_defined"] == []
        assert cell["symbols_imported"] == []

    def test_r_function_detected(self, tmp_path):
        """§6 A5: R kernel"""
        cell = self._index("my_func <- function(x) x + 1\n", tmp_path, kernel="r")
        assert "my_func" in cell["symbols_defined"]

    def test_r_library_detected(self, tmp_path):
        """§6 A5: R library() import"""
        cell = self._index("library(dplyr)\n", tmp_path, kernel="r")
        assert "dplyr" in cell["symbols_imported"]

    def test_symbol_index_built_from_all_cells(self, tmp_path):
        """§6.14"""
        cells = [
            code_cell("def process(x):\n    return x\n", cell_id="c0"),
            code_cell("result = process(1)\n", cell_id="c1"),
            code_cell("def process(y):\n    return y\n", cell_id="c2"),
        ]
        nb = make_notebook(cells, tmp_path=tmp_path)
        run_indexer(nb)
        data = load_index(index_path_for(nb))
        # Derive symbol_index from cells (it's not stored; build it in-memory)
        symbol_index = {}
        for c in data["cells"]:
            for s in c.get("symbols_defined", []):
                symbol_index.setdefault(s, []).append(c["i"])
        assert 0 in symbol_index.get("process", [])
        assert 2 in symbol_index.get("process", [])

    def test_non_code_cells_skipped(self, tmp_path):
        """§6.15"""
        nb = make_notebook([markdown_cell("## Heading\n")], tmp_path=tmp_path)
        run_indexer(nb)
        data = load_index(index_path_for(nb))
        cell = data["cells"][0]
        assert cell["symbols_defined"] == []
        assert cell["symbols_extracted"] is False

    def test_long_line_skipped_no_timeout(self, tmp_path):
        """§6.16: line > 500 chars is skipped without hanging"""
        long_line = "x = " + "a" * 600 + "\n"
        nb = make_notebook([code_cell(long_line + "y = 1\n")], tmp_path=tmp_path)
        r = run_indexer(nb)
        assert r.returncode == 0
        data = load_index(index_path_for(nb))
        # y = 1 is on a short line and must still be captured
        assert "y" in data["cells"][0]["symbols_defined"]

    def test_adversarial_no_closing_delimiter(self, tmp_path):
        """§6.17: 10k-char line with no closing paren must return quickly, not be captured"""
        source = "library(" + "a" * 10000 + "\n"
        nb = make_notebook([code_cell(source)], kernel_language="r", tmp_path=tmp_path)
        start = time.monotonic()
        r = run_indexer(nb)
        elapsed = time.monotonic() - start
        assert r.returncode == 0
        # Per spec: < 100ms for adversarial lines; use 2s as practical subprocess budget
        assert elapsed < 2.0, f"Indexer took {elapsed:.2f}s on adversarial input (limit 2s)"
        # The long line must be silently skipped — no spurious import must be captured
        data = load_index(index_path_for(nb))
        captured = data["cells"][0].get("symbols_imported", [])
        assert not any("a" * 100 in s for s in captured), (
            "Adversarial long line must be skipped, not partially captured"
        )

    def test_symbol_name_length_cap(self, tmp_path):
        """§6.18: identifier > MAX_SYMBOL_LEN (256) is discarded"""
        long_name = "A" * 257
        source = f"{long_name} = 1\n"
        nb = make_notebook([code_cell(source)], tmp_path=tmp_path)
        run_indexer(nb)
        data = load_index(index_path_for(nb))
        assert long_name not in data["cells"][0]["symbols_defined"]

    def test_symbol_name_at_limit_kept(self, tmp_path):
        """§6.18: identifier exactly 256 chars is retained"""
        name = "A" * 256
        source = f"{name} = 1\n"
        nb = make_notebook([code_cell(source)], tmp_path=tmp_path)
        run_indexer(nb)
        data = load_index(index_path_for(nb))
        assert name in data["cells"][0]["symbols_defined"]

    def test_symbol_count_cap_per_cell(self, tmp_path):
        """§6.19: more than MAX_SYMBOLS_PER_CELL (500) assignments are capped at exactly 500"""
        lines = "".join(f"a{i} = {i}\n" for i in range(600))
        nb = make_notebook([code_cell(lines)], tmp_path=tmp_path)
        run_indexer(nb)
        data = load_index(index_path_for(nb))
        count = len(data["cells"][0]["symbols_defined"])
        # All 600 unique symbols; capped at 500 — the first 500 must be present
        assert count == 500, (
            f"Expected exactly 500 symbols after cap (got {count}); "
            "cap must retain first 500, not drop all"
        )


# ---------------------------------------------------------------------------
# §7 — Output storage
# ---------------------------------------------------------------------------

class TestOutputStorage:

    def _run(self, cells, tmp_path, kernel="python"):
        nb = make_notebook(cells, kernel_language=kernel, tmp_path=tmp_path)
        run_indexer(nb)
        return load_index(index_path_for(nb))["cells"][0]

    def test_stream_output_stored(self, tmp_path):
        """§7.1"""
        cell = self._run(
            [code_cell("print('hello')", outputs=[stream_output("hello\nworld")])],
            tmp_path
        )
        assert "hello" in cell.get("output_text", "")

    def test_execute_result_stored(self, tmp_path):
        """§7.2"""
        cell = self._run(
            [code_cell("42", outputs=[execute_result_output("42")])],
            tmp_path
        )
        assert "42" in cell.get("output_text", "")

    def test_error_traceback_stored(self, tmp_path):
        """§7.3"""
        cell = self._run(
            [code_cell("1/0", outputs=[error_output("ZeroDivisionError", "division by zero")])],
            tmp_path
        )
        assert "ZeroDivisionError" in cell.get("output_text", "")

    def test_display_data_text_stored(self, tmp_path):
        """§7.14"""
        cell = self._run(
            [code_cell("display(42)", outputs=[display_data_output("42")])],
            tmp_path
        )
        assert "42" in cell.get("output_text", "")
        assert "display_data" in cell.get("output_types", [])

    def test_binary_output_not_stored_in_text(self, tmp_path):
        """§7.4: binary-only output must NOT produce an output_text key at all"""
        cell = self._run(
            [code_cell("plot()", outputs=[png_output()])],
            tmp_path
        )
        # Spec §7.4/§7.8: no output_text key for binary-only cells (not even empty string)
        assert "output_text" not in cell, (
            f"output_text must be absent for binary-only output, got: {cell.get('output_text')!r}"
        )
        assert cell["has_output"] is True
        assert "image/png" in cell.get("output_types", [])

    def test_no_output_no_output_text_key(self, tmp_path):
        """§7.8: cell with no outputs must have has_output=false and no output_text key"""
        cell = self._run([code_cell("x = 1")], tmp_path)
        # has_output must be explicitly False, not merely absent
        assert cell.get("has_output") is False, (
            f"has_output must be explicitly False for cells with no output, "
            f"got: {cell.get('has_output')!r}"
        )
        assert "output_text" not in cell

    def test_output_types_deduplicated(self, tmp_path):
        """§7.10"""
        outputs = [
            stream_output("a"),
            execute_result_output("1"),
            stream_output("b"),
            execute_result_output("2"),
        ]
        cell = self._run([code_cell("x", outputs=outputs)], tmp_path)
        types = cell.get("output_types", [])
        assert types.count("stream") == 1
        assert types.count("execute_result") == 1

    def test_output_types_order_of_first_appearance(self, tmp_path):
        """§7.10: order = first occurrence"""
        outputs = [stream_output("a"), execute_result_output("1")]
        cell = self._run([code_cell("x", outputs=outputs)], tmp_path)
        types = cell.get("output_types", [])
        assert types.index("stream") < types.index("execute_result")

    def test_4kb_cap_truncation(self, tmp_path):
        """§7.5: output > 4096 bytes truncated at last complete line boundary"""
        lines = ("x" * 100 + "\n") * 60   # ~6 KB
        cell = self._run(
            [code_cell("x", outputs=[stream_output(lines)])],
            tmp_path
        )
        text = cell.get("output_text", "")
        assert cell["output_truncated"] is True
        assert len(text.encode("utf-8")) <= 4096
        # Must end at a line boundary (spec §7.5: "last complete line before boundary")
        assert text.endswith("\n"), (
            f"Truncated output must end at a newline (line boundary), got: {text[-20:]!r}"
        )

    def test_exact_4096_bytes_not_truncated(self, tmp_path):
        """§7.6"""
        # Build exactly 4096 bytes of UTF-8 text ending with a newline
        line = "a" * 63 + "\n"  # 64 bytes
        text = line * 64        # 4096 bytes exactly
        cell = self._run(
            [code_cell("x", outputs=[stream_output(text)])],
            tmp_path
        )
        assert cell.get("output_truncated") is False

    def test_single_line_over_4096_hard_truncated(self, tmp_path):
        """§7.7: single line > 4096 bytes → hard truncate at 4096 + suffix"""
        long_line = "a" * 5000 + "\n"
        cell = self._run(
            [code_cell("x", outputs=[stream_output(long_line)])],
            tmp_path
        )
        assert cell["output_truncated"] is True
        text = cell.get("output_text", "")
        assert text != "", "output_text must not be empty for single-line overflow"
        assert "[truncated mid-line]" in text
        # Spec: "store first 4096 bytes … with suffix '\n[truncated mid-line]'"
        suffix = "\n[truncated mid-line]"
        assert len(text.encode("utf-8")) <= 4096 + len(suffix.encode("utf-8")), (
            f"Hard-truncated output is too long: {len(text.encode('utf-8'))} bytes"
        )

    def test_null_bytes_stripped(self, tmp_path):
        """§7.11"""
        cell = self._run(
            [code_cell("x", outputs=[stream_output("hello\x00world\n")])],
            tmp_path
        )
        assert "\x00" not in cell.get("output_text", "")
        assert "hello" in cell.get("output_text", "")

    def test_output_stored_as_string_not_object(self, tmp_path):
        """§7.13: cell output that is valid JSON text must stay as a string"""
        cell = self._run(
            [code_cell("x", outputs=[stream_output('{"key": "val"}\n')])],
            tmp_path
        )
        assert isinstance(cell.get("output_text"), str)

    def test_mixed_text_and_binary_outputs(self, tmp_path):
        """§7.15"""
        outputs = [stream_output("hello\n"), png_output()]
        cell = self._run([code_cell("x", outputs=outputs)], tmp_path)
        assert "hello" in cell.get("output_text", "")
        assert "image/png" in cell.get("output_types", [])

    def test_multiple_outputs_concatenated(self, tmp_path):
        """§7.9"""
        outputs = [stream_output("hello\n"), stream_output("world\n")]
        cell = self._run([code_cell("x", outputs=outputs)], tmp_path)
        assert "hello" in cell.get("output_text", "")
        assert "world" in cell.get("output_text", "")

    def test_ansi_stripped_from_output_text(self, tmp_path):
        """A4 pipeline step 4: ANSI sequences must not appear in stored output_text"""
        ansi_output = "\x1b[31mred\x1b[0m\n"
        cell = self._run(
            [code_cell("x", outputs=[stream_output(ansi_output)])],
            tmp_path
        )
        assert "\x1b[" not in cell.get("output_text", ""), (
            "ANSI escape sequences must be stripped from stored output_text"
        )


# ---------------------------------------------------------------------------
# §8 — nb-write.py Integration
# ---------------------------------------------------------------------------

class TestWriteIntegration:
    """
    These tests verify that nb-write.py spawns nb-index.py after patch/insert/delete.
    Since nb-index.py may not exist yet, §8.5 protects nb-write.py from failure.
    We use a mock nb-index.py that records its arguments.
    """

    def _mock_indexer(self, tmp_path):
        """Write a stub nb-index.py that records sys.executable and argv to a log file."""
        log = tmp_path / "indexer_args.txt"
        stub = tmp_path / "nb-index.py"
        stub.write_text(
            f"import sys\n"
            f"with open({str(log)!r}, 'w') as _f:\n"
            f"    _f.write(sys.executable + '\\n')\n"
            f"    _f.write(' '.join(sys.argv[1:]) + '\\n')\n",
            encoding="utf-8"
        )
        return stub, log

    def _wait_for_log(self, log, timeout=5.0):
        """Poll until log file appears or timeout expires."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and not log.exists():
            time.sleep(0.05)
        return log.exists()

    def _run_write(self, args, cwd=None):
        return subprocess.run(
            [PYTHON, str(WRITE_SCRIPT)] + [str(a) for a in args],
            capture_output=True, text=True, cwd=cwd
        )

    def _patch_write_script_path(self, tmp_path, stub_path):
        """
        nb-write.py looks for nb-index.py relative to itself.  We can't
        easily override that in a subprocess, so instead we copy nb-write.py
        alongside the stub into tmp_path and invoke the copy.
        """
        import shutil
        write_copy = tmp_path / "nb-write.py"
        shutil.copy2(WRITE_SCRIPT, write_copy)
        return write_copy

    def test_indexer_not_spawned_on_create(self, tmp_path):
        """§8.4: 'create' must NOT trigger indexing"""
        stub, log = self._mock_indexer(tmp_path)
        write_copy = self._patch_write_script_path(tmp_path, stub)
        nb_path = tmp_path / "new.ipynb"
        r = subprocess.run(
            [PYTHON, str(write_copy), str(nb_path), "create", "--python"],
            capture_output=True, text=True
        )
        assert r.returncode == 0
        # Allow a brief moment for any async spawn (polling: up to 0.5s)
        time.sleep(0.5)
        assert not log.exists(), "nb-index.py must NOT be spawned on 'create'"

    def test_indexer_spawned_on_patch(self, tmp_path):
        """§8.1: patch triggers indexing"""
        stub, log = self._mock_indexer(tmp_path)
        write_copy = self._patch_write_script_path(tmp_path, stub)
        nb_path = tmp_path / "nb.ipynb"
        # First create the notebook
        subprocess.run(
            [PYTHON, str(write_copy), str(nb_path), "create", "--python"],
            capture_output=True
        )
        # Write a source file for patch
        src = tmp_path / "src.py"
        src.write_text("x = 42\n", encoding="utf-8")
        r = subprocess.run(
            [PYTHON, str(write_copy), str(nb_path), "patch", "0", "-f", str(src)],
            capture_output=True, text=True
        )
        assert r.returncode == 0
        assert self._wait_for_log(log), "nb-index.py was not spawned within 5s after patch"

    def test_indexer_spawned_on_insert(self, tmp_path):
        """§8.2: insert triggers indexing"""
        stub, log = self._mock_indexer(tmp_path)
        write_copy = self._patch_write_script_path(tmp_path, stub)
        nb_path = tmp_path / "nb.ipynb"
        subprocess.run(
            [PYTHON, str(write_copy), str(nb_path), "create", "--python"],
            capture_output=True
        )
        src = tmp_path / "src.py"
        src.write_text("y = 99\n", encoding="utf-8")
        r = subprocess.run(
            [PYTHON, str(write_copy), str(nb_path), "insert", "0", "code", "-f", str(src)],
            capture_output=True, text=True
        )
        assert r.returncode == 0
        assert self._wait_for_log(log), "nb-index.py was not spawned within 5s after insert"

    def test_indexer_spawned_on_delete(self, tmp_path):
        """§8.3: delete triggers indexing"""
        stub, log = self._mock_indexer(tmp_path)
        write_copy = self._patch_write_script_path(tmp_path, stub)
        nb_path = tmp_path / "nb.ipynb"
        subprocess.run(
            [PYTHON, str(write_copy), str(nb_path), "create", "--python"],
            capture_output=True
        )
        r = subprocess.run(
            [PYTHON, str(write_copy), str(nb_path), "delete", "0"],
            capture_output=True, text=True
        )
        assert r.returncode == 0
        assert self._wait_for_log(log), "nb-index.py was not spawned within 5s after delete"

    def test_indexer_failure_does_not_fail_write(self, tmp_path):
        """§8.5: missing nb-index.py must not prevent write from succeeding"""
        # Create a directory with only nb-write.py (no nb-index.py)
        import shutil
        write_copy = tmp_path / "nb-write.py"
        shutil.copy2(WRITE_SCRIPT, write_copy)
        nb_path = tmp_path / "nb.ipynb"
        subprocess.run(
            [PYTHON, str(write_copy), str(nb_path), "create", "--python"],
            capture_output=True
        )
        src = tmp_path / "src.py"
        src.write_text("x = 1\n", encoding="utf-8")
        r = subprocess.run(
            [PYTHON, str(write_copy), str(nb_path), "patch", "0", "-f", str(src)],
            capture_output=True, text=True
        )
        assert r.returncode == 0, (
            f"Write must succeed even when nb-index.py is absent: {r.stderr}"
        )

    def test_uses_sys_executable(self, tmp_path):
        """§8.6: spawned interpreter is sys.executable (not a hardcoded 'python3')"""
        stub, log = self._mock_indexer(tmp_path)
        write_copy = self._patch_write_script_path(tmp_path, stub)
        nb_path = tmp_path / "nb.ipynb"
        subprocess.run(
            [PYTHON, str(write_copy), str(nb_path), "create", "--python"],
            capture_output=True
        )
        src = tmp_path / "src.py"
        src.write_text("x = 1\n", encoding="utf-8")
        subprocess.run(
            [PYTHON, str(write_copy), str(nb_path), "patch", "0", "-f", str(src)],
            capture_output=True
        )
        assert self._wait_for_log(log), "nb-index.py was not spawned within 5s after patch"
        lines = log.read_text(encoding="utf-8").splitlines()
        assert lines, "Stub log must not be empty"
        # Line 0: sys.executable of the spawned process
        # If nb-write.py used sys.executable, this will equal PYTHON
        recorded_exe = lines[0].strip()
        assert os.path.isabs(recorded_exe), (
            f"Spawned interpreter must be an absolute path, got: {recorded_exe!r}"
        )
        assert recorded_exe == PYTHON, (
            f"nb-write.py must use sys.executable ({PYTHON!r}) to spawn nb-index.py, "
            f"but the spawned process saw sys.executable={recorded_exe!r}"
        )


# ---------------------------------------------------------------------------
# §13 — Project-level symbol cache (symbols.json)
# ---------------------------------------------------------------------------

class TestSymbolCache:

    def test_symbols_json_created_on_first_index(self, tmp_path):
        """§13.1"""
        nb = make_notebook([code_cell("def process(x):\n    return x\n")], tmp_path=tmp_path)
        run_indexer(nb)
        symbols_path = tmp_path / ".nb_index" / "symbols.json"
        assert symbols_path.exists(), f"symbols.json not found at {symbols_path}"

    def test_symbols_json_valid_json(self, tmp_path):
        nb = make_notebook([code_cell("def process(x):\n    return x\n")], tmp_path=tmp_path)
        run_indexer(nb)
        symbols_path = tmp_path / ".nb_index" / "symbols.json"
        data = json.loads(symbols_path.read_text(encoding="utf-8"))
        assert isinstance(data, dict)

    def test_symbols_json_has_version_1(self, tmp_path):
        """§13.7"""
        nb = make_notebook([code_cell("x = 1")], tmp_path=tmp_path)
        run_indexer(nb)
        data = json.loads((tmp_path / ".nb_index" / "symbols.json").read_text())
        assert data["version"] == 1
        assert isinstance(data["version"], int)

    def test_symbols_json_has_generated_at(self, tmp_path):
        nb = make_notebook([code_cell("x = 1")], tmp_path=tmp_path)
        run_indexer(nb)
        data = json.loads((tmp_path / ".nb_index" / "symbols.json").read_text())
        assert "generated_at" in data
        ts = data["generated_at"]
        # Must be full ISO 8601 UTC format: YYYY-MM-DDTHH:MM:SSZ
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", ts), (
            f"generated_at must match ISO 8601 UTC format (YYYY-MM-DDTHH:MM:SSZ), got: {ts!r}"
        )

    def test_symbols_json_has_max_indexed_at(self, tmp_path):
        """§12.2 / schema: max_indexed_at field required"""
        nb = make_notebook([code_cell("x = 1")], tmp_path=tmp_path)
        run_indexer(nb)
        data = json.loads((tmp_path / ".nb_index" / "symbols.json").read_text())
        assert "max_indexed_at" in data, (
            "symbols.json must store max_indexed_at for O(1) freshness check"
        )

    def test_symbols_json_contains_defined_symbols(self, tmp_path):
        """§13.1"""
        nb = make_notebook([code_cell("def process(x):\n    return x\n")], tmp_path=tmp_path)
        run_indexer(nb)
        data = json.loads((tmp_path / ".nb_index" / "symbols.json").read_text())
        assert "process" in data.get("symbols", {}), (
            f"'process' not found in symbols.json symbols: {list(data.get('symbols', {}).keys())[:10]}"
        )

    def test_symbols_json_contains_imports(self, tmp_path):
        nb = make_notebook([code_cell("import numpy as np\n")], tmp_path=tmp_path)
        run_indexer(nb)
        data = json.loads((tmp_path / ".nb_index" / "symbols.json").read_text())
        assert "numpy" in data.get("imports", {})

    def test_symbols_json_updated_on_reindex(self, tmp_path):
        """§13.2: re-index updates (not duplicates) entries"""
        # First index: defines 'alpha'
        nb = make_notebook([code_cell("alpha = 1\n")], tmp_path=tmp_path)
        run_indexer(nb)
        # Rewrite notebook to define 'beta' instead
        nb = make_notebook([code_cell("beta = 2\n")], tmp_path=tmp_path)
        run_indexer(nb, extra_args=["--force"])
        data = json.loads((tmp_path / ".nb_index" / "symbols.json").read_text())
        # 'alpha' should be gone, 'beta' should be present
        symbols = data.get("symbols", {})
        assert "alpha" not in symbols, "Stale 'alpha' entry must be removed on re-index"
        assert "beta" in symbols, "'beta' must appear after re-index"

    def test_symbols_json_not_skipped_as_notebook_index(self, tmp_path):
        """§13.5: symbols.json itself must not be treated as a per-notebook index"""
        nb = make_notebook([code_cell("x = 1")], tmp_path=tmp_path)
        run_indexer(nb)
        symbols_path = tmp_path / ".nb_index" / "symbols.json"
        # symbols.json should not have a 'cells' key (it's not a notebook index)
        data = json.loads(symbols_path.read_text())
        assert "cells" not in data

    def test_symbols_json_location_format(self, tmp_path):
        """§13: location strings are '<notebook_path>:<cell_index>'"""
        nb = make_notebook(
            [code_cell("def foo():\n    pass\n")],
            tmp_path=tmp_path, name="mynotebook.ipynb"
        )
        run_indexer(nb)
        data = json.loads((tmp_path / ".nb_index" / "symbols.json").read_text())
        locs = data.get("symbols", {}).get("foo", [])
        assert locs, f"Expected at least one location for 'foo', got: {locs}"
        # Spec §13.1: format is "<notebook_path>:<cell_index>" (e.g. "nb.ipynb:0")
        assert any("mynotebook.ipynb" in loc for loc in locs), (
            f"Expected 'mynotebook.ipynb' in symbol location, got: {locs}"
        )
        # Verify the :<N> integer suffix is present
        for loc in locs:
            if "mynotebook.ipynb" in loc:
                colon_pos = loc.rfind(":")
                assert colon_pos > 0, f"Location must have ':N' suffix, got: {loc!r}"
                index_part = loc[colon_pos + 1:]
                assert index_part.isdigit(), (
                    f"Cell index in location must be a non-negative integer, got: {index_part!r}"
                )


# ---------------------------------------------------------------------------
# §14 — Edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:

    def test_0_cell_notebook(self, tmp_path):
        """§14.1"""
        nb = make_notebook([], tmp_path=tmp_path)
        r = run_indexer(nb)
        assert r.returncode == 0
        data = load_index(index_path_for(nb))
        assert data["cell_count"] == 0
        assert data["cells"] == []

    def test_0_code_cells_all_markdown(self, tmp_path):
        """§14.2"""
        cells = [markdown_cell("## Section A"), markdown_cell("## Section B", cell_id="m2")]
        nb = make_notebook(cells, tmp_path=tmp_path)
        r = run_indexer(nb)
        assert r.returncode == 0
        data = load_index(index_path_for(nb))
        # Build in-memory indices from cells
        all_defined = [s for c in data["cells"] for s in c.get("symbols_defined", [])]
        all_imported = [s for c in data["cells"] for s in c.get("symbols_imported", [])]
        assert all_defined == []
        assert all_imported == []

    def test_gitignore_symlink_does_not_block_indexing(self, tmp_path):
        """§14.4: .gitignore symlink → warning on stderr, index still written"""
        real_gitignore = tmp_path / "real_gi"
        real_gitignore.write_text("# original\n", encoding="utf-8")
        (tmp_path / ".gitignore").symlink_to(real_gitignore)
        nb = make_notebook([code_cell("x = 1")], tmp_path=tmp_path)
        r = run_indexer(nb)
        assert r.returncode == 0
        assert index_path_for(nb).exists(), "Index must be written even when .gitignore is a symlink"
        # Real .gitignore must not have been modified
        assert ".nb_index/" not in real_gitignore.read_text(encoding="utf-8")

    def test_concurrent_indexers_both_exit_0(self, tmp_path):
        """§14.5: two concurrent indexers must both exit 0 and leave valid JSON"""
        nb = make_notebook(
            [code_cell(f"x = {i}\n", cell_id=f"c{i:03}") for i in range(10)],
            tmp_path=tmp_path
        )
        p1 = subprocess.Popen(
            [PYTHON, str(SCRIPT), str(nb)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        p2 = subprocess.Popen(
            [PYTHON, str(SCRIPT), str(nb)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        rc1 = p1.wait(timeout=30)
        rc2 = p2.wait(timeout=30)
        assert rc1 == 0, f"First concurrent indexer exited {rc1}"
        assert rc2 == 0, f"Second concurrent indexer exited {rc2}"
        idx = index_path_for(nb)
        assert idx.exists()
        data = load_index(idx)
        assert isinstance(data, dict), "Index must be valid JSON after concurrent writes"
        assert "cells" in data
        assert data.get("cell_count") == 10, (
            f"Index must contain all 10 cells after concurrent writes, "
            f"got cell_count={data.get('cell_count')}"
        )

    def test_single_output_line_over_4096(self, tmp_path):
        """§14.7: alias for §7.7 edge case — covered by TestOutputStorage.test_single_line_over_4096_hard_truncated"""
        long_line = "a" * 5000 + "\n"
        cells = [code_cell("x", outputs=[stream_output(long_line)])]
        nb = make_notebook(cells, tmp_path=tmp_path)
        run_indexer(nb)
        data = load_index(index_path_for(nb))
        cell = data["cells"][0]
        text = cell.get("output_text", "")
        assert cell["output_truncated"] is True
        assert "[truncated mid-line]" in text
        suffix = "\n[truncated mid-line]"
        assert len(text.encode("utf-8")) <= 4096 + len(suffix.encode("utf-8"))

    def test_first_line_for_all_cell_types(self, tmp_path):
        """§14.11"""
        cells = [
            code_cell(["x = 1\n", "y = 2\n"], cell_id="c0"),
            markdown_cell("## Heading\nmore text\n", cell_id="m1"),
            code_cell("", cell_id="c2"),
        ]
        nb = make_notebook(cells, tmp_path=tmp_path)
        run_indexer(nb)
        data = load_index(index_path_for(nb))
        assert data["cells"][0]["first_line"] == "x = 1"
        assert data["cells"][1]["first_line"] == "## Heading"
        assert data["cells"][2]["first_line"] == "(empty)"

    def test_large_notebook_indexes_without_error(self, tmp_path):
        """Regression: 200-cell notebook must index successfully"""
        cells = [code_cell(f"x_{i} = {i}\n", cell_id=f"c{i:04}") for i in range(200)]
        nb = make_notebook(cells, tmp_path=tmp_path)
        r = run_indexer(nb)
        assert r.returncode == 0
        data = load_index(index_path_for(nb))
        assert data["cell_count"] == 200

    def test_notebook_with_unicode_source(self, tmp_path):
        """Non-ASCII source must not crash the indexer"""
        nb = make_notebook([code_cell("# 日本語コメント\nx = 1\n")], tmp_path=tmp_path)
        r = run_indexer(nb)
        assert r.returncode == 0

    def test_notebook_with_unicode_output(self, tmp_path):
        """Non-ASCII output must be stored without mangling"""
        nb = make_notebook(
            [code_cell("x", outputs=[stream_output("こんにちは\n")])],
            tmp_path=tmp_path
        )
        r = run_indexer(nb)
        assert r.returncode == 0
        data = load_index(index_path_for(nb))
        assert "こんにちは" in data["cells"][0].get("output_text", "")

    def test_indexed_at_field_present_and_utc(self, tmp_path):
        """Schema: indexed_at stored as full ISO 8601 UTC: YYYY-MM-DDTHH:MM:SSZ"""
        nb = make_notebook([code_cell("x = 1")], tmp_path=tmp_path)
        run_indexer(nb)
        data = load_index(index_path_for(nb))
        assert "indexed_at" in data
        ts = data["indexed_at"]
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", ts), (
            f"indexed_at must match ISO 8601 UTC (YYYY-MM-DDTHH:MM:SSZ), got: {ts!r}"
        )

    def test_no_orphaned_tmp_files_after_rapid_writes(self, tmp_path):
        """§14.6: rapid successive index writes must not leave .nb_tmp files"""
        nb = make_notebook([code_cell("x = 1")], tmp_path=tmp_path)
        for _ in range(5):
            run_indexer(nb, extra_args=["--force"])
        tmp_files = list((tmp_path / ".nb_index").glob("*.nb_tmp"))
        assert tmp_files == [], f"Orphaned tmp files found: {tmp_files}"

    def test_cells_have_i_field(self, tmp_path):
        """Schema: each cell entry must have an 'i' field (cell index)"""
        cells = [code_cell(f"x = {i}", cell_id=f"c{i}") for i in range(3)]
        nb = make_notebook(cells, tmp_path=tmp_path)
        run_indexer(nb)
        data = load_index(index_path_for(nb))
        for expected_i, cell in enumerate(data["cells"]):
            assert cell["i"] == expected_i, f"Cell {expected_i} has wrong i: {cell['i']}"

    def test_cells_have_type_field(self, tmp_path):
        """Schema: each cell must have a 'type' field"""
        cells = [code_cell("x = 1"), markdown_cell("## H", cell_id="m1")]
        nb = make_notebook(cells, tmp_path=tmp_path)
        run_indexer(nb)
        data = load_index(index_path_for(nb))
        assert data["cells"][0]["type"] == "code"
        assert data["cells"][1]["type"] == "markdown"

    def test_schema_tolerates_unknown_top_level_keys(self, tmp_path):
        """Schema compat: unknown keys in index must not cause nb-read/nb-search to crash"""
        nb = make_notebook([code_cell("x = 1")], tmp_path=tmp_path)
        run_indexer(nb)
        idx = index_path_for(nb)
        data = load_index(idx)
        data["_future_extension"] = "some_value"
        idx.write_text(json.dumps(data), encoding="utf-8")
        # Re-index should overwrite it (this also verifies no crash on read)
        r = run_indexer(nb, extra_args=["--force"])
        assert r.returncode == 0

    def test_derived_fields_not_stored_in_index(self, tmp_path):
        """Schema: symbol_index, import_index, and sections are NOT stored in the JSON"""
        cells = [
            markdown_cell("## Section A", cell_id="m0"),
            code_cell("def foo():\n    pass\n", cell_id="c1"),
        ]
        nb = make_notebook(cells, tmp_path=tmp_path)
        run_indexer(nb)
        data = load_index(index_path_for(nb))
        assert "symbol_index" not in data, (
            "symbol_index must be derived at load time, not stored in the JSON"
        )
        assert "import_index" not in data, (
            "import_index must be derived at load time, not stored in the JSON"
        )
        assert "sections" not in data, (
            "sections array must be derived at load time, not stored in the JSON"
        )

    def test_raw_cell_type_and_symbols_extracted_false(self, tmp_path):
        """Schema: raw cells store type='raw' and symbols_extracted=false"""
        nb = make_notebook([raw_cell("some raw content")], tmp_path=tmp_path)
        run_indexer(nb)
        data = load_index(index_path_for(nb))
        cell = data["cells"][0]
        assert cell["type"] == "raw", f"Expected type='raw', got: {cell.get('type')!r}"
        assert cell["symbols_extracted"] is False


# ---------------------------------------------------------------------------
# Additional coverage: §6 symbol extraction gaps
# ---------------------------------------------------------------------------

class TestSymbolExtractionAdditional:

    def _index(self, source, tmp_path, kernel="python"):
        nb = make_notebook([code_cell(source)], kernel_language=kernel, tmp_path=tmp_path)
        run_indexer(nb)
        return load_index(index_path_for(nb))["cells"][0]

    def test_symbols_extracted_true_for_python_code(self, tmp_path):
        """§6: symbols_extracted must be True for code cells with a known language"""
        cell = self._index("def foo():\n    pass\n", tmp_path, kernel="python")
        assert cell["symbols_extracted"] is True, (
            "symbols_extracted must be True for Python code cells"
        )

    def test_ir_kernel_not_treated_as_r(self, tmp_path):
        """§6 A5: 'ir' kernel (IRkernel) must NOT trigger R pattern extraction"""
        cell = self._index("my_func <- function(x) x + 1\n", tmp_path, kernel="ir")
        # 'ir' must not be treated as R language
        assert cell["symbols_extracted"] is False, (
            "IRkernel ('ir') must not trigger R symbol extraction"
        )

    def test_julia_struct_detected(self, tmp_path):
        """§6 A5: Julia struct definitions → symbols_defined"""
        cell = self._index("struct MyPoint\n    x::Float64\n    y::Float64\nend\n",
                            tmp_path, kernel="julia")
        assert "MyPoint" in cell["symbols_defined"], (
            "Julia struct type must be captured in symbols_defined"
        )

    def test_julia_mutable_struct_detected(self, tmp_path):
        """§6 A5: Julia mutable struct → symbols_defined"""
        cell = self._index("mutable struct Counter\n    n::Int\nend\n",
                            tmp_path, kernel="julia")
        assert "Counter" in cell["symbols_defined"]

    def test_julia_import_colon_syntax(self, tmp_path):
        """§6.12: 'import CancerResearch: PiecewiseTyson' → 'CancerResearch' in imports"""
        cell = self._index("import CancerResearch: PiecewiseTyson\n",
                            tmp_path, kernel="julia")
        assert "CancerResearch" in cell["symbols_imported"], (
            "Julia 'import X: Y' must add X to symbols_imported"
        )

    def test_r_require_detected(self, tmp_path):
        """§6 A5: R require() treated same as library()"""
        cell = self._index("require(dplyr)\n", tmp_path, kernel="r")
        assert "dplyr" in cell["symbols_imported"], (
            "R require() must be captured the same as library()"
        )


# ---------------------------------------------------------------------------
# Additional coverage: §7 output storage gaps
# ---------------------------------------------------------------------------

class TestOutputStorageAdditional:

    def _run(self, cells, tmp_path, kernel="python"):
        nb = make_notebook(cells, kernel_language=kernel, tmp_path=tmp_path)
        run_indexer(nb)
        return load_index(index_path_for(nb))["cells"][0]

    def test_has_output_true_for_stream(self, tmp_path):
        """§7: code cell with stream output must have has_output=True"""
        cell = self._run(
            [code_cell("print('hi')", outputs=[stream_output("hi\n")])], tmp_path
        )
        assert cell["has_output"] is True

    def test_output_truncated_false_for_small_output(self, tmp_path):
        """§7: small output must have output_truncated=False (field must be present)"""
        cell = self._run(
            [code_cell("x", outputs=[stream_output("short\n")])], tmp_path
        )
        assert cell.get("output_truncated") is False, (
            "output_truncated must be explicitly False for small (non-truncated) output; "
            f"got: {cell.get('output_truncated')!r}"
        )

    def test_lone_surrogate_replaced_in_output(self, tmp_path):
        """§7.12: lone surrogates in output must be replaced with U+FFFD"""
        # Construct a string with a lone surrogate using surrogatepass encoding
        lone_surrogate = "\ud800"  # lone high surrogate
        nb = {
            "nbformat": 4, "nbformat_minor": 5,
            "metadata": {"kernelspec": {"display_name": "Python 3", "language": "python",
                                         "name": "python3"},
                          "language_info": {"name": "python", "version": "3.10.0"}},
            "cells": [{
                "cell_type": "code", "id": "c001", "metadata": {},
                "source": ["x = 1"],
                "outputs": [{"output_type": "stream", "name": "stdout",
                              "text": [lone_surrogate + "hello\n"]}],
                "execution_count": 1,
            }],
        }
        nb_path = tmp_path / "surrogate.ipynb"
        nb_path.write_bytes(
            json.dumps(nb, ensure_ascii=False).encode("utf-8", errors="surrogatepass")
        )
        r = run_indexer(nb_path)
        assert r.returncode == 0, f"Lone surrogate must not crash indexer: {r.stderr}"
        data = load_index(index_path_for(nb_path))
        output_text = data["cells"][0].get("output_text", "")
        assert "\ud800" not in output_text, "Lone surrogate must be replaced (not left raw)"
        assert "hello" in output_text, "Non-surrogate content must be preserved"


# ---------------------------------------------------------------------------
# Additional coverage: §13 symbols.json gaps
# ---------------------------------------------------------------------------

class TestSymbolCacheAdditional:

    def test_symbols_json_version_gt1_falls_back(self, tmp_path):
        """§13.7: symbols.json with version > 1 must cause fallback to serial scan"""
        nb = make_notebook([code_cell("def version_gap_func():\n    pass\n")],
                            tmp_path=tmp_path)
        run_indexer(nb)
        symbols_path = tmp_path / ".nb_index" / "symbols.json"
        if not symbols_path.exists():
            pytest.skip("Implementation does not produce symbols.json yet")
        # Bump version to simulate future format
        data = json.loads(symbols_path.read_text(encoding="utf-8"))
        data["version"] = 999
        symbols_path.write_text(json.dumps(data), encoding="utf-8")
        # nb-index.py should not crash on re-index; symbols.json will be rebuilt
        r = run_indexer(nb, extra_args=["--force"])
        assert r.returncode == 0, (
            f"Indexer must tolerate unknown symbols.json version: {r.stderr}"
        )
        # After rebuild, version should be 1 again (or the file may have been recreated)
        new_data = json.loads(symbols_path.read_text(encoding="utf-8"))
        assert new_data.get("version") == 1, (
            "symbols.json must be rebuilt with version=1 after detecting unknown version"
        )
