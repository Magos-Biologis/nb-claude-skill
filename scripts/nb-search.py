#!/usr/bin/env python3
"""
nb-search.py — Search across indexed Jupyter notebooks.

Usage:
  python3 nb-search.py [--symbol | --import] [--type TYPE] [--section SECTION]
                       [--limit N] QUERY SEARCH_ROOT

Exit codes:
  0: one or more matches
  1: no matches
  2: usage error (missing args, invalid flags, search_root is a file)

stdout: results (one per line)  format: relative/path.ipynb:N: first source line
stderr: warnings ([STALE], [UNINDEXED], [WARN])
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path

MAX_FILE_SIZE = 100 * 1024 * 1024  # 100 MB
SKIP_DIRS = frozenset({"node_modules", ".venv", "venv", "__pycache__", ".tox", ".git", ".hg"})
MAX_WALK_DEPTH = 20

# ---------------------------------------------------------------------------
# ANSI sanitisation (same as nb-read.py)
# ---------------------------------------------------------------------------
_ANSI_RE = re.compile(
    r'\x1b(?:'
    r'\][^\x07\x1b]{0,512}(?:\x07|\x1b\\)'
    r'|[@-Z\\^_]'
    r'|\[[0-?]*[ -/]*[@-~]'
    r'|[^@-_]'
    r')'
)
_CTRL_RE = re.compile(r'[\x00-\x1f\x7f]')


def _sanitise(s: str) -> str:
    return _CTRL_RE.sub('', _ANSI_RE.sub('', str(s)))


# ---------------------------------------------------------------------------
# §15 — Index path resolution (re-implemented identically to nb-index.py)
# ---------------------------------------------------------------------------

def _find_index_dir(nb_path: Path) -> tuple[Path, Path | None]:
    """
    Return (index_dir, git_root_or_None).
    Walk upward from nb_path's parent looking for a real .git/ directory.
    Stops after 20 levels or if st_dev changes.
    nb_path must already be resolved.
    """
    cur = nb_path.parent
    try:
        cur_dev = os.stat(cur).st_dev
    except OSError:
        return (cur / ".nb_index", None)

    for _ in range(MAX_WALK_DEPTH):
        git_candidate = cur / ".git"
        if git_candidate.is_dir() and not git_candidate.is_symlink():
            return (cur / ".nb_index", cur)

        parent = cur.parent
        if parent == cur:
            break
        try:
            parent_dev = os.stat(parent).st_dev
        except OSError:
            break
        if parent_dev != cur_dev:
            break
        cur_dev = parent_dev
        cur = parent

    return (nb_path.parent / ".nb_index", None)


def _index_file_path(nb_path: Path) -> tuple[Path, Path, Path | None]:
    """
    Return (index_file_path, index_dir, git_root_or_None).
    nb_path must already be resolved.
    """
    index_dir, git_root = _find_index_dir(nb_path)

    if git_root is not None:
        try:
            rel = nb_path.relative_to(git_root)
        except ValueError:
            index_dir = nb_path.parent / ".nb_index"
            git_root = None
            index_file = index_dir / (nb_path.name + ".json")
        else:
            rel_posix = str(rel).replace(os.sep, "/")
            index_file = index_dir / (rel_posix + ".json")
    else:
        index_file = index_dir / (nb_path.name + ".json")

    return index_file, index_dir, git_root


# ---------------------------------------------------------------------------
# Staleness check (§A3)
# ---------------------------------------------------------------------------

def _check_staleness(index_data: dict, nb_path: Path) -> bool:
    """Returns True if the index is stale."""
    try:
        actual_mtime = os.path.getmtime(nb_path)
        actual_size = os.path.getsize(nb_path)
    except OSError:
        return True

    stored_mtime = index_data.get("notebook_mtime")
    stored_size = index_data.get("notebook_size")
    stored_hash = index_data.get("nb_content_hash")

    if stored_mtime != actual_mtime:
        return True
    if stored_size != actual_size:
        return True

    # Both match — skip file read (fast path)
    if stored_hash is None:
        return True

    # Read file for hash check (only when mtime+size both match)
    try:
        with open(nb_path, "rb") as f:
            raw = f.read()
    except OSError:
        return True

    actual_hash = hashlib.sha256(raw).hexdigest()[:16]
    return stored_hash != actual_hash


# ---------------------------------------------------------------------------
# Walk strategy (§12)
# ---------------------------------------------------------------------------

def _walk_for_index_dirs(search_root: Path):
    """
    Yield (dirpath, filenames) for all .nb_index/ directories found by walking
    search_root. Skips SKIP_DIRS. Max depth: MAX_WALK_DEPTH.
    Uses os.walk(followlinks=False).
    """
    root_str = str(search_root)
    root_depth = root_str.count(os.sep)

    for dirpath, dirnames, filenames in os.walk(root_str, followlinks=False):
        # Compute depth
        current_depth = dirpath.count(os.sep) - root_depth

        # Skip directories in SKIP_DIRS (in-place modification to prevent descent)
        dirnames[:] = [
            d for d in dirnames
            if d not in SKIP_DIRS and current_depth < MAX_WALK_DEPTH
        ]

        if os.path.basename(dirpath) == ".nb_index":
            yield dirpath, filenames


def _walk_for_ipynb(search_root: Path):
    """
    Yield all .ipynb file paths found by walking search_root.
    Skips SKIP_DIRS. Max depth: MAX_WALK_DEPTH.
    """
    root_str = str(search_root)
    root_depth = root_str.count(os.sep)

    for dirpath, dirnames, filenames in os.walk(root_str, followlinks=False):
        current_depth = dirpath.count(os.sep) - root_depth

        dirnames[:] = [
            d for d in dirnames
            if d not in SKIP_DIRS and current_depth < MAX_WALK_DEPTH
        ]

        for fname in filenames:
            if fname.endswith(".ipynb"):
                yield Path(dirpath) / fname


# ---------------------------------------------------------------------------
# notebook_path validation (§12.12)
# ---------------------------------------------------------------------------

def _validate_notebook_path(np_str: str, search_root: Path) -> Path | None:
    """
    Validate notebook_path from an index file.
    Returns resolved candidate path, or None if invalid.
    """
    # Null bytes are invalid in POSIX paths
    if "\x00" in np_str:
        return None

    raw = Path(np_str)
    try:
        candidate = raw.resolve() if raw.is_absolute() else (search_root / np_str).resolve()
    except Exception:
        return None

    try:
        if not candidate.is_relative_to(search_root.resolve()):
            return None
    except Exception:
        return None

    return candidate


# ---------------------------------------------------------------------------
# Section filter (§12.8)
# ---------------------------------------------------------------------------

def _cell_passes_section(cell: dict, section_query: str) -> bool:
    """Check if cell is within the named section."""
    section_path = cell.get("section_path")
    if section_path is not None:
        return section_query in section_path
    # Fallback for older index format without section_path
    return cell.get("section") == section_query


# ---------------------------------------------------------------------------
# Import prefix matching (§12.3)
# ---------------------------------------------------------------------------

def _import_matches(key: str, query: str) -> bool:
    """Module-boundary prefix matching."""
    return key == query or key.startswith(query + ".")


# ---------------------------------------------------------------------------
# Result formatting (§12.4)
# ---------------------------------------------------------------------------

def _format_result(nb_display_path: str, cell_index: int, first_line: str) -> str:
    """Format: relative/path.ipynb:N: first source line"""
    safe_line = _sanitise(first_line)
    return f"{nb_display_path}:{cell_index}: {safe_line}"


# ---------------------------------------------------------------------------
# Staleness signal for symbols.json fast path (§12.2)
# ---------------------------------------------------------------------------

def _symbols_json_is_fresh(symbols_data: dict, index_dir_path: Path) -> bool:
    """
    Check if symbols.json is fresh:
    1. version == 1
    2. generated_at > max_indexed_at
    3. No per-notebook .json file in index_dir is newer than generated_at
    """
    if not isinstance(symbols_data.get("version"), int):
        return False
    if symbols_data["version"] != 1:
        return False

    generated_at = symbols_data.get("generated_at", "")
    max_indexed_at = symbols_data.get("max_indexed_at", "")

    if not generated_at or not max_indexed_at:
        return False

    # generated_at must be >= max_indexed_at (symbols.json built after or at same time as indices)
    if generated_at < max_indexed_at:
        return False

    # Check if any per-notebook .json file is newer than generated_at
    # Convert generated_at ISO string to a timestamp for comparison
    try:
        from datetime import datetime, timezone
        dt = datetime.strptime(generated_at, "%Y-%m-%dT%H:%M:%SZ")
        dt = dt.replace(tzinfo=timezone.utc)
        gen_ts = dt.timestamp()
    except (ValueError, ImportError):
        return False

    try:
        for entry in os.scandir(str(index_dir_path)):
            if entry.name.endswith(".json") and entry.name != "symbols.json":
                try:
                    if entry.stat().st_mtime > gen_ts:
                        return False
                except OSError:
                    pass
    except OSError:
        return False

    return True


def _validate_location_string(loc: str, search_root: Path) -> tuple[str, int] | None:
    """
    Validate a location string 'notebook_path:cell_index' from symbols.json.
    Returns (notebook_path_str, cell_index) or None if invalid.
    """
    parts = loc.rsplit(":", 1)
    if len(parts) != 2:
        return None
    nb_part, idx_part = parts
    try:
        cell_idx = int(idx_part)
    except ValueError:
        return None

    candidate = _validate_notebook_path(nb_part, search_root)
    if candidate is None:
        return None

    return (nb_part, cell_idx)


# ---------------------------------------------------------------------------
# Core search functions
# ---------------------------------------------------------------------------

def _coerce_source(src) -> str:
    if src is None:
        return ""
    if isinstance(src, list):
        return "".join(str(s) for s in src)
    return str(src)


def _search_keyword(
    query: str,
    search_root: Path,
    type_filter: str | None,
    section_filter: str | None,
    limit: int | None,
) -> int:
    """
    Keyword search: open each .ipynb file and scan cell source text.
    Uses index to locate notebooks and get metadata.
    Returns number of results printed.
    """
    q_lower = query.lower()
    count = 0

    # Collect all indexed notebook paths and their index data
    # Also track all .ipynb files to detect unindexed ones
    indexed_nb_paths = set()  # resolved Path objects

    # First pass: find all indexed notebooks
    indexed_notebooks = []  # list of (nb_candidate_path, index_data, is_stale, nb_display)

    for index_dir_str, filenames in _walk_for_index_dirs(search_root):
        index_dir_path = Path(index_dir_str)
        for fname in filenames:
            if fname == "symbols.json" or not fname.endswith(".json"):
                continue
            json_file = index_dir_path / fname
            try:
                with open(json_file, encoding="utf-8") as f:
                    index_data = json.load(f)
            except (json.JSONDecodeError, OSError, UnicodeDecodeError):
                print(f"[WARN] corrupt index file: {json_file}", file=sys.stderr)
                continue

            if not isinstance(index_data.get("version"), int):
                print(f"[WARN] missing/invalid version in {json_file}", file=sys.stderr)
                continue
            if index_data["version"] > 1:
                print(f"[WARN] unsupported index version {index_data['version']} in {json_file}",
                      file=sys.stderr)
                continue

            np_str = index_data.get("notebook_path", "")
            if not np_str:
                continue

            candidate = _validate_notebook_path(np_str, search_root)
            if candidate is None:
                print(f"[WARN] invalid or unsafe notebook_path in {json_file}",
                      file=sys.stderr)
                continue

            indexed_nb_paths.add(candidate)

            # Check staleness
            stale = _check_staleness(index_data, candidate)
            if stale:
                # Use relative path for display
                try:
                    display = str(candidate.relative_to(search_root))
                except ValueError:
                    display = str(candidate)
                print(f"[STALE] {display}", file=sys.stderr)

            indexed_notebooks.append((candidate, index_data, stale))

    # Second pass: detect unindexed notebooks
    for nb_path in _walk_for_ipynb(search_root):
        try:
            resolved = nb_path.resolve()
        except OSError:
            resolved = nb_path
        if resolved not in indexed_nb_paths:
            try:
                display = str(nb_path.relative_to(search_root))
            except ValueError:
                display = str(nb_path)
            print(f"[UNINDEXED] {display} — run nb-index.py first", file=sys.stderr)

    # Third pass: actually search
    for candidate, index_data, stale in indexed_notebooks:
        if not candidate.exists():
            continue

        try:
            with open(candidate, encoding="utf-8-sig", errors="replace") as f:
                nb = json.load(f)
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            continue

        cells_raw = nb.get("cells", [])
        index_cells = {c["i"]: c for c in index_data.get("cells", [])} if index_data else {}

        try:
            display = str(candidate.relative_to(search_root))
        except ValueError:
            display = str(candidate)

        for i, cell in enumerate(cells_raw):
            ctype = cell.get("cell_type", "unknown")

            # Type filter
            if type_filter is not None and ctype != type_filter:
                continue

            source_str = _coerce_source(cell.get("source", []))

            # Case-insensitive keyword search
            if q_lower not in source_str.lower():
                continue

            # Section filter using index metadata
            if section_filter is not None:
                idx_cell = index_cells.get(i)
                if idx_cell is None:
                    continue
                if not _cell_passes_section(idx_cell, section_filter):
                    continue

            # First line for display
            first_line = "(empty)"
            idx_cell = index_cells.get(i)
            if idx_cell:
                first_line = idx_cell.get("first_line", "(empty)")
            else:
                for line in source_str.splitlines():
                    s = line.strip()
                    if s:
                        first_line = s[:120]
                        break

            print(_format_result(display, i, first_line))
            count += 1

            if limit is not None and count >= limit:
                return count

    return count


def _search_symbol(
    query: str,
    search_root: Path,
    type_filter: str | None,
    section_filter: str | None,
    limit: int | None,
) -> int:
    """
    Symbol search: read only index files, never open .ipynb.
    Returns number of results printed.
    """
    count = 0

    for index_dir_str, filenames in _walk_for_index_dirs(search_root):
        index_dir_path = Path(index_dir_str)

        # Try symbols.json fast path (§12.2)
        symbols_path = index_dir_path / "symbols.json"
        if symbols_path.exists():
            try:
                with open(symbols_path, encoding="utf-8") as f:
                    sym_data = json.load(f)
            except (json.JSONDecodeError, OSError):
                sym_data = None

            if sym_data is not None:
                ver = sym_data.get("version")
                if not isinstance(ver, int) or ver != 1:
                    sym_data = None  # Fall through to serial scan
                elif _symbols_json_is_fresh(sym_data, index_dir_path):
                    # Fast path: use symbols.json
                    syms = sym_data.get("symbols", {})
                    locations = syms.get(query, [])
                    for loc in locations:
                        result = _validate_location_string(loc, search_root)
                        if result is None:
                            continue
                        nb_path_str, cell_idx = result
                        # For display purposes, use the nb_path_str relative to search_root
                        candidate = _validate_notebook_path(nb_path_str, search_root)
                        if candidate is None:
                            continue
                        try:
                            display = str(candidate.relative_to(search_root))
                        except ValueError:
                            display = str(candidate)
                        # We need cell metadata — load per-notebook index
                        # to apply type/section filters and get first_line
                        # Find per-notebook index file
                        nb_index_file, _, _ = _index_file_path(candidate)
                        first_line = f"cell {cell_idx}"
                        cell_type = "code"
                        if nb_index_file.exists():
                            try:
                                with open(nb_index_file, encoding="utf-8") as nf:
                                    nb_idx = json.load(nf)
                                cells_list = nb_idx.get("cells", [])
                                for c in cells_list:
                                    if c.get("i") == cell_idx:
                                        first_line = c.get("first_line", first_line)
                                        cell_type = c.get("type", cell_type)
                                        if type_filter and cell_type != type_filter:
                                            first_line = None
                                        if section_filter and not _cell_passes_section(c, section_filter):
                                            first_line = None
                                        break
                            except (json.JSONDecodeError, OSError):
                                pass

                        if first_line is None:
                            continue

                        print(_format_result(display, cell_idx, first_line))
                        count += 1
                        if limit is not None and count >= limit:
                            return count

                    if limit is not None and count >= limit:
                        return count
                    continue  # Skip serial scan for this index_dir

        # Serial scan: read per-notebook index files
        for fname in filenames:
            if fname == "symbols.json" or not fname.endswith(".json"):
                continue
            json_file = index_dir_path / fname
            try:
                with open(json_file, encoding="utf-8") as f:
                    index_data = json.load(f)
            except (json.JSONDecodeError, OSError, UnicodeDecodeError):
                print(f"[WARN] corrupt index file: {json_file}", file=sys.stderr)
                continue

            if not isinstance(index_data.get("version"), int):
                continue
            if index_data["version"] > 1:
                print(f"[WARN] unsupported index version in {json_file}", file=sys.stderr)
                continue

            np_str = index_data.get("notebook_path", "")
            candidate = _validate_notebook_path(np_str, search_root)
            if candidate is None:
                print(f"[WARN] invalid or unsafe notebook_path in {json_file}", file=sys.stderr)
                continue

            try:
                display = str(candidate.relative_to(search_root))
            except ValueError:
                display = str(candidate)

            for cell in index_data.get("cells", []):
                if query not in cell.get("symbols_defined", []):
                    continue

                ctype = cell.get("type", "unknown")
                if type_filter and ctype != type_filter:
                    continue
                if section_filter and not _cell_passes_section(cell, section_filter):
                    continue

                first_line = cell.get("first_line", f"cell {cell['i']}")
                print(_format_result(display, cell["i"], first_line))
                count += 1
                if limit is not None and count >= limit:
                    return count

    return count


def _search_import(
    query: str,
    search_root: Path,
    type_filter: str | None,
    section_filter: str | None,
    limit: int | None,
) -> int:
    """
    Import search: read only index files, never open .ipynb.
    Module-boundary prefix matching.
    Returns number of results printed.
    """
    count = 0

    for index_dir_str, filenames in _walk_for_index_dirs(search_root):
        index_dir_path = Path(index_dir_str)

        # Try symbols.json fast path for imports
        symbols_path = index_dir_path / "symbols.json"
        if symbols_path.exists():
            try:
                with open(symbols_path, encoding="utf-8") as f:
                    sym_data = json.load(f)
            except (json.JSONDecodeError, OSError):
                sym_data = None

            if sym_data is not None:
                ver = sym_data.get("version")
                if not isinstance(ver, int) or ver != 1:
                    sym_data = None
                elif _symbols_json_is_fresh(sym_data, index_dir_path):
                    # Fast path: use imports from symbols.json
                    imps = sym_data.get("imports", {})
                    for key, locations in imps.items():
                        if not _import_matches(key, query):
                            continue
                        for loc in locations:
                            result = _validate_location_string(loc, search_root)
                            if result is None:
                                continue
                            nb_path_str, cell_idx = result
                            candidate = _validate_notebook_path(nb_path_str, search_root)
                            if candidate is None:
                                continue
                            try:
                                display = str(candidate.relative_to(search_root))
                            except ValueError:
                                display = str(candidate)
                            # Load per-notebook index for filters and first_line
                            nb_index_file, _, _ = _index_file_path(candidate)
                            first_line = f"cell {cell_idx}"
                            if nb_index_file.exists():
                                try:
                                    with open(nb_index_file, encoding="utf-8") as nf:
                                        nb_idx = json.load(nf)
                                    for c in nb_idx.get("cells", []):
                                        if c.get("i") == cell_idx:
                                            first_line = c.get("first_line", first_line)
                                            cell_type = c.get("type", "code")
                                            if type_filter and cell_type != type_filter:
                                                first_line = None
                                            if section_filter and not _cell_passes_section(c, section_filter):
                                                first_line = None
                                            break
                                except (json.JSONDecodeError, OSError):
                                    pass
                            if first_line is None:
                                continue
                            print(_format_result(display, cell_idx, first_line))
                            count += 1
                            if limit is not None and count >= limit:
                                return count

                    if limit is not None and count >= limit:
                        return count
                    continue  # Skip serial scan

        # Serial scan
        for fname in filenames:
            if fname == "symbols.json" or not fname.endswith(".json"):
                continue
            json_file = index_dir_path / fname
            try:
                with open(json_file, encoding="utf-8") as f:
                    index_data = json.load(f)
            except (json.JSONDecodeError, OSError, UnicodeDecodeError):
                print(f"[WARN] corrupt index file: {json_file}", file=sys.stderr)
                continue

            if not isinstance(index_data.get("version"), int):
                continue
            if index_data["version"] > 1:
                print(f"[WARN] unsupported index version in {json_file}", file=sys.stderr)
                continue

            np_str = index_data.get("notebook_path", "")
            candidate = _validate_notebook_path(np_str, search_root)
            if candidate is None:
                print(f"[WARN] invalid or unsafe notebook_path in {json_file}", file=sys.stderr)
                continue

            try:
                display = str(candidate.relative_to(search_root))
            except ValueError:
                display = str(candidate)

            for cell in index_data.get("cells", []):
                imported = cell.get("symbols_imported", [])
                if not any(_import_matches(k, query) for k in imported):
                    continue

                ctype = cell.get("type", "unknown")
                if type_filter and ctype != type_filter:
                    continue
                if section_filter and not _cell_passes_section(cell, section_filter):
                    continue

                first_line = cell.get("first_line", f"cell {cell['i']}")
                print(_format_result(display, cell["i"], first_line))
                count += 1
                if limit is not None and count >= limit:
                    return count

    return count


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Search across indexed Jupyter notebooks",
        add_help=True,
    )
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument("--symbol", action="store_true",
                            help="Search for symbol definitions (index-only)")
    mode_group.add_argument("--import", dest="import_mode", action="store_true",
                            help="Search for import statements (index-only)")
    parser.add_argument("--type", dest="cell_type",
                        choices=["code", "markdown", "raw"],
                        help="Filter by cell type")
    parser.add_argument("--section", dest="section",
                        help="Filter by section name")
    parser.add_argument("--limit", type=int, default=None,
                        help="Stop after N results")
    parser.add_argument("query", help="Search query")
    parser.add_argument("search_root", help="Directory to search")

    # Custom error handling for exit code 2
    try:
        args = parser.parse_args()
    except SystemExit:
        sys.exit(2)

    # Validate search_root
    search_root = Path(args.search_root)
    if search_root.is_file():
        print(f"Error: search_root must be a directory, got file: {args.search_root}",
              file=sys.stderr)
        sys.exit(2)
    if not search_root.is_dir():
        print(f"Error: search_root does not exist or is not a directory: {args.search_root}",
              file=sys.stderr)
        sys.exit(2)

    search_root = search_root.resolve()

    query = args.query
    type_filter = args.cell_type
    section_filter = args.section
    limit = args.limit

    if args.symbol:
        count = _search_symbol(query, search_root, type_filter, section_filter, limit)
    elif args.import_mode:
        count = _search_import(query, search_root, type_filter, section_filter, limit)
    else:
        count = _search_keyword(query, search_root, type_filter, section_filter, limit)

    sys.exit(0 if count > 0 else 1)


if __name__ == "__main__":
    main()
