#!/usr/bin/env python3
"""
nb-index.py — build/update a persistent JSON index for a Jupyter notebook.

Usage:
  python3 nb-index.py <notebook.ipynb> [--force]

Exit codes:
  0  Index written, already fresh, or best-effort success.
  1  Unrecoverable error.

stdout: always silent.
stderr: status messages.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Portable lock helper — verbatim copy shared with nb-write.py (no shared
# import between standalone scripts; keep both copies in sync).
# Tries fcntl (POSIX), falls back to msvcrt (Windows); no-op only if neither
# is available.
# ---------------------------------------------------------------------------
try:
    import fcntl
    _LOCK_BACKEND = "fcntl"
except ImportError:
    try:
        import msvcrt
        _LOCK_BACKEND = "msvcrt"
    except ImportError:
        _LOCK_BACKEND = None  # no locking primitive available


def _lock_file(f, blocking=True):
    """
    Acquire an exclusive lock on open file object *f*.

    Returns True on success; False if non-blocking and the lock is busy.
    Blocking acquisition failures raise OSError. With no backend, no-op True.
    """
    if _LOCK_BACKEND == "fcntl":
        flags = fcntl.LOCK_EX if blocking else (fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            fcntl.flock(f, flags)
            return True
        except OSError:
            if blocking:
                raise
            return False
    elif _LOCK_BACKEND == "msvcrt":
        # msvcrt.locking locks a byte range at the current file position.
        # LK_LOCK retries for ~10s (blocking-ish); LK_NBLCK fails immediately.
        mode = msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK
        try:
            f.seek(0)
            msvcrt.locking(f.fileno(), mode, 1)
            return True
        except OSError:
            if blocking:
                raise
            return False
    return True


def _unlock_file(f):
    """Release a lock taken with _lock_file (best-effort)."""
    if _LOCK_BACKEND == "fcntl":
        try:
            fcntl.flock(f, fcntl.LOCK_UN)
        except OSError:
            pass
    elif _LOCK_BACKEND == "msvcrt":
        try:
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass


def _lock_file_timeout(f, timeout=10.0, interval=0.1):
    """
    Blocking-with-timeout exclusive lock: poll the non-blocking acquire in a
    short-sleep loop (alarm-free, portable). Returns True if acquired within
    *timeout* seconds, False otherwise.
    """
    deadline = time.monotonic() + timeout
    while True:
        try:
            if _lock_file(f, blocking=False):
                return True
        except OSError:
            return False
        if time.monotonic() >= deadline:
            return False
        time.sleep(interval)

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8')

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_FILE_SIZE       = 100 * 1024 * 1024   # 100 MB
MAX_FIRST_LINE      = 120
MAX_OUTPUT_TEXT     = 4096
MAX_OUTPUT_PRELIM   = 16 * 1024           # 16 KB preliminary cap (A4)
MAX_SYMBOL_LEN      = 256
MAX_SYMBOLS_PER_CELL = 500
MAX_LINE_LEN        = 500                 # ReDoS protection

# ---------------------------------------------------------------------------
# ANSI sanitisation  (same patterns as nb-read.py)
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
# Symbol-extraction regexes (compiled once at module level)
# ---------------------------------------------------------------------------

# Python
# DEF_RE: 'def' and 'async def' at column 0.
DEF_RE    = re.compile(r'^(?:async\s+)?def\s+(\w+)\s*\(', re.MULTILINE)
CLASS_RE  = re.compile(r'^class\s+(\w+)\s*[:\(]', re.MULTILINE)
# ASSIGN_RE: optional annotation tolerates dots, brackets, quotes, pipes and
# spaces (e.g. 'x: np.ndarray = ...', 'y: "Foo|None" = ...'); the {0,200}
# bound on a single character class keeps it ReDoS-safe.
ASSIGN_RE = re.compile(r'^(\w+)\s*(?::\s*[\w\[\]., \'"|]{0,200})?\s*=(?!=)', re.MULTILINE)
# TUPLE_ASSIGN_RE: conservative tuple assignment at column 0 — a comma-
# separated list of two or more simple \w+ names followed by '='. Starred,
# attribute and subscript targets deliberately do not match.
TUPLE_ASSIGN_RE = re.compile(r'^(\w+(?:\s*,\s*\w+)+)\s*=(?!=)', re.MULTILINE)
# IMPORT_RE: captures the full comma-separated module list of an 'import'
# statement, including 'as alias' parts; split/stripped in _extract_symbols
# (the imports index is by MODULE name, so 'import x as y' records 'x').
IMPORT_RE = re.compile(r'^import\s+([\w.]+(?:\s+as\s+\w+)?(?:[ \t]*,[ \t]*[\w.]+(?:\s+as\s+\w+)?)*)', re.MULTILINE)
FROM_RE   = re.compile(r'^from\s+([\w.]+)\s+import', re.MULTILINE)

# Julia
JULIA_FUNC_RE   = re.compile(r'^function\s+([\w!]+)\s*\(', re.MULTILINE)
JULIA_SHORT_RE  = re.compile(r'^([\w!]+)\s*\([^)]{0,200}\)\s*=', re.MULTILINE)
JULIA_STRUCT_RE = re.compile(r'^(?:mutable\s+)?struct\s+(\w+)', re.MULTILINE)
JULIA_ASSIGN_RE = re.compile(r'^(\w+)\s*=(?!=)', re.MULTILINE)
JULIA_USING_RE  = re.compile(r'\busing\s+([\w.]+(?:\s*,\s*[\w.]+)*)', re.MULTILINE)
JULIA_IMPORT_RE = re.compile(r'\bimport\s+([\w.]+)', re.MULTILINE)

# R
R_FUNC_RE = re.compile(r'^(\w+)\s*(?:<-|=)\s*function\s*\(', re.MULTILINE)
R_LIB_RE  = re.compile(r'(?:library|require)\s*\(\s*["\']?([\w.]+)', re.MULTILINE)

# Heading detection
HEADING_RE = re.compile(r'^(#{1,6})\s+(.+)')


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _die(msg: str) -> None:
    print(f"Error: {msg}", file=sys.stderr)
    sys.exit(1)


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _filter_long_lines(src: str) -> str:
    """Return src with lines > MAX_LINE_LEN truncated to their first
    MAX_LINE_LEN characters (symbols appear at the start of a definition
    line; the bounded slice keeps ReDoS safety). Line structure preserved."""
    parts = []
    for line in src.splitlines(keepends=True):
        if len(line) > MAX_LINE_LEN:
            nl = '\n' if line.endswith('\n') else ''
            parts.append(line[:MAX_LINE_LEN].rstrip('\r\n') + nl)
        else:
            parts.append(line)
    return ''.join(parts)


def _cap_symbols(names: list) -> list:
    return [n for n in names if len(n) <= MAX_SYMBOL_LEN][:MAX_SYMBOLS_PER_CELL]


# ---------------------------------------------------------------------------
# §1 — Index path resolution
# ---------------------------------------------------------------------------

def _find_index_dir(nb_path: Path) -> tuple[Path, Path | None]:
    """
    Return (index_dir, git_root_or_None).

    CANONICAL COPY — nb-read.py and nb-search.py carry verbatim copies of this
    function (no shared import between standalone scripts); when changing it
    here, sync the copies. Keep it self-contained.

    Walk upward from the notebook's directory looking for a .git entry that is
    either a real (non-symlink) directory, or a regular (non-symlink) file
    whose first bytes start with 'gitdir:' (git worktrees and submodules).
    The git root for index purposes is the directory CONTAINING that .git
    entry — the 'gitdir:' pointer is never followed; the index belongs with
    the working tree. A symlinked .git is always rejected (security stance).
    Stops after 20 levels or if st_dev changes (filesystem boundary); when
    that forces the per-directory fallback, a one-line [note] goes to stderr.
    """
    nb_abs = nb_path  # already resolved by caller
    cur = nb_abs.parent
    try:
        cur_dev = os.stat(cur).st_dev
    except OSError:
        return (cur / ".nb_index", None)

    stop_reason = None       # "depth" | "boundary" | None (root / stat error)
    saw_symlink_git = False  # a symlinked .git was rejected during the walk

    for level in range(20):
        git_candidate = cur / ".git"
        try:
            if git_candidate.is_symlink():
                # Never accept a symlinked .git (dir or file).
                saw_symlink_git = True
            elif git_candidate.is_dir():
                return (cur / ".nb_index", cur)
            elif git_candidate.is_file():
                # Worktree / submodule: .git is a regular file containing
                # 'gitdir: <path>'. Read at most 4096 bytes; tolerate read
                # errors by ignoring the candidate.
                try:
                    with open(git_candidate, "rb") as gf:
                        head = gf.read(4096)
                except OSError:
                    head = b""
                if head.startswith(b"gitdir:"):
                    return (cur / ".nb_index", cur)
        except OSError:
            pass  # unreadable candidate — ignore and keep walking

        parent = cur.parent
        if parent == cur:
            break  # reached filesystem root — normal no-git case, no note
        try:
            parent_dev = os.stat(parent).st_dev
        except OSError:
            break
        if parent_dev != cur_dev:
            stop_reason = "boundary"  # filesystem boundary
            break
        cur_dev = parent_dev
        cur = parent
    else:
        stop_reason = "depth"  # 20-level cap exhausted

    # Fallback: use notebook's own directory
    index_dir = nb_abs.parent / ".nb_index"
    suffix = " (a symlinked .git was rejected during the walk)" if saw_symlink_git else ""
    if stop_reason == "depth":
        print(f"[note] no git root found within 20 levels — "
              f"using per-directory index at {index_dir}{suffix}", file=sys.stderr)
    elif stop_reason == "boundary":
        print(f"[note] no git root found before filesystem boundary — "
              f"using per-directory index at {index_dir}{suffix}", file=sys.stderr)
    elif saw_symlink_git:
        print(f"[note] symlinked .git rejected — "
              f"using per-directory index at {index_dir}", file=sys.stderr)
    return (index_dir, None)


def _index_file_path(nb_path: Path) -> tuple[Path, Path, Path | None]:
    """
    Return (index_file_path, index_dir, git_root_or_None).

    nb_path must already be resolved.
    """
    index_dir, git_root = _find_index_dir(nb_path)

    if git_root is not None:
        # Relative path from git root
        try:
            rel = nb_path.relative_to(git_root)
        except ValueError:
            # Notebook is outside git root somehow — fall back
            index_dir = nb_path.parent / ".nb_index"
            git_root = None
            index_file = index_dir / (nb_path.name + ".json")
        else:
            # Forward slashes, even on Windows
            index_file = index_dir / (rel.as_posix() + ".json")
    else:
        index_file = index_dir / (nb_path.name + ".json")

    # Containment assertion
    try:
        index_file.resolve().relative_to(index_dir.resolve())
    except ValueError:
        _die(f"Containment assertion failed: index path {index_file} "
             f"is not under {index_dir}. Aborting.")

    return index_file, index_dir, git_root


# ---------------------------------------------------------------------------
# §2 — .gitignore management
# ---------------------------------------------------------------------------

def _update_gitignore(index_dir: Path) -> None:
    """Append '.nb_index/' and '*.nblock' to .gitignore at the same level as index_dir.

    The read-modify-write is serialised against concurrent indexers via a
    blocking-with-timeout (~5 s) exclusive lock on .nb_index/gitignore.nblock
    (same portable helper as symbols.nblock; never unlinked). On timeout the
    update is skipped with a [warn] — the operation is idempotent, so a later
    run adds any missing entries.
    """
    gitignore = index_dir.parent / ".gitignore"
    entries = [".nb_index/", "*.nblock"]

    if gitignore.is_symlink():
        print(f"[warn] .gitignore is a symlink — skipping gitignore update",
              file=sys.stderr)
        return

    lock_fd = None
    if _LOCK_BACKEND is not None:
        try:
            lock_fd = open(index_dir / "gitignore.nblock", "a")
        except OSError:
            lock_fd = None  # cannot create lock file — proceed unlocked (best effort)
        if lock_fd is not None and not _lock_file_timeout(lock_fd, timeout=5.0):
            print("[warn] gitignore lock busy — skipping gitignore update",
                  file=sys.stderr)
            lock_fd.close()
            return

    try:
        if gitignore.exists():
            content = gitignore.read_text(encoding="utf-8")
            existing_lines = set(line.strip() for line in content.splitlines())
            # Check which entries are missing
            to_add = [e for e in entries if e not in existing_lines]
            if not to_add:
                return  # all entries already present
            # Append missing entries
            if content and not content.endswith("\n"):
                content += "\n"
            for entry in to_add:
                content += entry + "\n"
        else:
            content = "\n".join(entries) + "\n"
        gitignore.write_text(content, encoding="utf-8")
    except OSError as e:
        print(f"[warn] could not update .gitignore: {e}", file=sys.stderr)
    finally:
        if lock_fd is not None:
            # The lock file is deliberately NOT unlinked (unlink-after-release
            # is an inode race; *.nblock is gitignored).
            _unlock_file(lock_fd)
            lock_fd.close()


# ---------------------------------------------------------------------------
# §3 — Staleness detection
# ---------------------------------------------------------------------------

def _index_is_stale(index_file: Path, nb_path: Path, force: bool) -> tuple[bool, float, int]:
    """
    Return (is_stale, current_mtime, current_size).

    Implements the short-circuit: only opens the notebook file for SHA-256 if
    mtime or size changed.
    """
    if force:
        try:
            mtime = os.path.getmtime(nb_path)
            size  = os.path.getsize(nb_path)
        except OSError:
            mtime, size = 0.0, 0
        return True, mtime, size

    # Check 1: index missing
    if not index_file.exists():
        try:
            mtime = os.path.getmtime(nb_path)
            size  = os.path.getsize(nb_path)
        except OSError:
            mtime, size = 0.0, 0
        return True, mtime, size

    # Read existing index for staleness signals
    try:
        existing = json.loads(index_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        try:
            mtime = os.path.getmtime(nb_path)
            size  = os.path.getsize(nb_path)
        except OSError:
            mtime, size = 0.0, 0
        return True, mtime, size

    stored_mtime = existing.get("notebook_mtime", None)
    stored_size  = existing.get("notebook_size",  None)
    stored_hash  = existing.get("nb_content_hash", None)

    try:
        mtime = os.path.getmtime(nb_path)
        size  = os.path.getsize(nb_path)
    except OSError:
        return True, 0.0, 0

    # Check 2: mtime mismatch → stale (no file open needed)
    if stored_mtime != mtime:
        return True, mtime, size

    # Check 3: size mismatch → stale (no file open needed)
    if stored_size != size:
        return True, mtime, size

    if stored_hash is None:
        return True, mtime, size

    # Step 4: hash check — open the file only if needed
    # This is the ONLY step that opens the notebook.
    try:
        raw = nb_path.read_bytes()
        current_hash = hashlib.sha256(raw).hexdigest()[:16]
        if current_hash != stored_hash:
            return True, mtime, size
    except OSError:
        # Cannot read file for hashing — fail safe: an unverifiable index
        # must be treated as stale, not silently trusted.
        return True, mtime, size

    # All match → fresh
    return False, mtime, size


# ---------------------------------------------------------------------------
# §4 / Schema — first_line extraction
# ---------------------------------------------------------------------------

def _first_line(source_str: str) -> str:
    """Return first non-empty stripped source line, max 120 chars, ANSI-sanitised."""
    if not source_str:
        return "(empty)"
    for line in source_str.splitlines():
        stripped = line.strip()
        if stripped:
            sanitised = _sanitise(stripped)
            return sanitised[:MAX_FIRST_LINE]
    return "(empty)"


# ---------------------------------------------------------------------------
# §5 — Section extraction (O(C) single-pass with heading stack)
# ---------------------------------------------------------------------------

def _extract_sections_and_paths(cells_raw: list) -> list[tuple[str | None, list[str], dict | None]]:
    """
    Single-pass section extraction.

    Returns a list of (section_name, section_path, heading_info) per cell.
    heading_info is {"level": int, "text": str} for heading cells, else None.
    """
    # stack entries: (level, heading_text)
    stack: list[tuple[int, str]] = []
    results = []

    for cell in cells_raw:
        ctype = cell.get("cell_type", "")
        source = cell.get("source", [])
        if isinstance(source, list):
            source_str = "".join(str(s) for s in source)
        else:
            source_str = str(source) if source else ""

        heading_info = None
        is_heading   = False

        if ctype == "markdown":
            # Detect heading from first non-empty line
            for line in source_str.splitlines():
                line_stripped = line.strip()
                if line_stripped:
                    m = HEADING_RE.match(line_stripped)
                    if m:
                        level = len(m.group(1))
                        heading_text = m.group(2).strip()
                        heading_info = {"level": level, "text": heading_text}
                        is_heading = True
                    break

        if is_heading:
            level = heading_info["level"]
            text  = heading_info["text"]
            # Pop all stack entries whose level >= current level
            while stack and stack[-1][0] >= level:
                stack.pop()
            # The heading cell itself has section=null, section_path=[]
            # (it opens the section, is not inside it)
            cur_path = [s[1] for s in stack]
            results.append((None, cur_path[:], heading_info))
            # Push new heading
            stack.append((level, text))
        else:
            # Non-heading cell: assign innermost section
            if stack:
                section_name = stack[-1][1]
                section_path = [s[1] for s in stack]
            else:
                section_name = None
                section_path = []
            results.append((section_name, section_path, None))

    return results


# ---------------------------------------------------------------------------
# §6 — Symbol extraction
# ---------------------------------------------------------------------------

def _extract_symbols(source_str: str, lang: str) -> tuple[list, list, bool]:
    """
    Extract defined and imported symbols from source.
    Returns (symbols_defined, symbols_imported, symbols_extracted).
    """
    lang_lower = lang.lower() if lang else ""

    filtered = _filter_long_lines(source_str)

    if "python" in lang_lower:
        defined  = []
        imported = []

        defined += DEF_RE.findall(filtered)
        defined += CLASS_RE.findall(filtered)
        defined += ASSIGN_RE.findall(filtered)
        # Tuple assignment 'a, b = ...': capture each simple name
        for match in TUPLE_ASSIGN_RE.findall(filtered):
            for name in match.split(','):
                name = name.strip()
                if name:
                    defined.append(name)
        # Post-filter: remove "type" soft-keyword
        defined = [n for n in defined if n != "type"]

        # 'import a, b as c, d': record each MODULE name (aliases dropped —
        # the imports index is by module).
        for match in IMPORT_RE.findall(filtered):
            for part in match.split(','):
                words = part.split()
                if words:
                    imported.append(words[0])
        imported += FROM_RE.findall(filtered)

        return (_cap_symbols(defined), _cap_symbols(imported), True)

    elif "julia" in lang_lower:
        defined  = []
        imported = []

        defined += JULIA_FUNC_RE.findall(filtered)
        defined += JULIA_SHORT_RE.findall(filtered)
        defined += JULIA_STRUCT_RE.findall(filtered)
        defined += JULIA_ASSIGN_RE.findall(filtered)

        # JULIA_USING_RE: split comma-separated module names
        for match in JULIA_USING_RE.findall(filtered):
            for mod in re.split(r'\s*,\s*', match):
                mod = mod.strip()
                if mod:
                    imported.append(mod)

        imported += JULIA_IMPORT_RE.findall(filtered)

        return (_cap_symbols(defined), _cap_symbols(imported), True)

    elif lang_lower == "r":
        defined  = R_FUNC_RE.findall(filtered)
        imported = R_LIB_RE.findall(filtered)
        return (_cap_symbols(defined), _cap_symbols(imported), True)

    else:
        return ([], [], False)


# ---------------------------------------------------------------------------
# §7 — Output processing
# ---------------------------------------------------------------------------

def _coerce_text(v) -> str:
    """Coerce a text field (str or list-of-str) to a single string."""
    if isinstance(v, list):
        return "".join(str(x) for x in v)
    return str(v) if v is not None else ""


def _replace_lone_surrogates(s: str) -> str:
    """Replace lone surrogate code points with U+FFFD."""
    return s.encode('utf-16', 'surrogatepass').decode('utf-16', 'replace')


def _process_outputs(outputs: list) -> tuple[bool, list, str | None, bool | None]:
    """
    Process cell outputs per A4 pipeline.
    Returns (has_output, output_types, output_text_or_None, output_truncated_or_None).
    """
    if not outputs:
        return False, [], None, None

    text_parts = []
    output_types_ordered = []
    seen_types = set()
    has_any_output = False

    for out in outputs:
        has_any_output = True
        otype = out.get("output_type", "")

        # Deduplicate output_types in order of first appearance
        if otype not in seen_types:
            output_types_ordered.append(otype)
            seen_types.add(otype)

        # Extract text content
        if otype == "stream":
            text_val = out.get("text", "")
            text_parts.append(_coerce_text(text_val))

        elif otype in ("execute_result", "display_data"):
            data_dict = out.get("data", {})
            if "text/plain" in data_dict:
                text_parts.append(_coerce_text(data_dict["text/plain"]))
            else:
                # Check for binary MIME types — add them to output_types
                for mime in data_dict:
                    if mime not in seen_types:
                        output_types_ordered.append(mime)
                        seen_types.add(mime)

        elif otype == "error":
            tb = out.get("traceback", [])
            text_parts.append(_coerce_text(tb))

    # Check if we have any text at all
    if not text_parts:
        # All outputs were binary
        # But we may still need to add mime types for binary outputs
        for out in outputs:
            otype = out.get("output_type", "")
            if otype in ("execute_result", "display_data"):
                data_dict = out.get("data", {})
                for mime in data_dict:
                    if mime not in seen_types:
                        output_types_ordered.append(mime)
                        seen_types.add(mime)
        return True, output_types_ordered, None, None

    # Also add binary mime types that are alongside text outputs
    for out in outputs:
        otype = out.get("output_type", "")
        if otype in ("execute_result", "display_data"):
            data_dict = out.get("data", {})
            for mime in data_dict:
                if mime != "text/plain" and mime not in seen_types:
                    output_types_ordered.append(mime)
                    seen_types.add(mime)

    # Step 1: Concatenate
    combined = "".join(text_parts)

    # Preliminary 16 KB cap (A4 memory efficiency)
    if len(combined.encode("utf-8", errors="surrogatepass")) > MAX_OUTPUT_PRELIM:
        # Hard-cap at 16KB bytes
        b = combined.encode("utf-8", errors="surrogatepass")[:MAX_OUTPUT_PRELIM]
        combined = b.decode("utf-8", errors="replace")

    # Step 2: Strip null bytes
    combined = combined.replace("\x00", "")

    # Step 3: Replace lone surrogates
    combined = _replace_lone_surrogates(combined)

    # Step 4: ANSI-sanitise
    combined = _ANSI_RE.sub('', combined)
    # Note: we keep \n (newlines), only strip ANSI and C0 controls (except \n)
    # The _CTRL_RE from nb-read.py strips ALL control chars including \n.
    # For output_text we want to preserve \n but strip other control chars.
    combined = re.sub(r'[\x00-\x09\x0b-\x1f\x7f]', '', combined)

    # Step 5: 4096-byte UTF-8 cap
    encoded = combined.encode("utf-8")
    if len(encoded) <= MAX_OUTPUT_TEXT:
        return True, output_types_ordered, combined, False
    else:
        # Try to truncate at last newline before byte 4096
        chunk = encoded[:MAX_OUTPUT_TEXT]
        last_nl = chunk.rfind(b'\n')
        if last_nl > 0:
            truncated = chunk[:last_nl + 1].decode("utf-8", errors="replace")
            return True, output_types_ordered, truncated, True
        else:
            # No complete line fits — hard truncate
            text4096 = chunk.decode("utf-8", errors="replace")
            suffix = "\n[truncated mid-line]"
            return True, output_types_ordered, text4096 + suffix, True


# ---------------------------------------------------------------------------
# §13 — symbols.json management
# ---------------------------------------------------------------------------

def _compute_notebook_key(nb_path: Path, git_root: Path | None) -> str:
    """
    Compute the notebook key string used in symbols.json location entries.
    This must be derived from the actual file path, not read from existing index.
    """
    if git_root is not None:
        try:
            rel = nb_path.relative_to(git_root)
            return rel.as_posix()
        except ValueError:
            pass
    return nb_path.as_posix()


def _update_symbols_json(
    index_dir: Path,
    nb_key: str,
    indexed_at: str,
    cells_data: list,
) -> None:
    """
    Update (or create) symbols.json in index_dir.

    Takes a blocking exclusive lock on symbols.nblock (polling with a ~10s
    timeout). On timeout a warning is printed and the update is skipped.
    Location entries whose notebook no longer exists on disk are dropped
    (garbage collection for deleted/renamed notebooks).
    """
    lock_path = index_dir / "symbols.nblock"
    symbols_path = index_dir / "symbols.json"

    try:
        lock_fd = open(lock_path, "a")
    except OSError:
        print("[warn] cannot open symbols.nblock — symbol update skipped",
              file=sys.stderr)
        return

    if not _lock_file_timeout(lock_fd, timeout=10.0):
        print("[warn] symbols.json lock busy — symbol update skipped",
              file=sys.stderr)
        lock_fd.close()
        return

    # GC helper: resolve a location's notebook key against the index base
    # (parent of .nb_index) and drop entries whose notebook is gone. The
    # existence check is cached per distinct notebook key per run.
    base = index_dir.parent
    _exists_cache: dict = {}

    def _nb_exists(loc: str) -> bool:
        # Colon-separator safety: locations are "<key>:<index>" where <key>
        # always ends in ".ipynb" (extension enforced at entry) and <index>
        # is appended LAST. rsplit(":", 1) therefore always splits at the
        # appended separator: any colon inside the path (e.g. "C:/x.ipynb",
        # "dir:3/nb.ipynb", "evil:7.ipynb") stays in the key part, and a key
        # can never end in ":<digits>" because it ends in "ipynb". Readers
        # that additionally int-validate the suffix cannot mis-parse a path
        # segment as a cell index. No schema change needed.
        key = loc.rsplit(":", 1)[0]
        if key not in _exists_cache:
            p = Path(key)
            if not p.is_absolute():
                p = base / key
            try:
                _exists_cache[key] = p.exists()
            except OSError:
                _exists_cache[key] = True  # unverifiable — keep the entry
        return _exists_cache[key]

    try:
        # Load existing symbols.json if it exists and has correct version
        existing = {"version": 1, "symbols": {}, "imports": {}}
        if symbols_path.exists():
            try:
                raw = json.loads(symbols_path.read_text(encoding="utf-8"))
                ver = raw.get("version")
                if isinstance(ver, int) and ver == 1:
                    existing = raw
                # version > 1 or corrupt: rebuild from scratch (existing stays as empty)
            except (json.JSONDecodeError, OSError):
                pass  # corrupt → rebuild

        # Remove all existing entries for this notebook key
        new_symbols: dict = {}
        new_imports: dict = {}
        old_max = existing.get("max_indexed_at", "")

        for sym_name, locs in existing.get("symbols", {}).items():
            filtered = [loc for loc in locs
                        if not loc.startswith(nb_key + ":") and _nb_exists(loc)]
            if filtered:
                new_symbols[sym_name] = filtered

        for imp_name, locs in existing.get("imports", {}).items():
            filtered = [loc for loc in locs
                        if not loc.startswith(nb_key + ":") and _nb_exists(loc)]
            if filtered:
                new_imports[imp_name] = filtered

        # Add new entries from current cells
        for cell in cells_data:
            i = cell["i"]
            # "<key>:<index>" is unambiguous even for paths containing colons:
            # the key always ends in ".ipynb" and the integer index is appended
            # last, so rsplit(":", 1) + int-validation recovers it exactly
            # (see _nb_exists above for the full argument).
            location = f"{nb_key}:{i}"
            for sym in cell.get("symbols_defined", []):
                new_symbols.setdefault(sym, [])
                if location not in new_symbols[sym]:
                    new_symbols[sym].append(location)
            for imp in cell.get("symbols_imported", []):
                new_imports.setdefault(imp, [])
                if location not in new_imports[imp]:
                    new_imports[imp].append(location)

        # Compute max_indexed_at
        max_indexed_at = max(old_max, indexed_at) if old_max else indexed_at

        generated_at = _now_utc()
        new_data = {
            "version": 1,
            "generated_at": generated_at,
            "max_indexed_at": max_indexed_at,
            "symbols": new_symbols,
            "imports": new_imports,
        }

        # Atomic write
        try:
            fd, tmp_path = tempfile.mkstemp(dir=index_dir, suffix=".sym_tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(new_data, f, ensure_ascii=False)
                    f.write("\n")
                    f.flush()
                    os.fsync(f.fileno())
            except Exception:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                return
            os.replace(tmp_path, symbols_path)
        except OSError:
            pass  # Non-fatal

    finally:
        # NOTE: the lock file is deliberately NOT unlinked. Deleting it after
        # release would let two processes lock different inodes of "the same"
        # lock file and clobber symbols.json. *.nblock is gitignored.
        _unlock_file(lock_fd)
        lock_fd.close()


# ---------------------------------------------------------------------------
# Core indexing logic
# ---------------------------------------------------------------------------

def _coerce_source(src) -> str:
    """Normalise cell source to a plain string."""
    if src is None:
        return ""
    if isinstance(src, list):
        return "".join(str(s) for s in src)
    return str(src)


def _build_index(nb_path: Path, nb_path_str: str, mtime: float, size: int, git_root: Path | None) -> dict:
    """
    Read the notebook and build the full index dict.
    nb_path_str is the 'notebook_path' field value (relative or absolute).
    """
    # Read notebook
    try:
        raw_bytes = nb_path.read_bytes()
    except OSError as e:
        _die(f"Cannot read notebook: {e}")

    content_hash = hashlib.sha256(raw_bytes).hexdigest()[:16]

    # Parse JSON (handle BOM)
    try:
        text = raw_bytes.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            text = raw_bytes.decode("latin-1")
            print("[warn] notebook is not valid UTF-8; falling back to latin-1",
                  file=sys.stderr)
        except Exception as e:
            _die(f"Cannot decode notebook: {e}")

    try:
        nb = json.loads(text)
    except json.JSONDecodeError as e:
        _die(f"Invalid JSON in notebook: {e}")

    # Validate nbformat
    nbformat = nb.get("nbformat")
    if nbformat != 4:
        _die(f"Only nbformat 4 is supported (got nbformat={nbformat!r}).")

    if "cells" not in nb:
        _die("Notebook has no 'cells' key (nbformat 3 or malformed).")

    cells_raw = nb.get("cells", [])

    # Extract kernel language
    meta = nb.get("metadata", {})
    kernelspec = meta.get("kernelspec", {})
    lang_info  = meta.get("language_info", {})
    kernel_language = (
        kernelspec.get("language", "")
        or lang_info.get("name", "")
        or ""
    )

    # Section extraction (single pass)
    section_data = _extract_sections_and_paths(cells_raw)

    indexed_at = _now_utc()
    cells_out = []

    for i, cell in enumerate(cells_raw):
        ctype = cell.get("cell_type", "unknown")
        source_str = _coerce_source(cell.get("source", []))
        source_hash = hashlib.md5(source_str.encode("utf-8")).hexdigest()[:8]
        first_line = _first_line(source_str)

        section_name, section_path, heading_info = section_data[i]

        # exec and status
        if ctype == "code":
            exec_count = cell.get("execution_count")
            outputs = cell.get("outputs", [])
            has_error = any(o.get("output_type") == "error" for o in outputs)
            if exec_count is not None:
                status = "error" if has_error else "ok"
            else:
                status = "not_run"
        else:
            exec_count = None
            status = None
            outputs = []

        # Symbol extraction (only for code cells)
        if ctype == "code":
            symbols_defined, symbols_imported, symbols_extracted = \
                _extract_symbols(source_str, kernel_language)
        else:
            symbols_defined, symbols_imported, symbols_extracted = [], [], False

        # Output processing
        if ctype == "code":
            has_output, output_types, output_text, output_truncated = \
                _process_outputs(outputs)
        else:
            has_output, output_types, output_text, output_truncated = \
                False, [], None, None

        cell_entry: dict = {
            "i":                 i,
            "type":              ctype,
            "exec":              exec_count,
            "status":            status,
            "source_hash":       source_hash,
            "first_line":        first_line,
            "section":           section_name,
            "section_path":      section_path,
            "symbols_defined":   symbols_defined,
            "symbols_imported":  symbols_imported,
            "symbols_extracted": symbols_extracted,
            "has_output":        has_output,
        }

        # output_types always present when has_output (or empty list)
        if has_output:
            cell_entry["output_types"] = output_types
            if output_text is not None:
                cell_entry["output_text"] = output_text
                cell_entry["output_truncated"] = output_truncated
        else:
            cell_entry["output_types"] = output_types

        # Heading fields (only for markdown heading cells)
        if heading_info is not None:
            cell_entry["heading"]      = heading_info["level"]
            cell_entry["heading_text"] = heading_info["text"]

        cells_out.append(cell_entry)

    index = {
        "version":        1,
        "notebook_path":  nb_path_str,
        "indexed_at":     indexed_at,
        "notebook_mtime": mtime,
        "notebook_size":  size,
        "nb_content_hash": content_hash,
        "kernel_language": kernel_language,
        "cell_count":     len(cells_out),
        "cells":          cells_out,
    }
    return index, indexed_at


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    args = sys.argv[1:]

    force = "--force" in args
    args  = [a for a in args if a != "--force"]

    if not args:
        print(__doc__, file=sys.stderr)
        sys.exit(1)

    nb_arg = args[0]

    # Validate extension
    if not nb_arg.endswith(".ipynb"):
        _die(f"Expected a .ipynb file, got '{nb_arg}'.")

    # Reject symlinks before resolve (resolve() follows them)
    nb_raw = Path(nb_arg)
    if nb_raw.is_symlink():
        _die(f"Refusing to index a symlink: {nb_arg}")

    # Resolve path
    nb_path = nb_raw.resolve()

    # Check existence
    if not nb_path.exists():
        _die(f"File not found: {nb_arg}")

    # Size check
    try:
        nb_size = os.path.getsize(nb_path)
    except OSError as e:
        _die(f"Cannot stat '{nb_arg}': {e}")

    if nb_size > MAX_FILE_SIZE:
        _die(f"File too large ({nb_size:,} bytes). Max is {MAX_FILE_SIZE:,} bytes.")

    # Resolve index paths
    index_file, index_dir, git_root = _index_file_path(nb_path)

    # Staleness check
    is_stale, mtime, size = _index_is_stale(index_file, nb_path, force)

    if not is_stale:
        print("[index] fresh — skipping rebuild", file=sys.stderr)
        sys.exit(0)

    # Ensure index directory exists
    try:
        index_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        _die(f"Cannot create index directory '{index_dir}': {e}")

    # Update .gitignore (best-effort)
    _update_gitignore(index_dir)

    # Compute notebook_path field (relative to git root, or absolute)
    if git_root is not None:
        try:
            rel = nb_path.relative_to(git_root)
            nb_path_str = rel.as_posix()
        except ValueError:
            nb_path_str = nb_path.as_posix()
    else:
        nb_path_str = nb_path.as_posix()

    # Optimistic concurrency: re-stat before building index
    try:
        cur_mtime = os.path.getmtime(nb_path)
        cur_size  = os.path.getsize(nb_path)
    except OSError:
        cur_mtime, cur_size = mtime, size

    if not force and (cur_mtime != mtime or cur_size != size):
        # Notebook changed while we were computing — abort silently
        sys.exit(0)

    # Build index
    try:
        index, indexed_at = _build_index(nb_path, nb_path_str, mtime, size, git_root)
    except SystemExit:
        raise
    except Exception as e:
        _die(f"Failed to build index: {e}")

    # Serialise the final stat + index write against concurrent indexers by
    # holding the notebook's companion .nblock (same convention as nb-write).
    # This closes the stat→os.replace window where a stale index could
    # clobber a fresher one. On lock timeout, warn and skip the index write.
    nb_lock_fd = None
    if _LOCK_BACKEND is not None:
        try:
            nb_lock_fd = open(str(nb_path) + ".nblock", "a")
        except OSError:
            nb_lock_fd = None  # cannot create lock file — proceed unlocked
        if nb_lock_fd is not None and not _lock_file_timeout(nb_lock_fd, timeout=10.0):
            print(f"[warn] notebook lock busy — index write skipped: {index_file}",
                  file=sys.stderr)
            nb_lock_fd.close()
            sys.exit(0)

    def _release_nb_lock():
        # The lock file is deliberately NOT unlinked (unlink-after-release
        # is a race; *.nblock is gitignored).
        if nb_lock_fd is not None:
            _unlock_file(nb_lock_fd)
            try:
                nb_lock_fd.close()
            except OSError:
                pass

    try:
        # Optimistic concurrency check before writing (under the lock)
        try:
            check_mtime = os.path.getmtime(nb_path)
            check_size  = os.path.getsize(nb_path)
        except OSError:
            check_mtime, check_size = mtime, size

        if not force and (check_mtime != mtime or check_size != size):
            # Notebook changed during indexing — abort silently
            sys.exit(0)

        # Ensure parent directory of index_file exists (for git-root nested paths)
        try:
            index_file.parent.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            _die(f"Cannot create index subdirectory: {e}")

        # Atomic write of per-notebook index
        try:
            fd, tmp_path = tempfile.mkstemp(dir=index_file.parent, suffix=".idx_tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(index, f, ensure_ascii=False)
                    f.write("\n")
                    f.flush()
                    os.fsync(f.fileno())
            except Exception as e:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                _die(f"Failed to write index file: {e}")
            try:
                os.replace(tmp_path, index_file)
            except PermissionError:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                print(f"[warn] index not written (transient file lock): {index_file}",
                      file=sys.stderr)
                sys.exit(0)
        except OSError as e:
            _die(f"Failed to write index file: {e}")
    finally:
        _release_nb_lock()

    print(f"[index] wrote {index_file}", file=sys.stderr)

    # Update symbols.json
    nb_key = _compute_notebook_key(nb_path, git_root)
    try:
        _update_symbols_json(index_dir, nb_key, indexed_at, index["cells"])
    except Exception as e:
        print(f"[warn] symbols.json update failed: {e}", file=sys.stderr)

    sys.exit(0)


if __name__ == "__main__":
    main()
