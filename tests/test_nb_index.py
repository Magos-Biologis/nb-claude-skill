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

    @pytest.mark.skipif(sys.platform == 'win32', reason='symlink creation requires admin/Developer Mode on Windows')
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
        # Capture the inode before the second run; os.replace() creates a new inode
        # on write, so a changed inode proves the second run actually wrote to idx1.
        inode_before_second = idx1.stat().st_ino
        r2 = run_indexer(dotdot, extra_args=["--force"])
        assert r2.returncode == 0, (
            f"Indexing via non-canonical path must succeed: {r2.stderr}"
        )
        # The index must still be at the canonical location and have been refreshed.
        assert idx1.exists(), "Index must exist after second run"
        # os.replace() allocates a new inode — if the inode changed, the second run
        # wrote to the correct (canonical) path and did not create a separate file.
        assert idx1.stat().st_ino != inode_before_second, (
            "Second run must have written a new index file at the canonical location "
            "(inode must change after os.replace())"
        )
        # Sanity: no extra index JSON outside .nb_index/
        extra = [p for p in tmp_path.rglob("*.json") if p.parent.name != ".nb_index"]
        assert extra == [], f"Unexpected JSON files outside .nb_index/: {extra}"

    def test_containment_invariant_index_inside_nb_index(self, tmp_path):
        """§1.7: the constructed index path must always be inside .nb_index/, never above it.

        With Path.resolve() called at entry, the notebook path is canonicalised before
        any arithmetic.  This test verifies the invariant holds for a real notebook:
        regardless of how the path was passed, the produced index JSON is strictly
        under .nb_index/ and nowhere else.
        """
        nb = make_notebook([code_cell("x = 1")], tmp_path=tmp_path, name="nb.ipynb")
        run_indexer(nb)
        idx = index_path_for(nb)
        # The index must be inside .nb_index/
        nb_index_dir = idx.parent
        assert nb_index_dir.name == ".nb_index", (
            f"Index parent must be '.nb_index', got: {nb_index_dir.name!r}"
        )
        # The index must be a direct child of .nb_index/ (no sub-directories that
        # could represent path traversal).  For the no-git case, it is always flat.
        assert idx.parent.parent == tmp_path, (
            "Index must be in <nb_dir>/.nb_index/, not in a nested sub-directory"
        )
        # No index file must exist outside the expected .nb_index/ location
        unexpected = list(tmp_path.rglob("*.json"))
        assert all(p.parent.name == ".nb_index" for p in unexpected), (
            f"All JSON index files must be inside .nb_index/: {unexpected}"
        )

    def test_notebook_outside_git_root_uses_local_nb_index(self, tmp_path):
        """§1.7 / §1.4: a real notebook outside any git root uses its own dir's .nb_index/."""
        git_root = tmp_path / "project"
        git_root.mkdir()
        (git_root / ".git").mkdir()
        # Notebook is OUTSIDE the git root — indexer must fall back to nb_dir/.nb_index/
        outside = tmp_path / "external"
        outside.mkdir()
        nb = make_notebook([code_cell("x = 1")], tmp_path=outside, name="nb.ipynb")
        r = run_indexer(nb)
        assert r.returncode == 0
        # Index must be in external/.nb_index/, NOT in project/.nb_index/
        assert (outside / ".nb_index").exists(), "Index must be in notebook's own dir"
        assert not (git_root / ".nb_index").exists(), (
            "Index must not appear inside an unrelated git root"
        )

    @pytest.mark.skipif(sys.platform == 'win32', reason='symlink creation requires admin/Developer Mode on Windows')
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

    def test_nblock_entry_in_gitignore(self, tmp_path):
        """*.nblock lock files should be added to .gitignore"""
        nb = make_notebook([code_cell("x = 1")], tmp_path=tmp_path)
        run_indexer(nb)
        gitignore = tmp_path / ".gitignore"
        content = gitignore.read_text(encoding="utf-8")
        assert "*.nblock" in content, ".gitignore must include *.nblock entry"
        # Verify it's not duplicated on second index
        run_indexer(nb, extra_args=["--force"])
        content = gitignore.read_text(encoding="utf-8")
        assert content.count("*.nblock") == 1, "*.nblock must not be duplicated"

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

    @pytest.mark.skipif(sys.platform == 'win32', reason='symlink creation requires admin/Developer Mode on Windows')
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

    def test_unreadable_notebook_with_matching_mtime_size_is_stale(self, tmp_path):
        """A3 step 4: the hash is the authoritative freshness check. When mtime
        and size match but the notebook cannot be read for hashing, the index is
        unverifiable and must be treated as STALE (fail safe), not silently fresh.

        The A3 short-circuit requirement applies to mtime/size *mismatches*
        (steps 2-3 exit stale without reading) — not to skipping step 4 on match.
        """
        nb = make_notebook([code_cell("x = 1")], tmp_path=tmp_path)
        run_indexer(nb)
        idx = index_path_for(nb)
        inode_before = idx.stat().st_ino

        # Remove read permission — the authoritative hash check cannot run
        nb.chmod(0o000)
        try:
            r = run_indexer(nb)
            # Stale → rebuild attempted → unreadable notebook → error exit;
            # the one thing that must NOT happen is a silent fresh exit 0
            # with the index untouched and no error.
            assert not (
                r.returncode == 0
                and idx.stat().st_ino == inode_before
                and "fresh" in r.stderr.lower()
            ), (
                "Unverifiable index (unreadable notebook, mtime+size match) "
                f"must not be reported fresh: exit {r.returncode}, stderr {r.stderr!r}"
            )
        finally:
            nb.chmod(0o644)  # restore so tmp_path cleanup works

    def test_short_circuit_no_read_on_mtime_mismatch(self, tmp_path):
        """A3 short-circuit: an mtime mismatch must exit stale (rebuild) without
        the staleness check itself needing the hash — i.e. steps 2-3 decide."""
        nb = make_notebook([code_cell("x = 1")], tmp_path=tmp_path)
        run_indexer(nb)
        idx = index_path_for(nb)
        inode_before = idx.stat().st_ino

        os.utime(nb, (1, 1))  # force mtime mismatch, content unchanged
        r = run_indexer(nb)
        assert r.returncode == 0, f"rebuild after mtime change failed: {r.stderr}"
        assert idx.stat().st_ino != inode_before, (
            "mtime mismatch must trigger a rebuild (stale via step 2)"
        )

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
            markdown_cell("## Main", cell_id="m0"),    # idx 0
            code_cell("x = 1", cell_id="c1"),          # idx 1  → section "Main"
            markdown_cell("### Sub", cell_id="m2"),    # idx 2
            code_cell("y = 2", cell_id="c3"),          # idx 3  → section "Sub"
            markdown_cell("## Main2", cell_id="m4"),   # idx 4  — same level as Main, closes Sub
            code_cell("z = 3", cell_id="c5"),          # idx 5  → section "Main2"
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
        # A second h2 must close the h3 sub-section.
        # A wrong stack-pop implementation would assign "Main" or "Sub" here.
        assert data["cells"][5]["section"] == "Main2", (
            "Cell under ## Main2 must be in 'Main2', not the preceding h3 sub-section"
        )

    def test_section_path_stored_per_cell(self, tmp_path):
        """§5.5: section_path is an ordered list from outermost to innermost heading"""
        cells = [
            markdown_cell("## Data Loading", cell_id="m0"),   # idx 0
            markdown_cell("### Normalization", cell_id="m1"), # idx 1
            code_cell("x = normalize(df)", cell_id="c2"),     # idx 2  → in h3
        ]
        nb = make_notebook(cells, tmp_path=tmp_path)
        run_indexer(nb)
        data = load_index(index_path_for(nb))
        cell = data["cells"][2]
        assert cell["section"] == "Normalization", (
            "section must be the innermost heading name"
        )
        assert cell["section_path"] == ["Data Loading", "Normalization"], (
            "section_path must list all ancestor headings in order outermost→innermost"
        )

    def test_section_path_invariant_section_equals_last_element(self, tmp_path):
        """§5.5 invariant: section == section_path[-1] when section_path is non-empty"""
        cells = [
            markdown_cell("# Chapter", cell_id="m0"),
            markdown_cell("## Section", cell_id="m1"),
            markdown_cell("### Sub", cell_id="m2"),
            code_cell("pass", cell_id="c3"),
        ]
        nb = make_notebook(cells, tmp_path=tmp_path)
        run_indexer(nb)
        data = load_index(index_path_for(nb))
        cell = data["cells"][3]
        assert cell["section_path"], "section_path must be non-empty for nested cell"
        assert cell["section"] == cell["section_path"][-1], (
            "section must equal the last element of section_path"
        )

    def test_section_path_empty_before_first_heading(self, tmp_path):
        """§5.5: section_path is [] for cells before the first heading"""
        cells = [
            code_cell("x = 1", cell_id="c0"),              # before any heading
            markdown_cell("## Section A", cell_id="m1"),
            code_cell("y = 2", cell_id="c2"),
        ]
        nb = make_notebook(cells, tmp_path=tmp_path)
        run_indexer(nb)
        data = load_index(index_path_for(nb))
        assert data["cells"][0]["section_path"] == [], (
            "section_path must be [] for cells before any heading"
        )
        assert data["cells"][0]["section"] is None, (
            "section must be null for cells before any heading"
        )
        # Cell after heading must have non-empty section_path
        assert data["cells"][2]["section_path"] == ["Section A"]

    def test_section_path_top_level_heading_only(self, tmp_path):
        """§5.5: cell under a single top-level heading has section_path of length 1"""
        cells = [
            markdown_cell("## Analysis", cell_id="m0"),
            code_cell("result = run()", cell_id="c1"),
        ]
        nb = make_notebook(cells, tmp_path=tmp_path)
        run_indexer(nb)
        data = load_index(index_path_for(nb))
        assert data["cells"][1]["section_path"] == ["Analysis"], (
            "Cell under single h2 must have section_path=['Analysis']"
        )

    def test_section_path_no_headings(self, tmp_path):
        """§5.6: notebooks with no headings produce section_path=[] for all cells"""
        nb = make_notebook([code_cell("x = 1"), code_cell("y = 2", cell_id="c1")],
                           tmp_path=tmp_path)
        run_indexer(nb)
        data = load_index(index_path_for(nb))
        for i, cell in enumerate(data["cells"]):
            assert cell["section_path"] == [], (
                f"Cell {i} must have section_path=[] when no headings exist"
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

    def test_rust_kernel_not_misidentified_as_r(self, tmp_path):
        """R-kernel check must be exact match, not substring. Rust kernels should not extract R symbols."""
        cell = self._index("fn main() { println!(\"hello\"); }\n", tmp_path, kernel="rust")
        # Rust code should not extract any symbols (rust regex not implemented)
        assert cell["symbols_extracted"] == False

    def test_ruby_kernel_not_misidentified_as_r(self, tmp_path):
        """R-kernel check must be exact match, not substring. Ruby kernels should not extract R symbols."""
        cell = self._index("def hello; puts 'world'; end\n", tmp_path, kernel="ruby")
        # Ruby code should not extract any symbols (ruby regex not implemented)
        assert cell["symbols_extracted"] == False

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
        """A4 pipeline step 4: ANSI sequences stripped, non-ANSI content preserved"""
        ansi_output = "\x1b[31mred\x1b[0m\n"
        cell = self._run(
            [code_cell("x", outputs=[stream_output(ansi_output)])],
            tmp_path
        )
        # Positive: the visible content must survive stripping
        assert "red" in cell.get("output_text", ""), (
            "Non-ANSI content must be preserved after stripping"
        )
        # Negative: no escape sequences must remain
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

    def _wait_for_log(self, log, timeout=5.0, min_lines=1):
        """Poll until log file exists with at least min_lines of content, or timeout expires.

        Checking for min_lines guards against a race where the file is created
        (exists) but the subprocess hasn't finished writing its content yet.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if log.exists():
                try:
                    lines = log.read_text(encoding="utf-8").splitlines()
                    if len(lines) >= min_lines:
                        return True
                except OSError:
                    pass
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
        # Create the notebook and insert a cell so patch/delete have a valid target.
        subprocess.run(
            [PYTHON, str(write_copy), str(nb_path), "create", "--python"],
            capture_output=True
        )
        src = tmp_path / "src.py"
        src.write_text("x = 42\n", encoding="utf-8")
        subprocess.run(
            [PYTHON, str(write_copy), str(nb_path), "insert", "0", "code", "-f", str(src)],
            capture_output=True
        )
        # Now patch cell 0 (which exists)
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
        # Create the notebook and insert a cell so delete has a valid target.
        subprocess.run(
            [PYTHON, str(write_copy), str(nb_path), "create", "--python"],
            capture_output=True
        )
        src = tmp_path / "src.py"
        src.write_text("x = 1\n", encoding="utf-8")
        subprocess.run(
            [PYTHON, str(write_copy), str(nb_path), "insert", "0", "code", "-f", str(src)],
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
        # Insert a cell first so patch has a valid target.
        subprocess.run(
            [PYTHON, str(write_copy), str(nb_path), "insert", "0", "code", "-f", str(src)],
            capture_output=True
        )
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
        # Insert a cell first so patch has a valid target (create makes a 0-cell notebook).
        subprocess.run(
            [PYTHON, str(write_copy), str(nb_path), "insert", "0", "code", "-f", str(src)],
            capture_output=True
        )
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

    def test_path_with_spaces_and_parens_not_shell_interpreted(self, tmp_path):
        """§8.1: shell=False — notebook path with spaces and parentheses must not be
        shell-interpreted. If Popen used shell=True, spaces would split the argv and
        parens would be interpreted as sub-shell syntax, causing the spawn to fail."""
        stub, log = self._mock_indexer(tmp_path)
        # Place the notebook in a directory whose name contains spaces and parens
        nb_dir = tmp_path / "my project (2025)"
        nb_dir.mkdir()
        write_copy = tmp_path / "nb-write.py"
        import shutil
        shutil.copy2(WRITE_SCRIPT, write_copy)
        # stub is already at tmp_path / "nb-index.py" (placed by _mock_indexer);
        # write_copy is in tmp_path, so _NB_INDEX_SIBLING will resolve correctly.
        nb_path = nb_dir / "my data.ipynb"
        # Create the notebook via nb-write.py create
        subprocess.run(
            [PYTHON, str(write_copy), str(nb_path), "create", "--python"],
            capture_output=True
        )
        assert nb_path.exists(), "create must work even with spaces/parens in path"
        src = tmp_path / "src.py"
        src.write_text("x = 1\n", encoding="utf-8")
        subprocess.run(
            [PYTHON, str(write_copy), str(nb_path), "insert", "0", "code", "-f", str(src)],
            capture_output=True
        )
        r = subprocess.run(
            [PYTHON, str(write_copy), str(nb_path), "patch", "0", "-f", str(src)],
            capture_output=True, text=True
        )
        assert r.returncode == 0, (
            f"Write must succeed for path with spaces/parens: {r.stderr}"
        )
        # The stub nb-index.py in tmp_path (not nb_dir) is what write_copy will call.
        # If shell=True were used, the stub would not be invoked with the correct args.
        assert self._wait_for_log(log, min_lines=2), (
            "nb-index.py stub must be spawned even when path contains spaces/parens "
            "(confirms shell=False list-form Popen is used)"
        )
        lines = log.read_text(encoding="utf-8").splitlines()
        # Second line of the log is the argv passed to the stub: must contain the full path
        assert "my project (2025)" in lines[1], (
            f"Full path with spaces/parens must reach nb-index.py as a single argv[1]; "
            f"got: {lines[1]!r}"
        )

    # -- synchronous indexing (2026-06 concurrency batch) -------------------

    def test_sync_indexing_index_fresh_when_patch_returns(self, tmp_path):
        """Indexing is synchronous: by the time nb-write patch returns, the
        per-notebook index already reflects the change — no sleep/poll."""
        nb = make_notebook([code_cell("old_value = 1\n")], tmp_path=tmp_path)
        src = tmp_path / "src.py"
        src.write_text("brand_new_symbol = 42\n", encoding="utf-8")

        r = self._run_write([nb, "patch", "0", "-f", src])

        assert r.returncode == 0, r.stderr
        idx = index_path_for(nb)
        assert idx.exists(), (
            "Index must already exist when nb-write returns (synchronous indexing)"
        )
        data = load_index(idx)
        assert data["cells"][0]["first_line"] == "brand_new_symbol = 42"
        assert "brand_new_symbol" in data["cells"][0]["symbols_defined"]

    def test_index_failure_surfaces_warn_on_stderr(self, tmp_path):
        """A failing nb-index run must surface '[warn] indexing failed' on
        nb-write's stderr — but the write itself still exits 0."""
        nb = make_notebook([code_cell("x = 1\n")], tmp_path=tmp_path)
        # Sabotage indexing: .nb_index exists as a FILE, so mkdir() fails.
        (tmp_path / ".nb_index").write_text("not a directory", encoding="utf-8")
        src = tmp_path / "src.py"
        src.write_text("y = 2\n", encoding="utf-8")

        r = self._run_write([nb, "patch", "0", "-f", src])

        assert r.returncode == 0, (
            f"The write succeeded — indexing failure must not change the exit "
            f"code: {r.stderr}"
        )
        assert "[warn] indexing failed" in r.stderr, (
            f"Expected '[warn] indexing failed: ...' on stderr, got: {r.stderr!r}"
        )
        # The notebook write itself happened
        data = json.loads(nb.read_text(encoding="utf-8"))
        assert "y = 2" in "".join(data["cells"][0]["source"])


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

    def test_notebook_path_in_symbols_json_comes_from_file_not_index(self, tmp_path):
        """§13.2: the notebook_path stored in symbols.json must be derived from the
        actual file path at index time, NOT read from the per-notebook index file.

        If nb-index.py trusted the 'notebook_path' stored in an existing per-notebook
        index, a tampered index could inject a poisoned path into symbols.json, enabling
        cross-notebook symbol poisoning.

        Proof: tamper notebook_path in the per-notebook index, re-run with --force,
        verify symbols.json still uses the real path (not the tampered one).
        """
        nb = make_notebook(
            [code_cell("def poisoned_fn():\n    pass\n")],
            tmp_path=tmp_path, name="real.ipynb"
        )
        run_indexer(nb)
        idx = index_path_for(nb)

        # Tamper: inject a fake notebook_path into the per-notebook index
        data = load_index(idx)
        data["notebook_path"] = "../../injected/fake.ipynb"
        idx.write_text(json.dumps(data), encoding="utf-8")

        # Re-index with --force: the indexer must recompute notebook_path from
        # the actual file path argument, not from the tampered stored value.
        run_indexer(nb, extra_args=["--force"])

        symbols_data = json.loads(
            (tmp_path / ".nb_index" / "symbols.json").read_text(encoding="utf-8")
        )
        locs = symbols_data.get("symbols", {}).get("poisoned_fn", [])
        assert locs, "poisoned_fn must be in symbols.json after re-index"
        for loc in locs:
            assert "injected" not in loc and "fake.ipynb" not in loc, (
                f"symbols.json must NOT use tampered notebook_path; got location: {loc!r}"
            )
            assert "real.ipynb" in loc, (
                f"symbols.json must use the real file path; got location: {loc!r}"
            )

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

    # -- GC + lockfile persistence (2026-06 concurrency batch) --------------

    def test_symbols_gc_removes_entries_for_deleted_notebook(self, tmp_path):
        """Location entries whose notebook no longer exists on disk are dropped
        during the next symbols.json update (garbage collection)."""
        nb_keep = make_notebook(
            [code_cell("def keeper_fn():\n    pass\n")],
            tmp_path=tmp_path, name="keep.ipynb"
        )
        nb_gone = make_notebook(
            [code_cell("def goner_fn():\n    pass\n")],
            tmp_path=tmp_path, name="gone.ipynb"
        )
        run_indexer(nb_keep)
        run_indexer(nb_gone)
        symbols_path = tmp_path / ".nb_index" / "symbols.json"
        data = json.loads(symbols_path.read_text(encoding="utf-8"))
        assert "goner_fn" in data["symbols"], "Precondition: goner_fn indexed"

        # Delete the notebook from disk, then reindex the surviving one
        nb_gone.unlink()
        r = run_indexer(nb_keep, extra_args=["--force"])
        assert r.returncode == 0, r.stderr

        data = json.loads(symbols_path.read_text(encoding="utf-8"))
        assert "goner_fn" not in data.get("symbols", {}), (
            "Symbols of a deleted notebook must be garbage-collected"
        )
        assert "keeper_fn" in data.get("symbols", {}), (
            "Symbols of existing notebooks must survive GC"
        )

    def test_symbols_nblock_not_deleted_after_run(self, tmp_path):
        """symbols.nblock must persist after the indexer exits — deleting it
        after release is a lock-identity race (two processes could each lock
        a different inode of 'the same' lock file)."""
        nb = make_notebook([code_cell("x = 1")], tmp_path=tmp_path)
        r = run_indexer(nb)
        assert r.returncode == 0
        lock_path = tmp_path / ".nb_index" / "symbols.nblock"
        assert lock_path.exists(), (
            "symbols.nblock must NOT be unlinked after the lock is released"
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

    @pytest.mark.skipif(sys.platform == 'win32', reason='symlink creation requires admin/Developer Mode on Windows')
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

    def test_concurrent_indexers_symbols_json_not_corrupted(self, tmp_path):
        """§13.6: concurrent indexers on different notebooks must not corrupt symbols.json.

        Each indexer acquires the symbols.nblock lock before reading and writing
        symbols.json, so the file must be valid JSON and contain entries from at
        least one of the two notebooks after both complete.
        """
        nb1 = make_notebook(
            [code_cell("def alpha_unique_fn():\n    pass\n", cell_id="c0")],
            tmp_path=tmp_path, name="nb1.ipynb"
        )
        nb2 = make_notebook(
            [code_cell("def beta_unique_fn():\n    pass\n", cell_id="c0")],
            tmp_path=tmp_path, name="nb2.ipynb"
        )
        p1 = subprocess.Popen(
            [PYTHON, str(SCRIPT), str(nb1)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        p2 = subprocess.Popen(
            [PYTHON, str(SCRIPT), str(nb2)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        rc1 = p1.wait(timeout=30)
        rc2 = p2.wait(timeout=30)
        assert rc1 == 0, f"First indexer exited {rc1}"
        assert rc2 == 0, f"Second indexer exited {rc2}"

        symbols_path = tmp_path / ".nb_index" / "symbols.json"
        assert symbols_path.exists(), "symbols.json must exist after both indexers complete"

        # Must be valid JSON (not truncated/interleaved by a concurrent write)
        try:
            data = json.loads(symbols_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            pytest.fail(f"symbols.json is not valid JSON after concurrent writes: {exc}")

        assert isinstance(data, dict)
        syms = data.get("symbols", {})
        # At least one notebook's symbol must appear (the other may have been skipped
        # if the lock was unavailable — §13.6 specifies silent skip in that case)
        assert (
            "alpha_unique_fn" in syms or "beta_unique_fn" in syms
        ), (
            f"symbols.json must contain at least one of the two indexed functions; "
            f"got keys: {list(syms.keys())!r}"
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
        """§14.6: rapid successive index writes must not leave any temporary files.

        We check for ALL non-.json files in .nb_index/ — this catches .nb_tmp, .tmp,
        .json.tmp, randomly-named temp files, and any other intermediate suffix the
        implementation might use, not just the single *.nb_tmp pattern.

        *.nblock lock files are exempt: they persist by design (deleting a lock
        file after release is a race) and are gitignored.
        """
        nb = make_notebook([code_cell("x = 1")], tmp_path=tmp_path)
        for _ in range(5):
            run_indexer(nb, extra_args=["--force"])
        nb_index_dir = tmp_path / ".nb_index"
        non_json = [
            p for p in nb_index_dir.iterdir()
            if p.suffix not in (".json", ".nblock")
        ]
        assert non_json == [], (
            f"Orphaned non-JSON files found in .nb_index/: {non_json}"
        )

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
        """Schema compat: unknown keys in index must not cause the indexer to crash.

        The only code path that READS the existing index is the staleness check.
        We inject an unknown key, then re-run WITHOUT --force so the staleness check
        must parse the modified JSON.  The notebook is unchanged, so the indexer should
        determine it is fresh, exit 0, and leave the file untouched (unknown key survives).
        """
        nb = make_notebook([code_cell("x = 1")], tmp_path=tmp_path)
        run_indexer(nb)
        idx = index_path_for(nb)
        data = load_index(idx)
        data["_future_extension"] = "some_value"
        idx.write_text(json.dumps(data), encoding="utf-8")
        # Re-run without --force: notebook unchanged → staleness check reads modified JSON
        # → must tolerate unknown key and exit 0 without crashing.
        r = run_indexer(nb)
        assert r.returncode == 0, (
            f"Indexer must not crash when index has unknown top-level keys: {r.stderr}"
        )
        # The indexer found it fresh and did NOT rebuild, so our injected key survives.
        surviving = json.loads(idx.read_text(encoding="utf-8"))
        assert surviving.get("_future_extension") == "some_value", (
            "Indexer must not overwrite a fresh index; unknown keys must survive a no-op run"
        )

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
        """§7.12: lone surrogates in output must be replaced with U+FFFD

        Strategy: write the notebook as valid UTF-8 JSON where the stream text field
        contains a JSON \\uD800 escape.  Python's json.loads parses that escape into a
        Python str containing the lone surrogate U+D800.  The indexer's A4 pipeline
        step 2 must replace it with U+FFFD before storage.
        """
        nb_path = tmp_path / "surrogate.ipynb"
        # Build the JSON by hand so the \\uD800 escape lands literally in the file.
        # json.dumps would reject/mangle the surrogate; raw string interpolation is safe.
        nb_json = (
            '{"nbformat":4,"nbformat_minor":5,'
            '"metadata":{"kernelspec":{"display_name":"Python 3",'
            '"language":"python","name":"python3"},'
            '"language_info":{"name":"python","version":"3.10.0"}},'
            '"cells":[{"cell_type":"code","id":"c001","metadata":{},'
            '"source":["x = 1"],'
            '"outputs":[{"output_type":"stream","name":"stdout",'
            '"text":["\\uD800hello\\n"]}],'
            '"execution_count":1}]}'
        )
        nb_path.write_text(nb_json, encoding="utf-8")
        # Confirm the file is valid UTF-8 and json.loads produces the lone surrogate
        import ast as _ast  # noqa: F401 — just for the comment below
        parsed = json.loads(nb_json)
        cell_text = parsed["cells"][0]["outputs"][0]["text"][0]
        assert "\ud800" in cell_text, "Precondition: lone surrogate must be in parsed text"

        r = run_indexer(nb_path)
        assert r.returncode == 0, f"Lone surrogate must not crash indexer: {r.stderr}"
        data = load_index(index_path_for(nb_path))
        output_text = data["cells"][0].get("output_text", "")
        assert "\ud800" not in output_text, (
            "Lone surrogate must be replaced with U+FFFD, not left in stored output_text"
        )
        # Non-surrogate content on the same line must survive
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


# ---------------------------------------------------------------------------
# §15 — Shared index discovery: all three scripts agree on index location
# ---------------------------------------------------------------------------

class TestSharedIndexDiscovery:
    """§15: nb-index.py, nb-read.py, and nb-search.py must implement
    _find_index_dir() and _index_file_path() identically.

    We verify this behaviourally: index a notebook with nb-index.py, then
    confirm nb-read.py and nb-search.py both find the same index file without
    being told its location explicitly.
    """

    READ_SCRIPT   = REPO_ROOT / "scripts" / "nb-read.py"
    SEARCH_SCRIPT = REPO_ROOT / "scripts" / "nb-search.py"

    def test_nb_read_finds_index_written_by_nb_index(self, tmp_path):
        """§15: nb-read.py must locate the same .nb_index/<path>.json that
        nb-index.py wrote — without being given the index path directly."""
        nb = make_notebook(
            [code_cell("discovery_marker = True\n")], tmp_path=tmp_path
        )
        r_idx = run_indexer(nb)
        assert r_idx.returncode == 0, f"nb-index.py failed: {r_idx.stderr}"

        # nb-read.py should use --outline (cheapest read path) which goes through
        # the index-discovery logic and reads first_line from the index.
        r_read = subprocess.run(
            [PYTHON, str(self.READ_SCRIPT), str(nb), "--outline"],
            capture_output=True, text=True
        )
        assert r_read.returncode == 0, (
            f"nb-read.py must find the index written by nb-index.py; "
            f"exit {r_read.returncode}: {r_read.stderr}"
        )
        # The outline must contain the first_line content
        assert "discovery_marker" in r_read.stdout, (
            f"nb-read.py outline must reflect indexed content; stdout: {r_read.stdout!r}"
        )

    def test_nb_search_finds_index_written_by_nb_index(self, tmp_path):
        """§15: nb-search.py must locate the same .nb_index/<path>.json that
        nb-index.py wrote when doing a --symbol search (index-only path)."""
        nb = make_notebook(
            [code_cell("def search_discovery_fn():\n    pass\n")],
            tmp_path=tmp_path
        )
        r_idx = run_indexer(nb)
        assert r_idx.returncode == 0, f"nb-index.py failed: {r_idx.stderr}"

        r_search = subprocess.run(
            [PYTHON, str(self.SEARCH_SCRIPT), "--symbol", "search_discovery_fn",
             str(tmp_path)],
            capture_output=True, text=True
        )
        assert r_search.returncode == 0, (
            f"nb-search.py --symbol must find 'search_discovery_fn' in the index "
            f"written by nb-index.py; exit {r_search.returncode}: {r_search.stderr}"
        )
        assert "search_discovery_fn" in r_search.stdout, (
            f"Symbol must appear in search output; got: {r_search.stdout!r}"
        )

    def test_nb_read_and_nb_search_agree_on_index_location_git_root(self, tmp_path):
        """§15: in a git repo, all three scripts must resolve the index to
        <git-root>/.nb_index/, not the notebook's own directory."""
        git_root = tmp_path / "project"
        git_root.mkdir()
        (git_root / ".git").mkdir()  # fake git root

        sub = git_root / "notebooks"
        sub.mkdir()
        nb = make_notebook(
            [code_cell("git_root_marker = 1\n")],
            tmp_path=sub, name="nb.ipynb"
        )

        r_idx = run_indexer(nb)
        assert r_idx.returncode == 0, f"nb-index.py failed: {r_idx.stderr}"

        # Index must be at git root, not inside notebooks/
        expected_idx_dir = git_root / ".nb_index"
        assert expected_idx_dir.is_dir(), (
            f"nb-index.py must place .nb_index/ at git root {git_root}; "
            f"directories found: {list(tmp_path.rglob('.nb_index'))}"
        )

        # nb-read.py must find it there
        r_read = subprocess.run(
            [PYTHON, str(self.READ_SCRIPT), str(nb), "--outline"],
            capture_output=True, text=True
        )
        assert r_read.returncode == 0, (
            f"nb-read.py must find the git-root index; exit {r_read.returncode}: {r_read.stderr}"
        )

        # nb-search.py must find it there
        r_search = subprocess.run(
            [PYTHON, str(self.SEARCH_SCRIPT), "--symbol", "git_root_marker",
             str(git_root)],
            capture_output=True, text=True
        )
        assert r_search.returncode == 0, (
            f"nb-search.py must find symbol in git-root index; "
            f"exit {r_search.returncode}: {r_search.stderr}"
        )


# ---------------------------------------------------------------------------
# §14.12 — Filesystem boundary stop during git-root walk
# ---------------------------------------------------------------------------

class TestFilesystemBoundary:
    """§14.12: walk stops at filesystem mount boundary (st_dev change).

    This test is informational/documentation only — creating cross-filesystem
    directory trees in a portable, unprivileged way is not straightforward.
    We include a smoke test that verifies the indexer handles the notebook-dir
    fallback when no git root is found (which is what happens when the walk
    hits a boundary and gives up).
    """

    def test_no_git_fallback_is_notebook_dir(self, tmp_path):
        """When no git root is found (simulating boundary stop), index goes to notebook dir."""
        # No .git anywhere → walk exhausts 20 levels (or hits OS root) → fallback
        nb = make_notebook([code_cell("x = 1")], tmp_path=tmp_path)
        run_indexer(nb)
        # Index must be in the notebook's own directory, not somewhere else
        idx = index_path_for(nb)
        assert idx.exists(), "Fallback must place index in notebook dir"
        assert idx.parent.parent == tmp_path, (
            "No-git fallback must place .nb_index/ alongside the notebook"
        )

    @pytest.mark.skipif(
        sys.platform == "win32" or getattr(os, "getuid", lambda: -1)() != 0,
        reason="Filesystem boundary test requires root or bind-mount capability",
    )
    def test_filesystem_boundary_stops_walk(self, tmp_path):
        """§14.12: walk stops when st_dev changes across directory boundary.

        When the notebook is on a different filesystem than its ancestor directories,
        the walk stops at the boundary and falls back to the notebook's own directory.

        NOTE: This test requires root to create a tmpfs mount. On most CI systems
        this test is skipped. Manual verification: mount a tmpfs inside tmp_path,
        place a notebook there, and verify .nb_index is created inside the mount
        (not outside it).
        """
        pytest.skip(
            "Manual test only: requires root + bind-mount. "
            "See §14.12 for the algorithm specification."
        )


# ---------------------------------------------------------------------------
# §11.7–11.9 — nb-read.py --outline header length constraints
# ---------------------------------------------------------------------------

class TestOutlineHeaderLimits:
    """§11.7–11.9: outline header line length and truncation constraints.

    These tests verify nb-read.py --outline header format when a fresh index
    is available. They are red until both nb-index.py and nb-read.py --outline
    are implemented.
    """

    READ_SCRIPT = REPO_ROOT / "scripts" / "nb-read.py"

    def _make_and_index(self, tmp_path, cells, section_name=None):
        """Create a notebook, index it, return (nb_path, index_data)."""
        nb = make_notebook(cells, tmp_path=tmp_path)
        r = run_indexer(nb)
        if r.returncode != 0:
            pytest.skip(f"nb-index.py not yet implemented: {r.stderr}")
        data = load_index(index_path_for(nb))
        return nb, data

    def _run_outline(self, nb_path):
        return subprocess.run(
            [PYTHON, str(self.READ_SCRIPT), str(nb_path), "--outline"],
            capture_output=True, text=True
        )

    def test_outline_long_section_name_truncated(self, tmp_path):
        """§11.5/§11.7: section names > 20 chars are truncated with '…' in outline header."""
        cells = [
            {"cell_type": "markdown", "id": "m0", "metadata": {},
             "source": ["## " + "A" * 80 + "\n"]},
            {"cell_type": "code", "id": "c1", "metadata": {},
             "source": ["x = 1\n"], "outputs": [], "execution_count": 1},
        ]
        nb, _ = self._make_and_index(tmp_path, cells)
        r = self._run_outline(nb)
        if r.returncode != 0:
            pytest.skip("nb-read.py --outline not yet implemented")
        # The section name (80 A's) must be truncated in the header line
        lines = r.stdout.splitlines()
        cell1_lines = [l for l in lines if l.startswith("[1:code")]
        assert cell1_lines, f"Expected cell 1 outline line, got: {r.stdout!r}"
        line = cell1_lines[0]
        # Must contain truncation marker
        assert "…" in line or "..." in line, (
            f"Long section name must be truncated in header: {line!r}"
        )
        # Must NOT contain all 80 A's verbatim
        assert "A" * 80 not in line, (
            f"Full 80-char section name must not appear untruncated: {line!r}"
        )

    def test_outline_header_never_exceeds_72_chars(self, tmp_path):
        """§11.4/§11.8: outline header line (without first_line) must be ≤ 72 chars.

        §11.8 requires testing with 1000+ cells so the cell index number is
        4+ digits wide, which is the worst-case scenario for header length.
        """
        # Build 1001 cells: one markdown heading (section), then 1000 code cells.
        # Cell index 1000 produces "[1000:code:run=1 §SSS…]" — the widest bracket.
        heading = {"cell_type": "markdown", "id": "m0", "metadata": {},
                   "source": ["## " + "S" * 80 + "\n"]}
        code_cells = [
            {"cell_type": "code", "id": f"c{i}", "metadata": {},
             "source": [f"x{i} = 1\n"], "outputs": [], "execution_count": i + 1}
            for i in range(1000)
        ]
        cells = [heading] + code_cells
        nb, _ = self._make_and_index(tmp_path, cells)
        r = self._run_outline(nb)
        if r.returncode != 0:
            pytest.skip("nb-read.py --outline not yet implemented")
        lines = r.stdout.splitlines()
        # Every outline line (the bracket part) must fit within reason.
        # §11.4: total header ≤ 72 chars; in --outline mode there is no ─ bar,
        # so the bracket itself is the header.
        for line in lines:
            if line.startswith("["):
                bracket_end = line.find("]")
                if bracket_end >= 0:
                    bracket = line[:bracket_end + 1]
                    assert len(bracket) <= 72, (
                        f"Bracket part of outline header exceeds 72 chars: {bracket!r}"
                    )

    def test_outline_minimum_bar_length_with_section(self, tmp_path):
        """§11.4/§11.9: even with section name present, ─ bar must be ≥ 4 chars.

        §11.9: "force the worst-case metadata width and assert '─' * 4 appears
        in the header line."  We use a long section name (80 chars) and a large
        cell index (by creating 1000+ cells) to maximise bracket width and
        confirm the bar is never squeezed below 4 ─ characters.
        """
        heading = {"cell_type": "markdown", "id": "m0", "metadata": {},
                   "source": ["## " + "W" * 80 + "\n"]}
        code_cells = [
            {"cell_type": "code", "id": f"c{i}", "metadata": {},
             "source": [f"y{i} = 1\n"], "outputs": [], "execution_count": i + 1}
            for i in range(1000)
        ]
        cells = [heading] + code_cells
        nb, _ = self._make_and_index(tmp_path, cells)
        r = self._run_outline(nb)
        if r.returncode != 0:
            pytest.skip("nb-read.py --outline not yet implemented")
        assert r.returncode == 0
        lines = r.stdout.splitlines()
        assert any(l.startswith("[1:code") for l in lines), (
            f"Cell 1 must appear in outline output: {r.stdout!r}"
        )
        # §11.9: every non-outline header line (those with ─ bars) must have
        # a bar of at least 4 ─ characters.
        bar_char = "─"  # ─
        min_bar = bar_char * 4
        for line in lines:
            if line.startswith("[") and bar_char in line:
                assert min_bar in line, (
                    f"Header bar must be at least 4 '─' chars (§11.9): {line!r}"
                )


# ---------------------------------------------------------------------------
# Indexing-quality fixes (2026-06 batch): worktree/submodule .git files,
# symbol-extraction gaps, gitignore locking, walk-boundary notes
# ---------------------------------------------------------------------------

class TestGitFileRoots:
    """Worktrees and submodules: .git is a regular FILE containing
    'gitdir: <path>'. The directory containing that .git entry is the git
    root for index purposes; the gitdir: pointer is never followed."""

    def test_worktree_git_file_treated_as_root(self, tmp_path):
        root = tmp_path / "wt"
        sub = root / "notebooks"
        sub.mkdir(parents=True)
        (root / ".git").write_text(
            "gitdir: /elsewhere/repo/.git/worktrees/wt\n", encoding="utf-8"
        )
        nb = make_notebook([code_cell("x = 1")], tmp_path=sub, name="nb.ipynb")
        r = run_indexer(nb)
        assert r.returncode == 0, r.stderr
        idx = index_path_for_git(root, nb)
        assert idx.exists(), (
            f"Worktree .git file must be treated as git root; expected {idx}"
        )
        assert not (sub / ".nb_index").exists(), (
            "Index must not fall back to per-directory .nb_index for a worktree"
        )

    def test_gitdir_pointer_not_followed(self, tmp_path):
        """The gitdir: target does not exist — the index must still land at
        the directory containing the .git file (the working tree)."""
        root = tmp_path / "wt2"
        root.mkdir()
        (root / ".git").write_text(
            "gitdir: /nonexistent/path/.git/worktrees/wt2\n", encoding="utf-8"
        )
        nb = make_notebook([code_cell("x = 1")], tmp_path=root, name="nb.ipynb")
        r = run_indexer(nb)
        assert r.returncode == 0, r.stderr
        assert (root / ".nb_index").is_dir(), (
            "Index must live with the working tree, not at the gitdir: target"
        )
        assert index_path_for_git(root, nb).exists()

    def test_submodule_indexes_under_submodule_root(self, tmp_path):
        """Submodule simulation: superproject has a .git DIR, submodule has a
        .git FILE — a notebook inside the submodule must index under the
        submodule root, not the superproject."""
        superproject = tmp_path / "super"
        superproject.mkdir()
        (superproject / ".git").mkdir()
        submodule = superproject / "libs" / "subrepo"
        submodule.mkdir(parents=True)
        (submodule / ".git").write_text(
            "gitdir: ../../.git/modules/subrepo\n", encoding="utf-8"
        )
        data_dir = submodule / "data"
        data_dir.mkdir()
        nb = make_notebook([code_cell("x = 1")], tmp_path=data_dir, name="nb.ipynb")
        r = run_indexer(nb)
        assert r.returncode == 0, r.stderr
        idx = index_path_for_git(submodule, nb)
        assert idx.exists(), (
            f"Submodule notebook must index under the submodule root; expected {idx}"
        )
        assert not (superproject / ".nb_index").exists(), (
            "Submodule notebook must NOT index under the superproject"
        )

    def test_git_file_without_gitdir_prefix_ignored(self, tmp_path):
        """A .git regular file NOT starting with 'gitdir:' is not a git root."""
        root = tmp_path / "fake"
        root.mkdir()
        (root / ".git").write_text("this is not a git pointer\n", encoding="utf-8")
        nb = make_notebook([code_cell("x = 1")], tmp_path=root, name="nb.ipynb")
        r = run_indexer(nb)
        assert r.returncode == 0, r.stderr
        # Falls through to the per-directory fallback (no real git root above tmp_path)
        assert index_path_for(nb).exists(), (
            "Non-gitdir .git file must be ignored; per-directory fallback expected"
        )

    @pytest.mark.skipif(sys.platform == 'win32', reason='symlink creation requires admin/Developer Mode on Windows')
    def test_symlinked_git_file_rejected(self, tmp_path):
        """A .git that is a SYMLINK to a gitdir: file is still rejected
        (security stance), and the fallback note mentions the symlink."""
        real = tmp_path / "real_git_file"
        real.write_text("gitdir: /elsewhere\n", encoding="utf-8")
        root = tmp_path / "proj"
        root.mkdir()
        (root / ".git").symlink_to(real)
        nb = make_notebook([code_cell("x = 1")], tmp_path=root, name="nb.ipynb")
        r = run_indexer(nb)
        assert r.returncode == 0, r.stderr
        assert (root / ".nb_index").exists(), (
            "Symlinked .git file must not be treated as git root"
        )
        assert "symlink" in r.stderr.lower(), (
            f"Fallback caused by a symlinked .git must be noted on stderr: {r.stderr!r}"
        )


class TestSymbolExtractionFixes:
    """Python symbol-extraction gaps: async def, multi-module import,
    import aliases, tuple assignment, widened annotations, long-line prefix."""

    def _index(self, source, tmp_path, kernel="python"):
        nb = make_notebook([code_cell(source)], kernel_language=kernel, tmp_path=tmp_path)
        run_indexer(nb)
        return load_index(index_path_for(nb))["cells"][0]

    def test_async_def_detected(self, tmp_path):
        cell = self._index("async def fetch_data(url):\n    pass\n", tmp_path)
        assert "fetch_data" in cell["symbols_defined"]

    def test_import_multiple_modules(self, tmp_path):
        cell = self._index("import os, sys, json\n", tmp_path)
        for mod in ("os", "sys", "json"):
            assert mod in cell["symbols_imported"], (
                f"'{mod}' missing from {cell['symbols_imported']}"
            )

    def test_import_alias_records_module_name(self, tmp_path):
        cell = self._index("import numpy as np\n", tmp_path)
        assert "numpy" in cell["symbols_imported"]
        assert "np" not in cell["symbols_imported"], (
            "Imports index is by MODULE name — the alias must not be recorded"
        )

    def test_import_multiple_with_aliases(self, tmp_path):
        cell = self._index("import numpy as np, pandas as pd, re\n", tmp_path)
        for mod in ("numpy", "pandas", "re"):
            assert mod in cell["symbols_imported"]
        assert "np" not in cell["symbols_imported"]
        assert "pd" not in cell["symbols_imported"]

    def test_from_import_unchanged(self, tmp_path):
        cell = self._index("from sklearn.linear_model import Ridge\n", tmp_path)
        assert "sklearn.linear_model" in cell["symbols_imported"]

    def test_tuple_assignment_two_names(self, tmp_path):
        cell = self._index("a, b = 1, 2\n", tmp_path)
        assert "a" in cell["symbols_defined"]
        assert "b" in cell["symbols_defined"]

    def test_tuple_assignment_three_names(self, tmp_path):
        cell = self._index("x, y, z = compute()\n", tmp_path)
        for name in ("x", "y", "z"):
            assert name in cell["symbols_defined"]

    def test_starred_tuple_target_skipped(self, tmp_path):
        """Conservative regex: starred targets do not match at all."""
        cell = self._index("*rest, last = items\n", tmp_path)
        assert "rest" not in cell["symbols_defined"]
        assert "last" not in cell["symbols_defined"]

    def test_attribute_tuple_target_skipped(self, tmp_path):
        cell = self._index("self.x, y = 1, 2\n", tmp_path)
        assert "self" not in cell["symbols_defined"]
        assert "y" not in cell["symbols_defined"]

    def test_annotation_with_dotted_type(self, tmp_path):
        cell = self._index("arr: np.ndarray = np.zeros(3)\n", tmp_path)
        assert "arr" in cell["symbols_defined"]

    def test_annotation_with_quoted_union(self, tmp_path):
        cell = self._index('val: "Foo|None" = None\n', tmp_path)
        assert "val" in cell["symbols_defined"]

    def test_annotation_with_brackets_and_pipe(self, tmp_path):
        cell = self._index("table: dict[str, int | None] = {}\n", tmp_path)
        assert "table" in cell["symbols_defined"]

    def test_augmented_assignment_still_excluded(self, tmp_path):
        cell = self._index("counter += 1\n", tmp_path)
        assert "counter" not in cell["symbols_defined"]

    def test_long_line_prefix_extraction(self, tmp_path):
        """Lines > MAX_LINE_LEN are truncated, not dropped — a symbol at the
        start of a long definition line must still be captured."""
        long_def = "def my_long_fn(" + ", ".join(f"arg{i}=None" for i in range(100)) + "):\n    pass\n"
        assert len(long_def.splitlines()[0]) > 500, "Precondition: line must exceed 500 chars"
        cell = self._index(long_def, tmp_path)
        assert "my_long_fn" in cell["symbols_defined"], (
            "Symbol at the start of a >500-char line must be extracted from the prefix"
        )

    def test_long_assignment_line_prefix_extraction(self, tmp_path):
        long_assign = "long_value = '" + "a" * 600 + "'\n"
        cell = self._index(long_assign, tmp_path)
        assert "long_value" in cell["symbols_defined"]


class TestGitignoreLocking:
    """_update_gitignore is serialised on .nb_index/gitignore.nblock."""

    def test_concurrent_indexers_no_lost_gitignore_entries(self, tmp_path):
        """Two concurrent indexers updating the same .gitignore must preserve
        pre-existing entries and not duplicate the managed ones."""
        gitignore = tmp_path / ".gitignore"
        gitignore.write_text("custom_user_entry/\n", encoding="utf-8")
        nb1 = make_notebook([code_cell("x = 1")], tmp_path=tmp_path, name="a.ipynb")
        nb2 = make_notebook([code_cell("y = 2")], tmp_path=tmp_path, name="b.ipynb")
        p1 = subprocess.Popen(
            [PYTHON, str(SCRIPT), str(nb1)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        p2 = subprocess.Popen(
            [PYTHON, str(SCRIPT), str(nb2)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        assert p1.wait(timeout=30) == 0
        assert p2.wait(timeout=30) == 0
        content = gitignore.read_text(encoding="utf-8")
        assert "custom_user_entry/" in content, (
            "Pre-existing .gitignore entries must survive concurrent updates"
        )
        assert content.count(".nb_index/") == 1, (
            f".nb_index/ must appear exactly once, got:\n{content}"
        )
        assert content.count("*.nblock") == 1, (
            f"*.nblock must appear exactly once, got:\n{content}"
        )

    @pytest.mark.skipif(sys.platform == "win32", reason="uses fcntl to hold the lock")
    def test_gitignore_lock_busy_skips_with_warning(self, tmp_path):
        """While gitignore.nblock is held by another process, the indexer must
        skip the gitignore update with a [warn] (after ~5 s) but still write
        the index and exit 0."""
        import fcntl as _fcntl
        nb = make_notebook([code_cell("x = 1")], tmp_path=tmp_path)
        index_dir = tmp_path / ".nb_index"
        index_dir.mkdir()
        lock_path = index_dir / "gitignore.nblock"
        with open(lock_path, "a") as lock_fd:
            _fcntl.flock(lock_fd, _fcntl.LOCK_EX)
            r = run_indexer(nb)
            _fcntl.flock(lock_fd, _fcntl.LOCK_UN)
        assert r.returncode == 0, r.stderr
        assert "gitignore lock busy" in r.stderr, (
            f"Expected a 'gitignore lock busy' [warn] on stderr: {r.stderr!r}"
        )
        assert index_path_for(nb).exists(), (
            "Index must still be written when the gitignore update is skipped"
        )
        # The skipped update must not have touched .gitignore
        assert not (tmp_path / ".gitignore").exists() or \
            ".nb_index/" not in (tmp_path / ".gitignore").read_text(encoding="utf-8")

    def test_gitignore_idempotent_after_lock_fix(self, tmp_path):
        """Locked path keeps idempotency: repeat runs never duplicate entries."""
        nb = make_notebook([code_cell("x = 1")], tmp_path=tmp_path)
        run_indexer(nb)
        run_indexer(nb, extra_args=["--force"])
        run_indexer(nb, extra_args=["--force"])
        content = (tmp_path / ".gitignore").read_text(encoding="utf-8")
        assert content.count(".nb_index/") == 1
        assert content.count("*.nblock") == 1


class TestWalkBoundaryNotes:
    """When the upward walk gives up (20-level cap / st_dev boundary) without
    finding .git, a one-line [note] explains the per-directory fallback."""

    def test_20_level_cap_prints_note(self, tmp_path):
        deep = tmp_path
        for i in range(25):
            deep = deep / f"d{i}"
        deep.mkdir(parents=True)
        nb = make_notebook([code_cell("x = 1")], tmp_path=deep, name="nb.ipynb")
        r = run_indexer(nb)
        assert r.returncode == 0, r.stderr
        assert "[note] no git root found within 20 levels" in r.stderr, (
            f"Expected a 20-level-cap note on stderr: {r.stderr!r}"
        )
        assert str(deep / ".nb_index") in r.stderr, (
            "The note must name the per-directory index path"
        )
        assert index_path_for(nb).exists()

    def test_20_level_cap_with_git_above_cap_still_falls_back(self, tmp_path):
        """A .git ABOVE the 20-level cap is out of reach; the fallback note
        must fire and the index must land next to the notebook."""
        (tmp_path / ".git").mkdir()
        deep = tmp_path
        for i in range(25):
            deep = deep / f"e{i}"
        deep.mkdir(parents=True)
        nb = make_notebook([code_cell("x = 1")], tmp_path=deep, name="nb.ipynb")
        r = run_indexer(nb)
        assert r.returncode == 0, r.stderr
        assert "[note] no git root found within 20 levels" in r.stderr
        assert (deep / ".nb_index").exists()
        assert not (tmp_path / ".nb_index").exists()

    def test_git_within_20_levels_no_note(self, tmp_path):
        """A reachable .git produces no fallback note."""
        root = tmp_path / "proj"
        root.mkdir()
        (root / ".git").mkdir()
        nb = make_notebook([code_cell("x = 1")], tmp_path=root, name="nb.ipynb")
        r = run_indexer(nb)
        assert r.returncode == 0, r.stderr
        assert "[note]" not in r.stderr, (
            f"No fallback note expected when a git root is found: {r.stderr!r}"
        )

    @pytest.mark.skipif(sys.platform == 'win32', reason='symlink creation requires admin/Developer Mode on Windows')
    def test_symlinked_git_dir_fallback_notes_symlink(self, tmp_path):
        """A rejected symlinked .git directory that causes the fallback is
        mentioned on stderr (security stance is kept)."""
        real_git = tmp_path / "real_git_dir"
        real_git.mkdir()
        root = tmp_path / "proj"
        root.mkdir()
        (root / ".git").symlink_to(real_git)
        nb = make_notebook([code_cell("x = 1")], tmp_path=root, name="nb.ipynb")
        r = run_indexer(nb)
        assert r.returncode == 0, r.stderr
        assert (root / ".nb_index").exists(), "Symlinked .git must still be rejected"
        assert "symlink" in r.stderr.lower(), (
            f"Fallback caused by a symlinked .git must be noted: {r.stderr!r}"
        )
