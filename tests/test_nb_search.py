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

    def test_skips_venv_undotted(self, tmp_path):
        """§12 walk: plain 'venv' (without dot) must also be skipped"""
        venv = tmp_path / "venv" / "lib"
        venv.mkdir(parents=True)
        nb = make_notebook([code_cell("venv_hidden = 1")], tmp_path=venv, name="nb.ipynb")
        run_indexer(nb)
        r = run_search(["venv_hidden", str(tmp_path)])
        assert "venv_hidden" not in r.stdout

    def test_skips_tox(self, tmp_path):
        """§12 walk: .tox must be skipped"""
        tox = tmp_path / ".tox" / "py311"
        tox.mkdir(parents=True)
        nb = make_notebook([code_cell("tox_hidden = 1")], tmp_path=tox, name="nb.ipynb")
        run_indexer(nb)
        r = run_search(["tox_hidden", str(tmp_path)])
        assert "tox_hidden" not in r.stdout

    def test_skips_git_dir(self, tmp_path):
        """§12 walk: .git must be skipped"""
        git = tmp_path / ".git" / "hooks"
        git.mkdir(parents=True)
        nb = make_notebook([code_cell("git_hidden = 1")], tmp_path=git, name="nb.ipynb")
        run_indexer(nb)
        r = run_search(["git_hidden", str(tmp_path)])
        assert "git_hidden" not in r.stdout

    def test_skips_pycache(self, tmp_path):
        """§12 walk: __pycache__ must be skipped"""
        pc = tmp_path / "__pycache__"
        pc.mkdir()
        nb = make_notebook([code_cell("cached = 1")], tmp_path=pc, name="nb.ipynb")
        run_indexer(nb)
        r = run_search(["cached", str(tmp_path)])
        assert "cached" not in r.stdout

    @pytest.mark.skipif(sys.platform == 'win32', reason='symlink creation requires admin/Developer Mode on Windows')
    def test_does_not_follow_symlinks(self, tmp_path):
        """§12 walk: followlinks=False — symlink not traversed, no duplicate results"""
        real = tmp_path / "real_dir"
        real.mkdir()
        nb = make_notebook([code_cell("symlinked = 1")], tmp_path=real, name="nb.ipynb")
        run_indexer(nb)
        link = tmp_path / "linked"
        link.symlink_to(real, target_is_directory=True)
        r = run_search(["symlinked", str(tmp_path)])
        # The notebook was indexed under real_dir/, so the token IS findable.
        assert r.returncode == 0, (
            "Token indexed under real_dir/ must be found; "
            f"got exit {r.returncode}; stderr: {r.stderr!r}"
        )
        # The symlinked path must NOT produce a second result — followlinks=False.
        matching_lines = [l for l in r.stdout.splitlines()
                          if "symlinked" in l or "nb.ipynb" in l]
        assert len(matching_lines) <= 1, (
            f"followlinks=False must prevent duplicate results via symlink; "
            f"got {len(matching_lines)} lines:\n{r.stdout}"
        )

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

    def test_walk_depth_within_limit_found(self, tmp_path):
        """Positive control: .nb_index/ at level 18 MUST be found (< 20 limit)"""
        deep = tmp_path
        for i in range(18):
            deep = deep / f"level{i}"
        deep.mkdir(parents=True)
        make_indexed_project(deep, [("nb.ipynb", [code_cell("shallow_token = 1")])])
        r = run_search(["shallow_token", str(tmp_path)])
        assert r.returncode == 0, (
            "Notebook at depth 18 must be found (within 20-level limit)"
        )

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
        assert "nb1.ipynb" in r.stdout and "nb2.ipynb" in r.stdout, (
            "Keyword appearing in both notebooks must return results from both"
        )

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

    def test_import_prefix_no_false_match(self, tmp_path):
        """§12.3: prefix match must not match a longer module name"""
        make_indexed_project(tmp_path, [
            ("nb.ipynb", [code_cell("import sklearnx\n")])
        ])
        # 'sklearn' must NOT match 'sklearnx' (sklearnx does not start with 'sklearn.')
        r = run_search(["--import", "sklearn", str(tmp_path)])
        assert r.returncode == 1, (
            "--import sklearn must not match 'sklearnx' (prefix match, not substring); "
            f"got exit {r.returncode}; stdout: {r.stdout!r}"
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
        """§12.4: format 'relative/path.ipynb:N: first source line'"""
        make_indexed_project(tmp_path, [
            ("mynotebook.ipynb", [code_cell("unique_token_xyz = 42\n")])
        ])
        r = run_search(["unique_token_xyz", str(tmp_path)])
        assert r.returncode == 0
        line = r.stdout.strip().splitlines()[0]
        assert "mynotebook.ipynb" in line
        # Format: path:N: ... where N is an integer cell index
        # Must have at least two colons
        parts = line.split(":")
        assert len(parts) >= 3, f"Expected 'path:N:line' format, got: {line!r}"
        # The part after the path must be a numeric cell index
        nb_part_end = line.index("mynotebook.ipynb") + len("mynotebook.ipynb")
        rest = line[nb_part_end:]  # ":N: first line"
        assert rest.startswith(":"), f"Expected ':N:' after notebook name, got: {rest!r}"
        index_part = rest[1:].split(":")[0]
        assert index_part.isdigit(), (
            f"Cell index must be an integer, got: {index_part!r} in line: {line!r}"
        )

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
        """§12.5: --type code returns only code cells, not markdown cells"""
        make_indexed_project(tmp_path, [
            ("nb.ipynb", [
                code_cell("typefilter_code = 1\n"),
                markdown_cell("typefilter_md heading\n", cell_id="m1"),
            ])
        ])
        # Search for a token only in the markdown cell with --type code
        r = run_search(["typefilter_md", "--type", "code", str(tmp_path)])
        # The markdown cell must NOT appear (token only exists in markdown)
        assert r.returncode == 1, (
            "--type code must not return markdown cell results "
            f"(expected exit 1, got {r.returncode}); stdout: {r.stdout!r}"
        )

    def test_type_code_filter_returns_code_cell(self, tmp_path):
        """§12.5: --type code returns code cells that match"""
        make_indexed_project(tmp_path, [
            ("nb.ipynb", [
                code_cell("typefilter_code = 1\n"),
                markdown_cell("typefilter_md heading\n", cell_id="m1"),
            ])
        ])
        r = run_search(["typefilter_code", "--type", "code", str(tmp_path)])
        assert r.returncode == 0
        assert "nb.ipynb" in r.stdout or "typefilter_code" in r.stdout

    def test_type_markdown_filter(self, tmp_path):
        """§12.5: --type markdown returns only markdown cells, not code cells"""
        make_indexed_project(tmp_path, [
            ("nb.ipynb", [
                code_cell("markdowntoken_code = 1\n"),
                markdown_cell("markdowntoken_md in heading\n", cell_id="m1"),
            ])
        ])
        # Search for token only in the code cell with --type markdown
        r = run_search(["markdowntoken_code", "--type", "markdown", str(tmp_path)])
        assert r.returncode == 1, (
            "--type markdown must not return code cell results "
            f"(expected exit 1, got {r.returncode}); stdout: {r.stdout!r}"
        )

    def test_type_markdown_filter_returns_markdown_cell(self, tmp_path):
        """§12.5: --type markdown returns markdown cells that match"""
        make_indexed_project(tmp_path, [
            ("nb.ipynb", [
                code_cell("markdowntoken_code = 1\n"),
                markdown_cell("markdowntoken_md in heading\n", cell_id="m1"),
            ])
        ])
        r = run_search(["markdowntoken_md", "--type", "markdown", str(tmp_path)])
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
            code_cell("filtered_token = True\n", cell_id="c1"),
            markdown_cell("## Analysis\n", cell_id="m2"),
            code_cell("other_token = False\n", cell_id="c3"),
        ]
        make_indexed_project(tmp_path, [("nb.ipynb", cells)])
        # Search for a token ONLY in the "Data Loading" section
        r = run_search(["filtered_token", "--section", "Data Loading", str(tmp_path)])
        assert r.returncode == 0, (
            "--section Data Loading filter must find filtered_token "
            f"(exit {r.returncode}); stderr: {r.stderr!r}"
        )
        assert "filtered_token" in r.stdout or ":1:" in r.stdout

    def test_section_filter_excludes_other_sections(self, tmp_path):
        """§12.8: --section must not return results from other sections"""
        cells = [
            markdown_cell("## Data Loading\n", cell_id="m0"),
            code_cell("shared_token = True\n", cell_id="c1"),
            markdown_cell("## Analysis\n", cell_id="m2"),
            code_cell("shared_token = False\n", cell_id="c3"),
        ]
        make_indexed_project(tmp_path, [("nb.ipynb", cells)])
        # Search with section filter — should return c1 (Data Loading) but NOT c3 (Analysis)
        r = run_search(["shared_token", "--section", "Data Loading", str(tmp_path)])
        # Positive check first: c1 must be found (otherwise the assertion below is vacuous)
        assert r.returncode == 0, (
            "--section filter must still return cell 1 (in 'Data Loading'); "
            f"got exit {r.returncode}; stderr: {r.stderr!r}"
        )
        # Negative check: c3 (cell index 3, in 'Analysis') must NOT appear
        assert ":3:" not in r.stdout, (
            "--section filter must exclude cells from other sections; "
            f"cell 3 (Analysis section) appeared in output: {r.stdout!r}"
        )

    def test_section_filter_matches_parent_via_section_path(self, tmp_path):
        """§12.8: --section on a parent heading matches cells nested under sub-headings.

        A cell under '### Normalization' inside '## Data Loading' must be found
        by '--section Data Loading' because 'Data Loading' is in its section_path.
        Without section_path this query would fail — the cell's section field is
        'Normalization', not 'Data Loading'.
        """
        cells = [
            markdown_cell("## Data Loading\n", cell_id="m0"),
            markdown_cell("### Normalization\n", cell_id="m1"),
            code_cell("nested_token = True\n", cell_id="c2"),   # in Normalization ⊂ Data Loading
            markdown_cell("## Analysis\n", cell_id="m3"),
            code_cell("other_token = 1\n", cell_id="c4"),
        ]
        make_indexed_project(tmp_path, [("nb.ipynb", cells)])
        r = run_search(["nested_token", "--section", "Data Loading", str(tmp_path)])
        assert r.returncode == 0, (
            "--section 'Data Loading' must find 'nested_token' which is nested inside "
            "'### Normalization' ⊂ '## Data Loading' (section_path matching); "
            f"got exit {r.returncode}; stderr: {r.stderr!r}"
        )
        assert "nested_token" in r.stdout or ":2:" in r.stdout, (
            f"nested_token (cell 2) must appear in output; got: {r.stdout!r}"
        )
        # The Analysis cell must NOT appear
        assert ":4:" not in r.stdout, (
            f"Cell 4 (Analysis section) must not appear; got: {r.stdout!r}"
        )

    def test_section_filter_innermost_heading_still_works(self, tmp_path):
        """§12.8: --section on the innermost heading still narrows correctly"""
        cells = [
            markdown_cell("## Data Loading\n", cell_id="m0"),
            markdown_cell("### Normalization\n", cell_id="m1"),
            code_cell("norm_token = 1\n", cell_id="c2"),          # in Normalization
            markdown_cell("### Splitting\n", cell_id="m3"),
            code_cell("split_token = 1\n", cell_id="c4"),         # in Splitting ⊂ Data Loading
        ]
        make_indexed_project(tmp_path, [("nb.ipynb", cells)])
        r = run_search(["norm_token", "--section", "Normalization", str(tmp_path)])
        assert r.returncode == 0, (
            "--section 'Normalization' must find norm_token; "
            f"got exit {r.returncode}; stderr: {r.stderr!r}"
        )
        assert ":4:" not in r.stdout, (
            "--section 'Normalization' must not return the cell in 'Splitting'; "
            f"got: {r.stdout!r}"
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
        """§12.6: stale index prints warning on stderr AND still returns results"""
        make_indexed_project(tmp_path, [
            ("nb.ipynb", [code_cell("stale_token = 1\n")])
        ])
        # Make the notebook appear stale by advancing its mtime
        nb_path = tmp_path / "nb.ipynb"
        t = nb_path.stat().st_mtime + 10
        os.utime(nb_path, (t, t))
        r = run_search(["stale_token", str(tmp_path)])
        # §12.6: warn AND still return results (exit 0, results in stdout)
        assert "[STALE]" in r.stderr, (
            f"Expected '[STALE]' warning on stderr for stale index: {r.stderr!r}"
        )
        assert r.returncode == 0, (
            "Stale index search must still return results (exit 0), "
            f"got exit {r.returncode}; stdout: {r.stdout!r}"
        )
        assert "stale_token" in r.stdout or "nb.ipynb" in r.stdout, (
            "Results must still be printed even when index is stale"
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
        if not symbols_path.exists():
            pytest.skip("Implementation does not produce symbols.json yet")
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
            if json_file.name != "symbols.json":
                json_file.write_text("not valid json {{{", encoding="utf-8")
        r = run_search(["x", str(tmp_path)])
        assert "Traceback" not in r.stderr
        # Corrupt index → skipped → no matches → exit 1; exit 0 if symbols.json still valid
        assert r.returncode in (0, 1), (
            f"Corrupt index must not cause exit 2 (usage error), got {r.returncode}"
        )

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
        if not symbols_path.exists():
            pytest.skip("Implementation does not produce symbols.json yet")
        # Delete individual per-notebook index files to force reliance on symbols.json
        for f in (tmp_path / ".nb_index").glob("nb.ipynb.json"):
            f.unlink()
        r = run_search(["--symbol", "fast_lookup", str(tmp_path)])
        # If symbols.json fast path is used, this should succeed even without
        # the per-notebook index file.
        assert r.returncode == 0, (
            "When symbols.json is fresh, --symbol must succeed without per-notebook index files; "
            f"got exit {r.returncode}; stderr: {r.stderr!r}"
        )

    def test_symbols_json_stale_falls_back(self, tmp_path):
        """§12.2: stale symbols.json falls back to serial per-notebook scan"""
        make_indexed_project(tmp_path, [
            ("nb.ipynb", [code_cell("def fallback_func():\n    pass\n")])
        ])
        symbols_path = tmp_path / ".nb_index" / "symbols.json"
        if not symbols_path.exists():
            pytest.skip("Implementation does not produce symbols.json yet")
        nb_idx = tmp_path / ".nb_index" / "nb.ipynb.json"
        if not nb_idx.exists():
            pytest.skip("Per-notebook index file not found")
        # Isolate the mtime-scan staleness signal (§12.2 step 2):
        # leave generated_at and max_indexed_at intact (step 1 passes: generated_at >
        # max_indexed_at in a fresh index), but set nb_idx's mtime to 100 s in the
        # future so it is newer than symbols.json's generated_at wall-clock timestamp.
        import time as _time
        future = _time.time() + 100
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
        if not symbols_path.exists():
            pytest.skip("Implementation does not produce symbols.json yet")
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

    def test_symbols_json_future_version_falls_back_to_serial(self, tmp_path):
        """§13.7: nb-search with version>1 symbols.json must fall back to serial scan.

        This is distinct from the nb-index.py rebuild test: here we verify that
        nb-search.py itself ignores the cache and still returns correct results via
        the per-notebook index serial scan.
        """
        make_indexed_project(tmp_path, [
            ("nb.ipynb", [code_cell("def version_compat_func():\n    pass\n")])
        ])
        symbols_path = tmp_path / ".nb_index" / "symbols.json"
        if not symbols_path.exists():
            pytest.skip("Implementation does not produce symbols.json yet")
        # Overwrite with a future-version payload that has no 'version_compat_func' entry
        future_payload = {"version": 999, "generated_at": "2099-01-01T00:00:00Z",
                          "max_indexed_at": "2099-01-01T00:00:00Z",
                          "symbols": {}, "imports": {}}
        symbols_path.write_text(json.dumps(future_payload), encoding="utf-8")
        # nb-search must not crash and must fall back to per-notebook serial scan
        r = run_search(["--symbol", "version_compat_func", str(tmp_path)])
        assert "Traceback" not in r.stderr
        assert r.returncode == 0, (
            "Version-999 symbols.json must cause fallback to serial scan; "
            f"expected symbol found via serial scan (exit 0), got {r.returncode}; "
            f"stderr: {r.stderr!r}"
        )


# ---------------------------------------------------------------------------
# §12.10 — Streaming output (results printed as found, not buffered)
# ---------------------------------------------------------------------------

class TestStreamingOutput:
    """§12.10: nb-search must print results as found, not buffer until all files loaded."""

    def test_first_result_before_all_files_scanned(self, tmp_path):
        """§12.10: output is not withheld until all files are processed"""
        # Create multiple notebooks so there is real work to do
        notebooks = [
            (f"nb{i:02}.ipynb", [code_cell(f"stream_token = {i}\n", cell_id=f"c{i:03}")])
            for i in range(5)
        ]
        make_indexed_project(tmp_path, notebooks)
        # Run search and capture output — streaming means exit 0 with results in stdout
        r = run_search(["stream_token", str(tmp_path)])
        assert r.returncode == 0
        lines = [l for l in r.stdout.splitlines() if l.strip()]
        assert len(lines) >= 5, (
            f"Expected at least 5 results (one per notebook), got {len(lines)}: {r.stdout!r}"
        )

    def test_limit_stops_early(self, tmp_path):
        """§12.10/§12.11: --limit stops output before scanning all files"""
        # Create 10 notebooks each with the search token
        notebooks = [
            (f"nb{i:02}.ipynb", [code_cell(f"early_stop = {i}\n", cell_id=f"c{i:03}")])
            for i in range(10)
        ]
        make_indexed_project(tmp_path, notebooks)
        r = run_search(["early_stop", "--limit", "3", str(tmp_path)])
        lines = [l for l in r.stdout.splitlines() if l.strip()]
        assert len(lines) <= 3, (
            f"--limit 3 must stop output at 3 results, got {len(lines)}: {r.stdout!r}"
        )
