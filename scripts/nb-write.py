#!/usr/bin/env python3
"""
nb-write.py — surgical Jupyter notebook editor.

Operations:
  create                                     Create a new empty notebook.
  patch  <index> [-f <source_file>]          Replace cell source.
  insert <index> <type> [-f <source_file>]   Insert new cell before <index>.
                                              type: code | markdown | raw
                                              Use -1 to append at end.
  delete <index>                             Delete cell at <index>.

Source input:
  Pass -f <file>  to read source from a file (RECOMMENDED — avoids heredoc EOF issues).
  Omit -f         to read source from stdin.

Examples:
  python3 nb-write.py nb.ipynb create
  python3 nb-write.py nb.ipynb patch 0 -f /tmp/new_source.py
  python3 nb-write.py nb.ipynb insert 3 code -f /tmp/new_cell.py
  python3 nb-write.py nb.ipynb delete 5

Notes:
  - Patching a code cell clears its outputs and execution_count.
  - Inserting a code cell sets outputs=[] and execution_count=None.
  - Writes are atomic (temp file + fsync + rename). No .bak file is created.
  - The script refuses to operate on symlinks.
  - Only .ipynb files are accepted.
  - All status messages go to stderr; stdout is silent on success.
  - An exclusive file lock on a companion .nblock file serialises concurrent
    writes (fcntl on POSIX, msvcrt.locking on Windows).
  - Source files (-f) must be valid UTF-8 (a leading BOM is tolerated).
  - A patch whose source is identical to the cell's current source is a
    no-op: the file is not rewritten and outputs are preserved.
"""

import json
import sys
import os
import subprocess
import tempfile
import time
import secrets
import string
from pathlib import Path

MAX_FILE_SIZE = 100 * 1024 * 1024  # 100 MB
_REPLACE_RETRIES    = 3
_REPLACE_RETRY_DELAY = 0.05   # 50 ms — AV scans typically release within one window

_NB_INDEX_SIBLING = Path(__file__).parent / "nb-index.py"  # unresolved; module-level

# ---------------------------------------------------------------------------
# Portable lock helper — verbatim copy shared with nb-index.py (no shared
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

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8')


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def die(msg):
    print(f"Error: {msg}", file=sys.stderr)
    sys.exit(1)


def _parse_index(s, label="index"):
    try:
        n = int(s)
    except (ValueError, TypeError):
        die(f"{label} must be an integer, got '{s}'.")
    if n < 0:
        die(f"negative indices are not supported for {label} (got {n}). "
            f"Use -1 only for 'insert' to append at end.")
    return n


def _cell_id():
    """Random 8-char alphanumeric ID required by nbformat >= 4.5."""
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(8))


# ---------------------------------------------------------------------------
# I/O
# ---------------------------------------------------------------------------

def _check_path(path):
    """Common path validation: must be .ipynb and not a symlink."""
    if not path.endswith(".ipynb"):
        die(f"expected a .ipynb file, got '{path}'.")
    if os.path.islink(path):
        die(f"refusing to operate on a symlink: {path}")


def _validate_cells(path, nb, lock_fd):
    """
    Post-load validation: nb['cells'] (when present) must be a list, and
    duplicate cell ids get a one-line stderr warning (not repaired).
    """
    cells = nb.get("cells")
    if cells is not None and not isinstance(cells, list):
        if lock_fd:
            lock_fd.close()
        die(f"notebook 'cells' must be a list, got {type(cells).__name__} "
            f"in {path} (malformed notebook).")
    if isinstance(cells, list):
        seen = set()
        dupes = set()
        for cell in cells:
            if isinstance(cell, dict) and "id" in cell:
                cid = cell["id"]
                if cid in seen:
                    dupes.add(cid)
                seen.add(cid)
        if dupes:
            print(f"Warning: duplicate cell id(s) in {path}: "
                  f"{', '.join(sorted(str(d) for d in dupes))} (not repaired)",
                  file=sys.stderr)


def load(path, allow_oversize=False):
    """
    Load notebook JSON from *path* and return ``(nb, lock_fd)``.

    *lock_fd* is an open file object holding an exclusive lock (fcntl flock
    on POSIX, msvcrt.locking on Windows) on a companion ``<notebook>.nblock``
    lock file.  This lock **must** be released only *after* ``save()`` has
    completed its ``os.replace()``.  Pass it to ``save(path, nb,
    lock_fd=lock_fd)``.

    Why a separate lock file?  ``os.replace()`` (atomic rename) swaps the
    inode at *path*.  A flock held on the original notebook inode would be
    released when we close that fd, but a racing process that opened the
    file *before* the rename is still pointing at the old inode and would get
    its lock immediately — then read stale data.  Locking a stable companion
    file avoids this inode-swap hazard.

    If neither locking primitive exists, *lock_fd* is ``None``.

    *allow_oversize* — when True (patch/delete, which can shrink the file),
    the 100 MB load-time limit is not enforced; ``save()`` still refuses a
    result that both exceeds the limit and grew relative to the original.
    """
    _check_path(path)

    # Acquire exclusive lock on a companion lock file BEFORE reading the
    # notebook so that the lock scope covers the full read-modify-write cycle.
    lock_fd = None
    if _LOCK_BACKEND is not None:
        lpath = path + ".nblock"
        try:
            lf = open(lpath, "a")  # "a" creates if absent without truncating
        except OSError as e:
            die(f"cannot open lock file '{lpath}': {e}")
        try:
            _lock_file(lf, blocking=True)
        except OSError as e:
            lf.close()
            die(f"cannot acquire lock on '{lpath}': {e}")
        lock_fd = lf

    try:
        size = os.path.getsize(path)
    except FileNotFoundError:
        if lock_fd:
            lock_fd.close()
        die(f"file not found: {path}")
    except OSError as e:
        if lock_fd:
            lock_fd.close()
        die(f"cannot stat '{path}': {e}")
    if size > MAX_FILE_SIZE and not allow_oversize:
        if lock_fd:
            lock_fd.close()
        die(f"file too large ({size:,} bytes). Max is {MAX_FILE_SIZE:,} bytes.")
    try:
        # utf-8-sig handles UTF-8 BOM transparently
        with open(path, encoding="utf-8-sig") as f:
            nb = json.load(f)
        _validate_cells(path, nb, lock_fd)
        return nb, lock_fd
    except FileNotFoundError:
        if lock_fd:
            lock_fd.close()
        die(f"file not found: {path}")
    except UnicodeDecodeError:
        if lock_fd:
            lock_fd.close()
        die(f"cannot read '{path}': file is not valid UTF-8. "
            f"Jupyter notebooks must be UTF-8 encoded.")
    except json.JSONDecodeError as e:
        if lock_fd:
            lock_fd.close()
        die(f"invalid JSON in {path}: {e}")


def save(path, nb, lock_fd=None):
    """
    Atomic write: write to a temp file in the same directory, fsync, then
    os.replace() (POSIX atomic rename). No .bak created — the atomicity
    guarantee is that the file is either the old version or the new version,
    never a partial write.

    *lock_fd* — if not None, the open file object returned by ``load()``
    holding the exclusive lock.  It is released and closed *after*
    os.replace() succeeds so the lock spans the full read-modify-write cycle.

    Size policy: the serialised result is refused (error, no write) only if
    it both exceeds MAX_FILE_SIZE *and* is larger than the original file —
    shrinking an already-oversized notebook is always allowed.
    """
    dir_ = os.path.dirname(os.path.abspath(path))

    # Serialise up-front so the size policy can be enforced before any write.
    payload = json.dumps(nb, indent=1, ensure_ascii=False) + "\n"
    new_size = len(payload.encode("utf-8"))
    if new_size > MAX_FILE_SIZE:
        try:
            orig_size = os.path.getsize(path)
        except OSError:
            orig_size = 0
        if new_size > orig_size:
            die(f"refusing to write: result would be {new_size:,} bytes "
                f"(exceeds the {MAX_FILE_SIZE:,}-byte limit and is larger "
                f"than the original {orig_size:,} bytes). File left untouched.")

    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(dir=dir_, suffix=".nb_tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
        except Exception:
            os.unlink(tmp_path)
            tmp_path = None
            raise
        for _attempt in range(_REPLACE_RETRIES):
            try:
                os.replace(tmp_path, path)
                break
            except PermissionError:
                if _attempt == _REPLACE_RETRIES - 1:
                    os.unlink(tmp_path)
                    tmp_path = None
                    die(f"cannot write '{path}': file is locked by another process "
                        f"(is it open in Jupyter?). Close or checkpoint it first.")
                time.sleep(_REPLACE_RETRY_DELAY)
        tmp_path = None
    except OSError as e:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        die(f"failed to write {path}: {e}")
    finally:
        # Release the exclusive lock after the rename so the entire
        # read-modify-write cycle is serialised.
        if lock_fd is not None:
            _unlock_file(lock_fd)
            try:
                lock_fd.close()
            except OSError:
                pass
    print(f"✓ Written {path}", file=sys.stderr)


def read_source(flag_args):
    """
    Parse optional -f <file> from flag_args.
    Returns (source_lines, remaining_args).
    source_lines is a list of strings in notebook format (splitlines(keepends=True)).
    """
    if "-f" in flag_args:
        idx = flag_args.index("-f")
        if idx + 1 >= len(flag_args):
            die("-f requires a file path argument.")
        fpath = flag_args[idx + 1]
        try:
            # utf-8-sig tolerates a leading UTF-8 BOM
            with open(fpath, encoding="utf-8-sig") as f:
                text = f.read()
        except FileNotFoundError:
            die(f"source file not found: {fpath}")
        except UnicodeDecodeError as e:
            die(f"source file is not valid UTF-8: '{fpath}' ({e}). "
                f"Re-encode it as UTF-8 and retry; the notebook was not modified.")
        except OSError as e:
            die(f"cannot read source file '{fpath}': {e}")
        remaining = flag_args[:idx] + flag_args[idx + 2:]
        return text.splitlines(keepends=True), remaining
    else:
        text = sys.stdin.read()
        return text.splitlines(keepends=True), flag_args


def make_cell(cell_type, source, with_id):
    """
    Build a new cell dict. A cell ``id`` is emitted only when *with_id* is
    True (the notebook declares nbformat_minor >= 5, where ids are required);
    pre-4.5 notebooks must not gain ids — and nbformat/nbformat_minor are
    never bumped.
    """
    cell = {
        "cell_type": cell_type,
        "metadata": {},
        "source": source,
    }
    if with_id:
        cell["id"] = _cell_id()
    if cell_type == "code":
        cell["outputs"] = []
        cell["execution_count"] = None
    return cell


def get_cells(nb):
    if "cells" not in nb:
        die("notebook has no top-level 'cells' key "
            "(nbformat 3 or malformed? nb-write only supports nbformat 4).")
    if not isinstance(nb["cells"], list):
        die(f"notebook 'cells' must be a list, got "
            f"{type(nb['cells']).__name__} (malformed notebook).")
    return nb["cells"]


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------

def cmd_create(path):
    """Create a new empty nbformat 4.5 notebook. Fails if path already exists."""
    _check_path(path)
    skeleton = {
        "nbformat": 4,
        "nbformat_minor": 5,
        "metadata": {
            "kernelspec": {
                "display_name": "Python 3",
                "language": "python",
                "name": "python3",
            },
            "language_info": {"name": "python"},
        },
        "cells": [],
    }
    dir_ = os.path.dirname(os.path.abspath(path))
    os.makedirs(dir_, exist_ok=True)
    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(dir=dir_, suffix=".nb_tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(skeleton, f, indent=1, ensure_ascii=False)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        # Atomic exclusive create: link temp file to target only if target absent.
        # os.link raises FileExistsError atomically on POSIX.
        # On Windows, use open(path, "x") fallback.
        try:
            os.link(tmp_path, path)
            os.unlink(tmp_path)
            tmp_path = None
        except FileExistsError:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
            die(f"file already exists: '{path}'")
        except (AttributeError, NotImplementedError, OSError):
            # Fallback for Windows or filesystems that don't support hard links:
            if os.path.exists(path):
                os.unlink(tmp_path)
                tmp_path = None
                die(f"file already exists: '{path}'")
            os.replace(tmp_path, path)
            tmp_path = None
    except OSError as e:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        die(f"failed to create {path}: {e}")
    print(f"✓ Created {path}", file=sys.stderr)


def cmd_patch(nb, index, source):
    """
    Replace cell source. Returns True if the notebook was modified, False for
    a no-op patch (new source identical to current) — in that case nothing is
    touched: outputs/execution_count are kept and no write should happen.
    """
    cells = get_cells(nb)
    n = len(cells)
    if not (0 <= index < n):
        die(f"cell index {index} out of range "
            f"(notebook has {n} cell{'s' if n != 1 else ''}, indices 0–{n - 1}).")
    cell = cells[index]
    old = cell.get("source", [])
    old_str = "".join(old) if isinstance(old, list) else str(old)
    if old_str == "".join(source):
        print(f"cell {index} unchanged — no write", file=sys.stderr)
        return False
    cell["source"] = source
    if cell.get("cell_type") == "code":
        cell["outputs"] = []
        cell["execution_count"] = None
    # nbformat 4.5+ requires an id on every cell (JEP-62; writers should
    # auto-fill). Repair a missing id on the cell we are rewriting anyway.
    if (isinstance(nb.get("nbformat_minor"), int) and nb["nbformat_minor"] >= 5
            and "id" not in cell):
        cell["id"] = _cell_id()
        print(f"  cell {index} had no id — generated one (nbformat 4.5 "
              f"requires ids)", file=sys.stderr)
    line_count = len("".join(source).splitlines())
    print(f"  patched cell {index} "
          f"({cell.get('cell_type', 'unknown')}, {line_count} line(s))", file=sys.stderr)
    return True


def cmd_insert(nb, index, cell_type, source):
    cells = get_cells(nb)
    n = len(cells)
    # Cell ids are required from nbformat 4.5 on; emit one only when the
    # notebook itself declares minor >= 5 (never bump the declared version).
    with_id = isinstance(nb.get("nbformat_minor"), int) and nb["nbformat_minor"] >= 5
    new_cell = make_cell(cell_type, source, with_id)
    if index == -1:
        # Explicit append sentinel (only negative value allowed for insert)
        cells.append(new_cell)
        actual = len(cells) - 1
    elif index > n:
        die(f"insert index {index} out of range "
            f"(notebook has {n} cell{'s' if n != 1 else ''}; "
            f"valid insert positions: 0–{n}, or -1 to append).")
    else:
        # 0 <= index <= n: insert before cell at index (index==n appends naturally)
        cells.insert(index, new_cell)
        actual = index
    line_count = len("".join(source).splitlines())
    print(f"  inserted {cell_type} cell at index {actual} ({line_count} line(s))",
          file=sys.stderr)


def cmd_delete(nb, index):
    cells = get_cells(nb)
    n = len(cells)
    if not (0 <= index < n):
        die(f"cell index {index} out of range "
            f"(notebook has {n} cell{'s' if n != 1 else ''}, indices 0–{n - 1}).")
    ctype = cells[index].get("cell_type", "unknown")
    del cells[index]
    print(f"  deleted cell {index} ({ctype}), "
          f"notebook now has {len(cells)} cell(s)", file=sys.stderr)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)

    path = sys.argv[1]
    op   = sys.argv[2]
    rest = list(sys.argv[3:])

    if op == "create":
        cmd_create(path)
        return

    # Read the source (file or stdin) fully BEFORE acquiring the .nblock
    # exclusive lock in load(), so a slow/hung stdin producer cannot hold
    # the lock and block other writers.
    if op == "patch":
        if not rest:
            die("patch requires: <index> [-f <source_file>]")
        index = _parse_index(rest[0], "patch index")
        source, _ = read_source(rest[1:])
        # patch can shrink an oversized notebook — allow loading it
        nb, lock_fd = load(path, allow_oversize=True)
        if not cmd_patch(nb, index, source):
            # No-op patch: nothing changed — no write, no reindex.
            if lock_fd is not None:
                _unlock_file(lock_fd)
                lock_fd.close()
            return

    elif op == "insert":
        if len(rest) < 2:
            die("insert requires: <index> <type> [-f <source_file>]")
        # insert allows -1 as special append sentinel — parse manually
        try:
            index = int(rest[0])
        except (ValueError, TypeError):
            die(f"insert index must be an integer, got '{rest[0]}'.")
        if index < -1:
            die(f"negative indices are not supported for insert index (got {index}). "
                f"Use -1 to append at end.")
        cell_type = rest[1]
        if cell_type not in ("code", "markdown", "raw"):
            die(f"unknown cell type '{cell_type}', must be code | markdown | raw.")
        source, _ = read_source(rest[2:])
        nb, lock_fd = load(path)
        cmd_insert(nb, index, cell_type, source)

    elif op == "delete":
        if not rest:
            die("delete requires: <index>")
        index = _parse_index(rest[0], "delete index")
        # delete can shrink an oversized notebook — allow loading it
        nb, lock_fd = load(path, allow_oversize=True)
        cmd_delete(nb, index)

    else:
        die(f"unknown operation '{op}', must be create | patch | insert | delete.")

    save(path, nb, lock_fd=lock_fd)

    # Synchronous indexing: the write already succeeded (that is the
    # contract), so an indexing failure is surfaced as a warning but never
    # changes the exit code.
    if _NB_INDEX_SIBLING.exists():
        _script = _NB_INDEX_SIBLING.resolve()   # resolved at call time, not import time
        try:
            r = subprocess.run(
                [sys.executable, str(_script), str(Path(path).resolve())],
                shell=False,          # NEVER shell=True
                close_fds=True,
                capture_output=True,
                text=True,
            )
        except Exception as e:
            print(f"[warn] indexing failed: {e}", file=sys.stderr)
        else:
            if r.returncode != 0:
                tail = " | ".join((r.stderr or "").strip().splitlines()[-3:])
                print(f"[warn] indexing failed: {tail}", file=sys.stderr)
    else:
        print(f"[warn] nb-index.py not found at {_NB_INDEX_SIBLING}; "
              f"skipping auto-index", file=sys.stderr)


if __name__ == "__main__":
    main()
