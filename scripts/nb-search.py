#!/usr/bin/env python3
"""
nb-search.py — Search across indexed Jupyter notebooks.

Usage:
  python3 nb-search.py [--symbol | --import] [--type TYPE] [--section SECTION]
                       [--limit N] [--in-outputs] QUERY SEARCH_ROOT

--in-outputs (keyword mode only): also match output text of code cells;
output hits show "[output] <line>" as the result text.

Exit codes:
  0: one or more matches
  1: no matches
  2: usage error (missing args, invalid flags, search_root is a file)

stdout: results (one per line)  format: relative/path.ipynb:N: first source line
stderr: warnings ([STALE], [UNINDEXED], [WARN], [DUP])
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8')

MAX_FILE_SIZE = 100 * 1024 * 1024  # 100 MB
SKIP_DIRS = frozenset({"node_modules", ".venv", "venv", "__pycache__", ".tox", ".git", ".hg", ".ipynb_checkpoints"})
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

    # Check file size limit
    if actual_size > MAX_FILE_SIZE:
        try:
            display = str(nb_path.relative_to(Path.cwd()))
        except (ValueError, OSError):
            display = str(nb_path)
        print(f"[WARN] {display}: exceeds 100 MB limit, skipped", file=sys.stderr)
        return True

    stored_mtime = index_data.get("notebook_mtime")
    stored_size = index_data.get("notebook_size")
    stored_hash = index_data.get("nb_content_hash")

    if stored_mtime is not None and stored_size is not None:
        if stored_mtime != actual_mtime or stored_size != actual_size:
            return True
        # True fast path: stored mtime AND size both match — treat as fresh
        # WITHOUT reading the notebook (same freshness rule as nb-read.py).
        return False

    # Cannot decide from mtime+size (stored values missing) — fall back to
    # the content hash, which requires reading the file.
    if stored_hash is None:
        return True
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

def _find_upward_index_dir(search_root: Path) -> Path | None:
    """
    Walk UP from search_root (same algorithm as nb-index.py's _find_index_dir:
    <= MAX_WALK_DEPTH levels, real .git dir that is_dir() and not is_symlink(),
    stop on st_dev change) looking for a git root strictly ABOVE search_root.

    Returns that git root's .nb_index directory if it exists, else None.
    A symlinked .nb_index is accepted — nb-index.py writes through one, so
    rejecting it here would make indexed notebooks silently unsearchable
    (symlink checks on notebooks and other paths are unaffected). A git root
    equal to search_root is already covered by the downward walk, so None is
    returned in that case.
    """
    cur = search_root
    try:
        cur_dev = os.stat(cur).st_dev
    except OSError:
        return None

    for _ in range(MAX_WALK_DEPTH):
        git_candidate = cur / ".git"
        if git_candidate.is_dir() and not git_candidate.is_symlink():
            if cur == search_root:
                return None  # downward walk already covers this dir
            index_dir = cur / ".nb_index"
            if index_dir.is_dir():  # symlinked .nb_index accepted (see docstring)
                return index_dir
            return None

        parent = cur.parent
        if parent == cur:
            break  # reached filesystem root
        try:
            parent_dev = os.stat(parent).st_dev
        except OSError:
            break
        if parent_dev != cur_dev:
            break  # filesystem boundary
        cur_dev = parent_dev
        cur = parent

    return None


def _collect_index_files(index_dir: Path):
    """
    Return relative (posix-style) paths of all files under index_dir,
    recursing into subdirectories (git-root indexes mirror the repo layout:
    .nb_index/<repo-relative-path>.json).

    SKIP_DIRS pruning is deliberately NOT applied inside the mirror: every
    entry under .nb_index was explicitly indexed (nb-index.py also writes
    indexes for notebooks under venv/, node_modules/, ...) and the mirror is
    small. Nested `.nb_index` directory names ARE pruned, so a nested index
    dir is never collected under the wrong index_base (the downward walk
    yields it separately with its own base).

    Depth note: the MAX_WALK_DEPTH cap here is measured from the .nb_index
    dir itself, not from search_root. The mirror replicates repo-relative
    paths starting at the index base, so its depth budget mirrors the
    indexer's repo-relative depth and is intentionally independent of where
    the search was started from. followlinks=False; depth-capped.
    """
    names = []
    base = str(index_dir)
    base_depth = base.count(os.sep)
    for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
        depth = dirpath.count(os.sep) - base_depth
        dirnames[:] = [
            d for d in dirnames
            if d != ".nb_index" and depth < MAX_WALK_DEPTH
        ]
        for fname in filenames:
            rel = os.path.relpath(os.path.join(dirpath, fname), base)
            names.append(rel.replace(os.sep, "/"))
    return names


def _walk_for_index_dirs(search_root: Path):
    """
    Yield (dirpath, filenames) for all .nb_index/ directories relevant to
    search_root:

    1. The .nb_index of a git root found by walking UP from search_root
       (if any, and only when strictly above search_root).
    2. All .nb_index/ directories found by walking DOWN from search_root.

    filenames are paths relative to the .nb_index dir (may contain
    subdirectory components for git-root indexes). Skips SKIP_DIRS.
    Max depth: MAX_WALK_DEPTH. Uses os.walk(followlinks=False).
    """
    upward = _find_upward_index_dir(search_root)
    if upward is not None:
        yield str(upward), _collect_index_files(upward)

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
        # Deterministic order so cross-index dedup has a stable winner
        # (".nb_index" sorts before typical subdir names, so the index
        # closest to search_root is claimed first).
        dirnames.sort()

        if os.path.basename(dirpath) == ".nb_index":
            # Collect recursively (git-root indexes nest by repo path);
            # prevent os.walk from re-yielding the nested dirs.
            dirnames[:] = []
            yield dirpath, _collect_index_files(Path(dirpath))
            continue

        # os.walk(followlinks=False) never descends into a *symlinked*
        # .nb_index, but nb-index.py happily writes through one — yield it
        # explicitly so indexed notebooks stay searchable. Real .nb_index
        # dirs are handled above when os.walk reaches them as dirpath.
        for d in dirnames:
            if d == ".nb_index":
                p = os.path.join(dirpath, d)
                if os.path.islink(p) and os.path.isdir(p):
                    yield p, _collect_index_files(Path(p))


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

def _validate_notebook_path(np_str: str, index_base: Path) -> Path | None:
    """
    Validate notebook_path from an index file.

    index_base is the parent directory of the .nb_index dir that contains the
    index file (the git root for repo indexes, the notebook's own directory
    otherwise). Index-stored relative paths are relative to that base — NOT
    to search_root. The safety containment check is against the same base:
    a notebook_path escaping its index base is UNSAFE → None (caller warns).

    Scope filtering (under search_root) is a separate concern — see
    _in_scope() — applied by callers after resolution.

    Returns resolved candidate path, or None if invalid/unsafe.
    """
    # Null bytes are invalid in POSIX paths
    if "\x00" in np_str:
        return None

    raw = Path(np_str)
    try:
        candidate = raw.resolve() if raw.is_absolute() else (index_base / np_str).resolve()
    except Exception:
        return None

    try:
        if not candidate.is_relative_to(index_base.resolve()):
            return None
    except Exception:
        return None

    return candidate


def _in_scope(candidate: Path, search_root: Path) -> bool:
    """
    Scope check: is the (already safety-validated) notebook under search_root?
    A notebook outside search_root but inside its index base is SAFE but OUT
    OF SCOPE — callers skip it silently.
    """
    try:
        return candidate.is_relative_to(search_root)
    except Exception:
        return False


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
    safe_path = _sanitise(nb_display_path)
    safe_line = _sanitise(first_line)
    return f"{safe_path}:{cell_index}: {safe_line}"


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

    # Git-root index dirs mirror the repo layout, so per-notebook .json files
    # live in subdirectories — the scan must be recursive or nested indexes
    # newer than symbols.json never invalidate it.
    try:
        for dirpath, _dirnames, filenames in os.walk(str(index_dir_path),
                                                     followlinks=False):
            for name in filenames:
                if name.endswith(".json") and name != "symbols.json":
                    try:
                        st = os.stat(os.path.join(dirpath, name))
                        if st.st_mtime > gen_ts:
                            return False
                    except OSError:
                        pass
    except OSError:
        return False

    return True


def _validate_location_string(loc: str, index_base: Path) -> tuple[str, int] | None:
    """
    Validate a location string 'notebook_path:cell_index' from symbols.json.
    index_base is the parent of the .nb_index dir containing symbols.json.
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

    candidate = _validate_notebook_path(nb_part, index_base)
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


def _display_path(candidate: Path, search_root: Path) -> str:
    try:
        return str(candidate.relative_to(search_root))
    except ValueError:
        return str(candidate)


def _claim_notebook(candidate: Path, index_dir_str: str, dedup: dict,
                    search_root: Path) -> bool:
    """
    Cross-index dedup by RESOLVED notebook path. A notebook reachable via
    two index files (legacy per-dir .nb_index + git-root .nb_index) must not
    print every match twice. The first index file to claim a notebook wins
    (the upward/git-root index is yielded first by _walk_for_index_dirs —
    that is the preferred one); later index entries for an already-seen
    notebook are skipped with a one-line [DUP] stderr note.
    """
    claimed = dedup["claimed"]
    owner = claimed.get(candidate)
    if owner is None:
        claimed[candidate] = index_dir_str
        return True
    if owner == index_dir_str:
        return True  # same index source (e.g. several symbols.json locations)
    if candidate not in dedup["warned"]:
        dedup["warned"].add(candidate)
        display = _display_path(candidate, search_root)
        print(f"[DUP] {_sanitise(display)}: shadowed by another index, skipped",
              file=sys.stderr)
    return False


def _open_notebook(candidate: Path, display: str):
    """
    Open a notebook JSON with the MAX_FILE_SIZE guard.
    Returns the parsed dict, or None if missing/oversized/corrupt.
    """
    try:
        file_size = os.path.getsize(candidate)
    except OSError:
        return None
    if file_size > MAX_FILE_SIZE:
        print(f"[WARN] {_sanitise(display)}: exceeds 100 MB limit, skipped",
              file=sys.stderr)
        return None
    try:
        with open(candidate, encoding="utf-8-sig", errors="replace") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return None


def _iter_output_lines(cell: dict):
    """
    Yield text lines from a code cell's outputs (--in-outputs): stream
    text, data['text/plain'], and error tracebacks. list/str coerced the
    same way as cell sources.
    """
    outputs = cell.get("outputs")
    if not isinstance(outputs, list):
        return
    for out in outputs:
        if not isinstance(out, dict):
            continue
        parts = []
        if "text" in out:
            parts.append(_coerce_source(out.get("text")))
        data = out.get("data")
        if isinstance(data, dict) and "text/plain" in data:
            parts.append(_coerce_source(data.get("text/plain")))
        tb = out.get("traceback")
        if isinstance(tb, list):
            parts.append("\n".join(str(t) for t in tb))
        elif isinstance(tb, str):
            parts.append(tb)
        for part in parts:
            yield from part.splitlines()


def _search_notebook_cells(
    nb: dict,
    index_cells: dict,
    q_lower: str,
    type_filter: str | None,
    section_filter: str | None,
    in_outputs: bool,
):
    """
    Yield (cell_index, shown_text) keyword matches in one loaded notebook.
    Used for both indexed and unindexed notebooks (index_cells may be {}).
    """
    cells_raw = nb.get("cells", [])
    if not isinstance(cells_raw, list):
        return
    for i, cell in enumerate(cells_raw):
        if not isinstance(cell, dict):
            continue
        ctype = cell.get("cell_type", "unknown")

        # Type filter
        if type_filter is not None and ctype != type_filter:
            continue

        # Section filter uses index metadata; without it the cell is skipped
        if section_filter is not None:
            idx_cell = index_cells.get(i)
            if idx_cell is None:
                continue
            if not _cell_passes_section(idx_cell, section_filter):
                continue

        source_str = _coerce_source(cell.get("source", []))

        # Case-insensitive keyword search over source
        if q_lower in source_str.lower():
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
            yield i, first_line

        # --in-outputs: also match output text of code cells
        if in_outputs and ctype == "code":
            for line in _iter_output_lines(cell):
                if q_lower in line.lower():
                    yield i, "[output] " + line.strip()[:120]


def _search_keyword(
    query: str,
    search_root: Path,
    type_filter: str | None,
    section_filter: str | None,
    limit: int | None,
    in_outputs: bool = False,
) -> int:
    """
    Keyword search: open each .ipynb file and scan cell source text
    (and, with --in-outputs, output text of code cells).
    Uses index to locate notebooks and get metadata; unindexed notebooks
    found by the directory walk are searched directly.
    Returns number of results printed.
    """
    q_lower = query.lower()
    count = 0

    # Collect all indexed notebook paths and their index data
    # Also track all .ipynb files to detect unindexed ones
    indexed_nb_paths = set()  # resolved Path objects
    dedup = {"claimed": {}, "warned": set()}

    # First pass: find all indexed notebooks
    indexed_notebooks = []  # list of (nb_candidate_path, index_data, is_stale)

    for index_dir_str, filenames in _walk_for_index_dirs(search_root):
        index_dir_path = Path(index_dir_str)
        index_base = index_dir_path.parent
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

            candidate = _validate_notebook_path(np_str, index_base)
            if candidate is None:
                print(f"[WARN] invalid or unsafe notebook_path in {json_file}",
                      file=sys.stderr)
                continue

            if not _in_scope(candidate, search_root):
                continue  # safe, but outside the requested search scope

            indexed_nb_paths.add(candidate)

            if not _claim_notebook(candidate, index_dir_str, dedup, search_root):
                continue  # duplicate entry from a second index file

            # Check staleness
            stale = _check_staleness(index_data, candidate)
            if stale:
                display = _display_path(candidate, search_root)
                print(f"[STALE] {display}", file=sys.stderr)

            indexed_notebooks.append((candidate, index_data, stale))

    # Second pass: detect unindexed notebooks — keyword mode opens .ipynb
    # files anyway, so these are searched directly (with a stderr note).
    unindexed_notebooks = []
    for nb_path in _walk_for_ipynb(search_root):
        try:
            resolved = nb_path.resolve()
        except OSError:
            resolved = nb_path
        if resolved not in indexed_nb_paths:
            display = _display_path(nb_path, search_root)
            print(f"[UNINDEXED] {_sanitise(display)} — searched directly",
                  file=sys.stderr)
            unindexed_notebooks.append(resolved)

    # Third pass: search indexed notebooks
    for candidate, index_data, stale in indexed_notebooks:
        display = _display_path(candidate, search_root)
        nb = _open_notebook(candidate, display)
        if nb is None:
            continue

        index_cells = {}
        if index_data:
            try:
                index_cells = {c["i"]: c for c in index_data.get("cells", []) if isinstance(c, dict) and "i" in c}
            except (KeyError, TypeError):
                print(f"[WARN] {display}: malformed index, skipped", file=sys.stderr)
                continue

        for i, shown in _search_notebook_cells(nb, index_cells, q_lower,
                                               type_filter, section_filter,
                                               in_outputs):
            print(_format_result(display, i, shown))
            count += 1
            if limit is not None and count >= limit:
                return count

    # Fourth pass: search unindexed notebooks directly (no index metadata —
    # --section can never match, first_line falls back to the source).
    for candidate in unindexed_notebooks:
        display = _display_path(candidate, search_root)
        nb = _open_notebook(candidate, display)
        if nb is None:
            continue
        for i, shown in _search_notebook_cells(nb, {}, q_lower,
                                               type_filter, section_filter,
                                               in_outputs):
            print(_format_result(display, i, shown))
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
    dedup = {"claimed": {}, "warned": set()}
    stale_checked = set()  # notebooks whose staleness was already reported

    for index_dir_str, filenames in _walk_for_index_dirs(search_root):
        index_dir_path = Path(index_dir_str)
        index_base = index_dir_path.parent

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
                        result = _validate_location_string(loc, index_base)
                        if result is None:
                            continue
                        nb_path_str, cell_idx = result
                        # For display purposes, use the nb_path_str relative to search_root
                        candidate = _validate_notebook_path(nb_path_str, index_base)
                        if candidate is None:
                            continue
                        if not _in_scope(candidate, search_root):
                            continue  # safe, but outside the requested search scope
                        display = _display_path(candidate, search_root)
                        if not _claim_notebook(candidate, index_dir_str,
                                               dedup, search_root):
                            continue
                        # We need cell metadata — load per-notebook index
                        # to apply type/section filters and get first_line
                        # Find per-notebook index file
                        nb_index_file, _, _ = _index_file_path(candidate)
                        first_line = f"cell {cell_idx}"
                        cell_type = "code"
                        nb_idx = None
                        try:
                            with open(nb_index_file, encoding="utf-8") as nf:
                                nb_idx = json.load(nf)
                        except (json.JSONDecodeError, OSError):
                            nb_idx = None
                        if nb_idx is None:
                            # Cannot apply requested filters without the
                            # per-notebook index — skip rather than emit an
                            # unfiltered result.
                            if type_filter or section_filter:
                                print(f"[WARN] {_sanitise(display)}: cannot apply "
                                      "--type/--section filter (index unreadable), "
                                      "result skipped", file=sys.stderr)
                                continue
                        else:
                            # Per-notebook staleness (§12.6): warn, return anyway
                            if candidate not in stale_checked:
                                stale_checked.add(candidate)
                                if _check_staleness(nb_idx, candidate):
                                    print(f"[STALE] {_sanitise(display)}",
                                          file=sys.stderr)
                            cells_list = nb_idx.get("cells", [])
                            for c in cells_list:
                                if not isinstance(c, dict) or "i" not in c:
                                    continue
                                if c.get("i") == cell_idx:
                                    first_line = c.get("first_line", first_line)
                                    cell_type = c.get("type", cell_type)
                                    if type_filter and cell_type != type_filter:
                                        first_line = None
                                    if section_filter and not _cell_passes_section(c, section_filter):
                                        first_line = None
                                    break

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
            candidate = _validate_notebook_path(np_str, index_base)
            if candidate is None:
                print(f"[WARN] invalid or unsafe notebook_path in {json_file}", file=sys.stderr)
                continue

            if not _in_scope(candidate, search_root):
                continue  # safe, but outside the requested search scope

            if not _claim_notebook(candidate, index_dir_str, dedup, search_root):
                continue

            display = _display_path(candidate, search_root)

            for cell in index_data.get("cells", []):
                if not isinstance(cell, dict) or "i" not in cell:
                    continue
                if query not in cell.get("symbols_defined", []):
                    continue

                ctype = cell.get("type", "unknown")
                if type_filter and ctype != type_filter:
                    continue
                if section_filter and not _cell_passes_section(cell, section_filter):
                    continue

                # Per-notebook staleness (§12.6): warn on first match for
                # this notebook, return the result anyway.
                if candidate not in stale_checked:
                    stale_checked.add(candidate)
                    if _check_staleness(index_data, candidate):
                        print(f"[STALE] {_sanitise(display)}", file=sys.stderr)

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
    dedup = {"claimed": {}, "warned": set()}
    stale_checked = set()  # notebooks whose staleness was already reported

    for index_dir_str, filenames in _walk_for_index_dirs(search_root):
        index_dir_path = Path(index_dir_str)
        index_base = index_dir_path.parent

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
                            result = _validate_location_string(loc, index_base)
                            if result is None:
                                continue
                            nb_path_str, cell_idx = result
                            candidate = _validate_notebook_path(nb_path_str, index_base)
                            if candidate is None:
                                continue
                            if not _in_scope(candidate, search_root):
                                continue  # safe, but out of scope
                            display = _display_path(candidate, search_root)
                            if not _claim_notebook(candidate, index_dir_str,
                                                   dedup, search_root):
                                continue
                            # Load per-notebook index for filters and first_line
                            nb_index_file, _, _ = _index_file_path(candidate)
                            first_line = f"cell {cell_idx}"
                            nb_idx = None
                            try:
                                with open(nb_index_file, encoding="utf-8") as nf:
                                    nb_idx = json.load(nf)
                            except (json.JSONDecodeError, OSError):
                                nb_idx = None
                            if nb_idx is None:
                                # Cannot apply requested filters without the
                                # per-notebook index — skip rather than emit
                                # an unfiltered result.
                                if type_filter or section_filter:
                                    print(f"[WARN] {_sanitise(display)}: cannot apply "
                                          "--type/--section filter (index unreadable), "
                                          "result skipped", file=sys.stderr)
                                    continue
                            else:
                                # Per-notebook staleness (§12.6): warn, return anyway
                                if candidate not in stale_checked:
                                    stale_checked.add(candidate)
                                    if _check_staleness(nb_idx, candidate):
                                        print(f"[STALE] {_sanitise(display)}",
                                              file=sys.stderr)
                                for c in nb_idx.get("cells", []):
                                    if not isinstance(c, dict) or "i" not in c:
                                        continue
                                    if c.get("i") == cell_idx:
                                        first_line = c.get("first_line", first_line)
                                        cell_type = c.get("type", "code")
                                        if type_filter and cell_type != type_filter:
                                            first_line = None
                                        if section_filter and not _cell_passes_section(c, section_filter):
                                            first_line = None
                                        break
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
            candidate = _validate_notebook_path(np_str, index_base)
            if candidate is None:
                print(f"[WARN] invalid or unsafe notebook_path in {json_file}", file=sys.stderr)
                continue

            if not _in_scope(candidate, search_root):
                continue  # safe, but outside the requested search scope

            if not _claim_notebook(candidate, index_dir_str, dedup, search_root):
                continue

            display = _display_path(candidate, search_root)

            for cell in index_data.get("cells", []):
                if not isinstance(cell, dict) or "i" not in cell:
                    continue
                imported = cell.get("symbols_imported", [])
                if not any(_import_matches(k, query) for k in imported):
                    continue

                ctype = cell.get("type", "unknown")
                if type_filter and ctype != type_filter:
                    continue
                if section_filter and not _cell_passes_section(cell, section_filter):
                    continue

                # Per-notebook staleness (§12.6): warn on first match for
                # this notebook, return the result anyway.
                if candidate not in stale_checked:
                    stale_checked.add(candidate)
                    if _check_staleness(index_data, candidate):
                        print(f"[STALE] {_sanitise(display)}", file=sys.stderr)

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
    def validate_limit(value_str):
        try:
            value = int(value_str)
            if value < 1:
                raise argparse.ArgumentTypeError(f"--limit must be >= 1, got {value}")
            return value
        except ValueError:
            raise argparse.ArgumentTypeError(f"--limit must be an integer, got {value_str}")

    parser.add_argument("--limit", type=validate_limit, default=None,
                        help="Stop after N results")
    parser.add_argument("--in-outputs", dest="in_outputs", action="store_true",
                        help="Keyword mode only: also match output text "
                             "(stream text, text/plain, tracebacks) of code cells")
    parser.add_argument("query", help="Search query")
    parser.add_argument("search_root", help="Directory to search")

    # Custom error handling for exit code 2
    try:
        args = parser.parse_args()
        if args.in_outputs and (args.symbol or args.import_mode):
            parser.error("--in-outputs is only valid in keyword mode "
                         "(cannot be combined with --symbol/--import)")
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
        count = _search_keyword(query, search_root, type_filter, section_filter,
                                limit, in_outputs=args.in_outputs)

    sys.exit(0 if count > 0 else 1)


if __name__ == "__main__":
    main()
