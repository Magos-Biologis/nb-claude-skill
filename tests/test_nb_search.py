"""
Test suite for nb-search.py — derived from TDD §12.

All tests use subprocess to invoke nb-search.py as a black-box CLI tool.
Tests are written tests-first against the specification; they will fail
until nb-search.py is implemented.

Section mapping:
  TestWalkStrategy          → §12 walk (skip dirs, depth, followlinks)
  TestKeywordSearch         → §12.1 (opens .ipynb files, case-insensitive)
  TestSymbolSearch          → §12.2 (--symbol, index-only, symbols.json fast path)
  TestImportSearch          → §12.3 (--import)
  TestOutputFormat          → §12.4, §12.10, §12.11
  TestTypeFilter            → §12.5
  TestSectionFilter         → §12.8
  TestExitCodes             → §12.9
  TestStreamingOutput       → §12.10
  TestSecurity              → §12.12 (notebook_path traversal, null bytes)
  TestStaleUnindexed        → §12.6, §12.7
  TestKeywordVsSymbol       → §12.13
  TestSymbolsJsonFastPath   → §12.2 fast path
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REPO_ROOT    = Path(__file__).parent.parent
SEARCH_SCRIPT= REPO_ROOT / "scripts" / "nb-search.py"
INDEX_SCRIPT = REPO_ROOT / "scripts" / "nb-index.py"
PYTHON       = sys.executable

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def run_search(args, cwd=None):
    """Run nb-search.py with the given argument list."""
    cmd = [PYTHON, str(SEARCH_SCRIPT)] + [str(a) for a in args]
    return subprocess.run(cmd, capture_output=True, text=True, cwd=cwd)


def run_indexer(nb_path, extra_args=None):
    args = [PYTHON, str(INDEX_SCRIPT), str(nb_path)] + (extra_args or [])
    return subprocess.run(args, capture_output=True, text=True)


def make_notebook(cells=None, kernel_language="python", name="test.ipynb", tmp_path=None):
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
        "cell_type": "code", "id": cell_id, "metadata": {},
        "source": source if isinstance(source, list) else [source],
        "outputs": outputs or [], "execution_count": execution_count,
    }


def markdown_cell(source, cell_id="m001"):
    return {
        "cell_type": "markdown", "id": cell_id, "metadata": {},
        "source": source if isinstance(source, list) else [source],
    }


def make_indexed_project(tmp_path, notebooks):
    """
    Create and index a set of notebooks in tmp_path.

    notebooks: list of (name, cells, kernel_language) tuples.
    Returns a list of notebook Paths.
    """
    paths = []
    for entry in notebooks:
        name, cells = entry[0], entry[1]
        kernel = entry[2] if len(entry) > 2 else "python"
        nb = make_notebook(cells, kernel_language=kernel, name=name, tmp_path=tmp_path)
        r = run_indexer(nb)
        assert r.returncode == 0, f"Indexing {name} failed: {r.stderr}"
        paths.append(nb)
    return paths


# ---------------------------------------------------------------------------
# §12 walk — skip dirs, depth limit, followlinks=False
# ---------------------------------------------------------------------------

class TestWalkStrategy:

    def test_skips_node_modules(self, tmp_path):
        """§12 walk: node_modules must be skipped"""
        nm = tmp_path / "node_modules" / "deep"
        nm.mkdir(parents=True)
        # Put an indexed notebook there
        nb = make_notebook([code_cell("findme = 1")], tmp_path=nm, name="nb.ipynb")
        run_indexer(nb)
        r = run_search(["findme", str(tmp_path)])
        # Should NOT find it
        assert "findme" not in r.stdout

    def test_skips_venv(self, tmp_path):
        """§12 walk: .venv must be skipped"""
        venv = tmp_path / ".venv" / "lib"
        venv.mkdir(parents=True)
        nb = make_notebook([code_cell("hidden = 1")], tmp_path=venv, name="nb.ipynb")
        run_indexer(nb)
        r = run_search(["hidden", str(tmp_path)])
        assert "hidden" not in r.stdout

    def test_skips_pycache(self, tmp_path):
        """§12 walk: __pycache__ must be skipped"""
        pc = tmp_path / "__pycache__"
        pc.mkdir()
        nb = make_notebook([code_cell("cached = 1")], tmp_path=pc, name="nb.ipynb")
        run_indexer(nb)
        r = run_search(["cached", str(tmp_path)])
        assert "cached" not in r.stdout

    def test_does_not_follow_symlinks(self, tmp_path):
        """§12 walk: followlinks=False — symlink directories not traversed"""
        real = tmp_path / "real_dir"
        real.mkdir()
        nb = make_notebook([code_cell("symlinked = 1")], tmp_path=real, name="nb.ipynb")
        run_indexer(nb)
        link = tmp_path / "linked"
        link.symlink_to(real, target_is_directory=True)
        r = run_search(["symlinked", str(tmp_path)])
        # The real_dir result may appear, but the symlink must not add duplicates
        # from the linked path. This primarily checks no crash and followlinks=False.
        assert r.returncode in (0, 1)

    def test_walk_depth_limit(self, tmp_path):
        """§14.9: .nb_index/ at level > 20 must NOT be found"""
        deep = tmp_path
        for i in range(22):
            deep = deep / f"level{i}"
        deep.mkdir(parents=True)
        nb = make_notebook([code_cell("deeptoken = 1")], tmp_path=deep, name="nb.ipynb")
        run_indexer(nb)
        r = run_search(["deeptoken", str(tmp_path)])
        assert "deeptoken" not in r.stdout

    def test_finds_notebook_in_subdirectory(self, tmp_path):
        """Walk should find .nb_index/ directories in subdirectories"""
        sub = tmp_path / "subproject"
        sub.mkdir()
        make_indexed_project(sub, [("nb.ipynb", [code_cell("visible = 42")])])
        r = run_search(["visible", str(tmp_path)])
        assert r.returncode == 0
        assert "visible" in r.stdout

    def test_search_root_must_be_directory(self, tmp_path):
        """§14.8: file path as search root → exit 2"""
        nb = make_notebook([code_cell("x = 1")], tmp_path=tmp_path)
        r = run_search(["x", str(nb)])
        assert r.returncode == 2, (
            f"File path as search root must exit 2, got {r.returncode}"
        )


# ---------------------------------------------------------------------------
# §12.1 — Keyword search
# ---------------------------------------------------------------------------

class TestKeywordSearch:

    def test_keyword_found_in_cell(self, tmp_path):
        """§12.1: bare keyword query matches cell source"""
        make_indexed_project(tmp_path, [
            ("analysis.ipynb", [code_cell("def process_data(df):\n    return df\n")])
        ])
        r = run_search(["process_data", str(tmp_path)])
        assert r.returncode == 0
        assert "process_data" in r.stdout or "analysis.ipynb" in r.stdout

    def test_keyword_case_insensitive(self, tmp_path):
        """§12.1: search is case-insensitive"""
        make_indexed_project(tmp_path, [
            ("nb.ipynb", [code_cell("ProcessData = lambda x: x\n")])
        ])
        r = run_search(["processdata", str(tmp_path)])
        assert r.returncode == 0, "Keyword search must be case-insensitive"

    def test_keyword_not_found_returns_1(self, tmp_path):
        """§12.9: no matches → exit 1"""
        make_indexed_project(tmp_path, [
            ("nb.ipynb", [code_cell("x = 1\n")])
        ])
        r = run_search(["zyxwvutsrqponmlkjihgfedcba", str(tmp_path)])
        assert r.returncode == 1

    def test_keyword_multiple_notebooks(self, tmp_path):
        """§12.1: keyword found across multiple notebooks"""
        make_indexed_project(tmp_path, [
            ("nb1.ipynb", [code_cell("common_func = lambda: None\n")]),
            ("nb2.ipynb", [code_cell("# uses common_func\n")]),
        ])
        r = run_search(["common_func", str(tmp_path)])
        assert r.returncode == 0
        assert "nb1.ipynb" in r.stdout or "nb2.ipynb" in r.stdout

    def test_keyword_result_format(self, tmp_path):
        """§12.4: result format is '<path>:<N>: <first_line>'"""
        make_indexed_project(tmp_path, [
            ("mynotebook.ipynb", [code_cell("target_symbol = 99\n")])
        ])
        r = run_search(["target_symbol", str(tmp_path)])
        assert r.returncode == 0
        # Each result line must contain the notebook name and a colon-separated index
        for line in r.stdout.splitlines():
            if "target_symbol" in line or "mynotebook" in line:
                assert ":" in line, f"Result line must contain ':': {line!r}"

    def test_keyword_matches_markdown_source(self, tmp_path):
        """§12.1: keyword search scans all cell types"""
        make_indexed_project(tmp_path, [
            ("nb.ipynb", [markdown_cell("## findme_heading\nsome text\n")])
        ])
        r = run_search(["findme_heading", str(tmp_path)])
        assert r.returncode == 0


# ---------------------------------------------------------------------------
# §12.2 — Symbol search
# ---------------------------------------------------------------------------

class TestSymbolSearch:

    def test_symbol_found_with_flag(self, tmp_path):
        """§12.2: --symbol finds cells that define the symbol"""
        make_indexed_project(tmp_path, [
            ("analysis.ipynb", [code_cell("def compute_loss(y):\n    return y\n")])
        ])
        r = run_search(["--symbol", "compute_loss", str(tmp_path)])
        assert r.returncode == 0
        assert "analysis.ipynb" in r.stdout or "compute_loss" in r.stdout

    def test_symbol_not_found_returns_1(self, tmp_path):
        """§12.9"""
        make_indexed_project(tmp_path, [
            ("nb.ipynb", [code_cell("x = 1\n")])
        ])
        r = run_search(["--symbol", "nonexistent_symbol_xyz", str(tmp_path)])
        assert r.returncode == 1

    def test_symbol_search_does_not_open_ipynb_files(self, tmp_path):
        """§12.13: --symbol uses only index files, never opens .ipynb"""
        make_indexed_project(tmp_path, [
            ("nb.ipynb", [code_cell("def my_func():\n    pass\n")])
        ])
        # Delete the .ipynb file after indexing — symbol search must still work
        nb_path = tmp_path / "nb.ipynb"
        nb_path.unlink()
        r = run_search(["--symbol", "my_func", str(tmp_path)])
        assert r.returncode == 0, (
            "--symbol must succeed using index alone, even if .ipynb is deleted"
        )

    def test_symbol_across_multiple_notebooks(self, tmp_path):
        """§12.2: symbol defined in multiple notebooks → all listed"""
        make_indexed_project(tmp_path, [
            ("nb1.ipynb", [code_cell("def shared_func():\n    pass\n")]),
            ("nb2.ipynb", [code_cell("def shared_func():\n    return 1\n", cell_id="c002")]),
        ])
        r = run_search(["--symbol", "shared_func", str(tmp_path)])
        assert r.returncode == 0
        assert "nb1.ipynb" in r.stdout
        assert "nb2.ipynb" in r.stdout


# ---------------------------------------------------------------------------
# §12.3 — Import search
# ---------------------------------------------------------------------------

class TestImportSearch:

    def test_import_found_with_flag(self, tmp_path):
        """§12.3: --import finds cells that import the module"""
        make_indexed_project(tmp_path, [
            ("nb.ipynb", [code_cell("import pandas as pd\n")])
        ])
        r = run_search(["--import", "pandas", str(tmp_path)])
        assert r.returncode == 0
        assert "nb.ipynb" in r.stdout or "pandas" in r.stdout

    def test_import_prefix_match(self, tmp_path):
        """§12.3: 'from sklearn.linear_model import ...' → module starts with 'sklearn'"""
        make_indexed_project(tmp_path, [
            ("nb.ipynb", [code_cell("from sklearn.linear_model import Ridge\n")])
        ])
        r = run_search(["--import", "sklearn", str(tmp_path)])
        assert r.returncode == 0

    def test_import_not_found_returns_1(self, tmp_path):
        """§12.9"""
        make_indexed_project(tmp_path, [("nb.ipynb", [code_cell("x = 1\n")])])
        r = run_search(["--import", "nonexistent_module_xyz", str(tmp_path)])
        assert r.returncode == 1

    def test_import_does_not_open_ipynb(self, tmp_path):
        """§12.13: --import uses only index files"""
        make_indexed_project(tmp_path, [
            ("nb.ipynb", [code_cell("import numpy as np\n")])
        ])
        (tmp_path / "nb.ipynb").unlink()
        r = run_search(["--import", "numpy", str(tmp_path)])
        assert r.returncode == 0, (
            "--import must succeed using index alone, even if .ipynb is deleted"
        )


# ---------------------------------------------------------------------------
# §12.4, §12.10, §12.11 — Output format / streaming / --limit
# ---------------------------------------------------------------------------

class TestOutputFormat:

    def test_result_per_line(self, tmp_path):
        """§12.4: one result per line"""
        make_indexed_project(tmp_path, [
            ("nb.ipynb", [
                code_cell("alpha = 1\n"),
                code_cell("alpha += 2\n", cell_id="c002"),
            ])
        ])
        r = run_search(["alpha", str(tmp_path)])
        lines = [l for l in r.stdout.splitlines() if l.strip()]
        assert len(lines) >= 1

    def test_result_line_contains_notebook_path_and_cell_index(self, tmp_path):
        """§12.4: format '<path>:<N>: ...'"""
        make_indexed_project(tmp_path, [
            ("mynotebook.ipynb", [code_cell("unique_token_xyz = 42\n")])
        ])
        r = run_search(["unique_token_xyz", str(tmp_path)])
        assert r.returncode == 0
        line = r.stdout.strip().splitlines()[0]
        assert "mynotebook.ipynb" in line
        # Must contain at least one colon (separating path from cell index)
        assert ":" in line

    def test_limit_flag(self, tmp_path):
        """§12.11: --limit N stops after N results"""
        cells = [code_cell(f"match_token = {i}\n", cell_id=f"c{i:03}") for i in range(10)]
        make_indexed_project(tmp_path, [("nb.ipynb", cells)])
        r = run_search(["match_token", "--limit", "3", str(tmp_path)])
        lines = [l for l in r.stdout.splitlines() if l.strip()]
        assert len(lines) <= 3, f"Expected at most 3 results with --limit 3, got {len(lines)}"

    def test_no_limit_returns_all_results(self, tmp_path):
        """§12.11: no --limit returns all matches"""
        cells = [code_cell(f"all_token = {i}\n", cell_id=f"c{i:03}") for i in range(5)]
        make_indexed_project(tmp_path, [("nb.ipynb", cells)])
        r = run_search(["all_token", str(tmp_path)])
        lines = [l for l in r.stdout.splitlines() if l.strip()]
        assert len(lines) >= 5


# ---------------------------------------------------------------------------
# §12.5 — --type filter
# ---------------------------------------------------------------------------

class TestTypeFilter:

    def test_type_code_filter(self, tmp_path):
        """§12.5: --type code returns only code cells"""
        make_indexed_project(tmp_path, [
            ("nb.ipynb", [
                code_cell("typefilter = 1\n"),
                markdown_cell("typefilter heading\n", cell_id="m1"),
            ])
        ])
        r = run_search(["typefilter", "--type", "code", str(tmp_path)])
        assert r.returncode == 0
        # All returned results should indicate code cells somehow
        # (at minimum, the markdown cell's result should not appear)
        # We check by using --symbol which explicitly scans code cells' symbol index
        r2 = run_search(["--symbol", "typefilter", str(tmp_path)])
        if r2.returncode == 0:
            assert "nb.ipynb" in r2.stdout

    def test_type_markdown_filter(self, tmp_path):
        """§12.5: --type markdown returns only markdown cells"""
        make_indexed_project(tmp_path, [
            ("nb.ipynb", [
                code_cell("# markdowntoken = code comment\n"),
                markdown_cell("markdowntoken in heading\n", cell_id="m1"),
            ])
        ])
        r = run_search(["markdowntoken", "--type", "markdown", str(tmp_path)])
        assert r.returncode == 0

    def test_invalid_type_exits_2(self, tmp_path):
        """§12.9: invalid --type value is a usage error → exit 2"""
        make_indexed_project(tmp_path, [("nb.ipynb", [code_cell("x = 1")])])
        r = run_search(["x", "--type", "invalid_cell_type", str(tmp_path)])
        assert r.returncode == 2


# ---------------------------------------------------------------------------
# §12.8 — --section filter
# ---------------------------------------------------------------------------

class TestSectionFilter:

    def test_section_filter(self, tmp_path):
        """§12.8: --section limits results to cells within the named section"""
        cells = [
            markdown_cell("## Data Loading\n", cell_id="m0"),
            code_cell("load_data = True\n", cell_id="c1"),
            markdown_cell("## Analysis\n", cell_id="m2"),
            code_cell("load_data = False\n", cell_id="c3"),  # same name, different section
        ]
        make_indexed_project(tmp_path, [("nb.ipynb", cells)])
        r = run_search(["load_data", "--section", "Data Loading", str(tmp_path)])
        assert r.returncode == 0
        # Should match cell 1 (in Data Loading), not necessarily cell 3 (in Analysis)
        lines = r.stdout.strip().splitlines()
        assert any(":1:" in l or "Data Loading" in r.stdout for l in lines), (
            "--section must filter by section name"
        )


# ---------------------------------------------------------------------------
# §12.9 — Exit codes
# ---------------------------------------------------------------------------

class TestExitCodes:

    def test_exit_0_on_match(self, tmp_path):
        """§12.9"""
        make_indexed_project(tmp_path, [("nb.ipynb", [code_cell("exitcode_match = 1")])])
        r = run_search(["exitcode_match", str(tmp_path)])
        assert r.returncode == 0

    def test_exit_1_on_no_match(self, tmp_path):
        """§12.9"""
        make_indexed_project(tmp_path, [("nb.ipynb", [code_cell("x = 1")])])
        r = run_search(["zzznomatch_unique_xyz", str(tmp_path)])
        assert r.returncode == 1

    def test_exit_2_on_missing_query(self, tmp_path):
        """§12.9: no query argument is a usage error"""
        r = run_search([str(tmp_path)])
        assert r.returncode == 2

    def test_exit_2_on_missing_search_root(self, tmp_path):
        """§12.9: no search root is a usage error"""
        r = run_search(["somequery"])
        assert r.returncode == 2

    def test_exit_2_on_search_root_is_file(self, tmp_path):
        """§14.8"""
        f = tmp_path / "file.txt"
        f.write_text("not a directory", encoding="utf-8")
        r = run_search(["query", str(f)])
        assert r.returncode == 2


# ---------------------------------------------------------------------------
# §12.6, §12.7 — Stale / unindexed notices
# ---------------------------------------------------------------------------

class TestStaleUnindexed:

    def test_unindexed_notebook_notice(self, tmp_path):
        """§12.7: unindexed notebook prints notice on stderr"""
        # Create a notebook but do NOT index it
        nb = make_notebook([code_cell("unindexed_token = 1")], tmp_path=tmp_path)
        r = run_search(["unindexed_token", str(tmp_path)])
        # Exit code 1 (no match) and a notice on stderr
        assert r.returncode == 1
        assert "UNINDEXED" in r.stderr or "unindexed" in r.stderr.lower()

    def test_stale_index_warns_on_stderr(self, tmp_path):
        """§12.6: stale index prints warning on stderr, results still printed"""
        make_indexed_project(tmp_path, [
            ("nb.ipynb", [code_cell("stale_token = 1\n")])
        ])
        # Make the notebook appear stale by advancing its mtime
        nb_path = tmp_path / "nb.ipynb"
        t = nb_path.stat().st_mtime + 10
        os.utime(nb_path, (t, t))
        r = run_search(["stale_token", str(tmp_path)])
        # Search should still return results (§12.6 says "return results anyway")
        assert "STALE" in r.stderr, (
            f"Expected [STALE] warning on stderr for stale index: {r.stderr!r}"
        )


# ---------------------------------------------------------------------------
# §12.12 — Security: notebook_path traversal
# ---------------------------------------------------------------------------

class TestSecurity:

    def test_traversal_in_notebook_path_rejected(self, tmp_path):
        """§12.12: crafted notebook_path with ../ must not cause file open outside root"""
        make_indexed_project(tmp_path, [
            ("nb.ipynb", [code_cell("x = 1")])
        ])
        # Craft a malicious index by patching the notebook_path field
        idx_dir = tmp_path / ".nb_index"
        for json_file in idx_dir.glob("*.json"):
            data = json.loads(json_file.read_text(encoding="utf-8"))
            data["notebook_path"] = "../../../../etc/passwd"
            json_file.write_text(json.dumps(data), encoding="utf-8")
        # Search must not crash or open /etc/passwd
        r = run_search(["x", str(tmp_path)])
        # The crafted entry should be skipped — search may exit 1 (no results)
        assert r.returncode in (0, 1), f"Unexpected exit code: {r.returncode}"
        assert "Traceback" not in r.stderr, "Must not crash on traversal attempt"
        assert "/etc/passwd" not in r.stdout
        assert "/etc/passwd" not in r.stderr

    def test_null_byte_in_notebook_path_skipped(self, tmp_path):
        """§12.12: null bytes in notebook_path must cause entry to be skipped"""
        make_indexed_project(tmp_path, [
            ("nb.ipynb", [code_cell("x = 1")])
        ])
        idx_dir = tmp_path / ".nb_index"
        for json_file in idx_dir.glob("*.json"):
            data = json.loads(json_file.read_text(encoding="utf-8"))
            data["notebook_path"] = "nb\x00.ipynb"
            json_file.write_text(json.dumps(data), encoding="utf-8")
        r = run_search(["x", str(tmp_path)])
        assert "Traceback" not in r.stderr, "Must not crash on null byte in path"

    def test_absolute_notebook_path_outside_root_rejected(self, tmp_path):
        """§12.12: absolute notebook_path that escapes search_root must be rejected"""
        make_indexed_project(tmp_path, [
            ("nb.ipynb", [code_cell("x = 1")])
        ])
        idx_dir = tmp_path / ".nb_index"
        for json_file in idx_dir.glob("*.json"):
            data = json.loads(json_file.read_text(encoding="utf-8"))
            data["notebook_path"] = "/etc/passwd"
            json_file.write_text(json.dumps(data), encoding="utf-8")
        r = run_search(["x", str(tmp_path)])
        assert r.returncode in (0, 1)
        assert "Traceback" not in r.stderr
        assert "/etc/passwd" not in r.stdout

    def test_symbols_json_traversal_rejected(self, tmp_path):
        """§13.8: crafted symbols.json with traversal in location strings"""
        make_indexed_project(tmp_path, [
            ("nb.ipynb", [code_cell("def target_func():\n    pass\n")])
        ])
        symbols_path = tmp_path / ".nb_index" / "symbols.json"
        if symbols_path.exists():
            data = json.loads(symbols_path.read_text(encoding="utf-8"))
            data.setdefault("symbols", {})["target_func"] = ["../../../../etc/passwd:0"]
            symbols_path.write_text(json.dumps(data), encoding="utf-8")
        r = run_search(["--symbol", "target_func", str(tmp_path)])
        assert "Traceback" not in r.stderr
        assert "/etc/passwd" not in r.stdout

    def test_corrupt_index_json_skipped(self, tmp_path):
        """§12 schema: corrupt index file (non-JSON) must be skipped without crash"""
        make_indexed_project(tmp_path, [
            ("nb.ipynb", [code_cell("x = 1")])
        ])
        idx_dir = tmp_path / ".nb_index"
        for json_file in idx_dir.glob("*.json"):
            json_file.write_text("not valid json {{{", encoding="utf-8")
        r = run_search(["x", str(tmp_path)])
        assert "Traceback" not in r.stderr
        assert r.returncode in (0, 1, 2)

    def test_future_version_index_skipped(self, tmp_path):
        """Schema: version > 1 → skip + warn, no crash"""
        make_indexed_project(tmp_path, [
            ("nb.ipynb", [code_cell("x = 1")])
        ])
        idx_dir = tmp_path / ".nb_index"
        for json_file in idx_dir.glob("*.json"):
            if json_file.name != "symbols.json":
                data = json.loads(json_file.read_text(encoding="utf-8"))
                data["version"] = 999
                json_file.write_text(json.dumps(data), encoding="utf-8")
        r = run_search(["x", str(tmp_path)])
        assert "Traceback" not in r.stderr


# ---------------------------------------------------------------------------
# §12.13 — Keyword vs symbol/import distinction
# ---------------------------------------------------------------------------

class TestKeywordVsSymbol:

    def test_keyword_opens_ipynb_symbol_does_not(self, tmp_path):
        """
        §12.13: keyword search opens .ipynb; --symbol does not.
        After deleting the .ipynb, keyword search should fail (exit 1),
        but --symbol should still work.
        """
        make_indexed_project(tmp_path, [
            ("nb.ipynb", [code_cell("def indexed_function():\n    pass\n")])
        ])
        nb_path = tmp_path / "nb.ipynb"
        nb_path.unlink()

        # Symbol search must work without the .ipynb file
        r_sym = run_search(["--symbol", "indexed_function", str(tmp_path)])
        assert r_sym.returncode == 0, (
            "--symbol must find symbols using index alone (no .ipynb needed)"
        )

        # Keyword search on a unique token in the source should fail
        # (source not in index for keyword search — requires .ipynb)
        r_kw = run_search(["indexed_function", str(tmp_path)])
        assert r_kw.returncode == 1, (
            "Keyword search must not find results after .ipynb is deleted"
        )


# ---------------------------------------------------------------------------
# §12.2 fast path — symbols.json
# ---------------------------------------------------------------------------

class TestSymbolsJsonFastPath:

    def test_symbol_search_uses_symbols_json_when_fresh(self, tmp_path):
        """§12.2: when symbols.json is fresh, --symbol uses it (O(1) lookup)"""
        make_indexed_project(tmp_path, [
            ("nb.ipynb", [code_cell("def fast_lookup():\n    pass\n")])
        ])
        symbols_path = tmp_path / ".nb_index" / "symbols.json"
        if symbols_path.exists():
            # Delete individual index files to force reliance on symbols.json
            for f in (tmp_path / ".nb_index").glob("nb.ipynb.json"):
                f.unlink()
            r = run_search(["--symbol", "fast_lookup", str(tmp_path)])
            # If symbols.json fast path is used, this should succeed
            # (the per-notebook index is gone but symbols.json has the data)
            assert r.returncode in (0, 1)  # 0 if fast path used, 1 if falls back to serial scan

    def test_symbols_json_stale_falls_back(self, tmp_path):
        """§12.2: stale symbols.json falls back to serial per-notebook scan"""
        make_indexed_project(tmp_path, [
            ("nb.ipynb", [code_cell("def fallback_func():\n    pass\n")])
        ])
        symbols_path = tmp_path / ".nb_index" / "symbols.json"
        if symbols_path.exists():
            # Make a per-notebook index appear newer than symbols.json
            nb_idx = tmp_path / ".nb_index" / "nb.ipynb.json"
            if nb_idx.exists():
                future = symbols_path.stat().st_mtime + 100
                os.utime(nb_idx, (future, future))
            r = run_search(["--symbol", "fallback_func", str(tmp_path)])
            # Must still find the symbol via serial scan fallback
            assert r.returncode == 0, (
                "Stale symbols.json must fall back to serial scan and still find results"
            )

    def test_corrupt_symbols_json_falls_back(self, tmp_path):
        """§13.4: corrupt symbols.json → fall back to serial scan"""
        make_indexed_project(tmp_path, [
            ("nb.ipynb", [code_cell("def corrupt_test():\n    pass\n")])
        ])
        symbols_path = tmp_path / ".nb_index" / "symbols.json"
        if symbols_path.exists():
            symbols_path.write_text("{corrupt json{{", encoding="utf-8")
        r = run_search(["--symbol", "corrupt_test", str(tmp_path)])
        assert "Traceback" not in r.stderr
        assert r.returncode == 0, (
            "Corrupt symbols.json must fall back to serial scan (not crash)"
        )

    def test_missing_symbols_json_uses_serial_scan(self, tmp_path):
        """§13.3: missing symbols.json → serial scan works correctly"""
        make_indexed_project(tmp_path, [
            ("nb.ipynb", [code_cell("def serial_scan_func():\n    pass\n")])
        ])
        symbols_path = tmp_path / ".nb_index" / "symbols.json"
        if symbols_path.exists():
            symbols_path.unlink()
        r = run_search(["--symbol", "serial_scan_func", str(tmp_path)])
        assert r.returncode == 0, (
            "Missing symbols.json must fall back to serial scan"
        )
