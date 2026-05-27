#!/usr/bin/env python3
"""
nb-write.py — surgical Jupyter notebook editor.

Operations:
  patch  <index> [-f <source_file>]          Replace cell source.
  insert <index> <type> [-f <source_file>]   Insert new cell before <index>.
                                              type: code | markdown | raw
                                              Use -1 to append at end.
  delete <index>                             Delete cell at <index>.

Source input:
  Pass -f <file>  to read source from a file (RECOMMENDED — avoids heredoc EOF issues).
  Omit -f         to read source from stdin.

Examples:
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
"""

import json
import sys
import os
import tempfile
import secrets
import string

MAX_FILE_SIZE = 100 * 1024 * 1024  # 100 MB


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def die(msg):
    print(f"Error: {msg}", file=sys.stderr)
    sys.exit(1)


def _parse_index(s, label="index"):
    try:
        return int(s)
    except (ValueError, TypeError):
        die(f"{label} must be an integer, got '{s}'.")


def _cell_id():
    """Random 8-char alphanumeric ID required by nbformat >= 4.5."""
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(8))


# ---------------------------------------------------------------------------
# I/O
# ---------------------------------------------------------------------------

def load(path):
    if not path.endswith(".ipynb"):
        die(f"expected a .ipynb file, got '{path}'.")
    if os.path.islink(path):
        die(f"refusing to operate on a symlink: {path}")
    try:
        size = os.path.getsize(path)
    except FileNotFoundError:
        die(f"file not found: {path}")
    except OSError as e:
        die(f"cannot stat '{path}': {e}")
    if size > MAX_FILE_SIZE:
        die(f"file too large ({size:,} bytes). Max is {MAX_FILE_SIZE:,} bytes.")
    try:
        # utf-8-sig handles UTF-8 BOM transparently
        with open(path, encoding="utf-8-sig") as f:
            return json.load(f)
    except FileNotFoundError:
        die(f"file not found: {path}")
    except UnicodeDecodeError:
        die(f"cannot read '{path}': file is not valid UTF-8. "
            f"Jupyter notebooks must be UTF-8 encoded.")
    except json.JSONDecodeError as e:
        die(f"invalid JSON in {path}: {e}")


def save(path, nb):
    """
    Atomic write: write to a temp file in the same directory, fsync, then
    os.replace() (POSIX atomic rename). No .bak created — the atomicity
    guarantee is that the file is either the old version or the new version,
    never a partial write.
    """
    dir_ = os.path.dirname(os.path.abspath(path))
    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(dir=dir_, suffix=".nb_tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(nb, f, indent=1, ensure_ascii=False)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
        except Exception:
            os.unlink(tmp_path)
            tmp_path = None
            raise
        os.replace(tmp_path, path)
        tmp_path = None
    except OSError as e:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        die(f"failed to write {path}: {e}")
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
            with open(fpath, encoding="utf-8") as f:
                text = f.read()
        except FileNotFoundError:
            die(f"source file not found: {fpath}")
        except UnicodeDecodeError:
            # File is not valid UTF-8 — fall back to latin-1, which accepts
            # every possible byte value. Warn so the user knows what happened.
            print(f"Warning: '{fpath}' is not valid UTF-8; "
                  f"falling back to latin-1 encoding.", file=sys.stderr)
            try:
                with open(fpath, encoding="latin-1") as f:
                    text = f.read()
            except OSError as e:
                die(f"cannot read source file '{fpath}': {e}")
        except OSError as e:
            die(f"cannot read source file '{fpath}': {e}")
        remaining = flag_args[:idx] + flag_args[idx + 2:]
        return text.splitlines(keepends=True), remaining
    else:
        text = sys.stdin.read()
        return text.splitlines(keepends=True), flag_args


def make_cell(cell_type, source):
    cell = {
        "id": _cell_id(),
        "cell_type": cell_type,
        "metadata": {},
        "source": source,
    }
    if cell_type == "code":
        cell["outputs"] = []
        cell["execution_count"] = None
    return cell


def get_cells(nb):
    if "cells" not in nb:
        die("notebook has no top-level 'cells' key "
            "(nbformat 3 or malformed? nb-write only supports nbformat 4).")
    return nb["cells"]


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------

def cmd_patch(nb, index, source):
    cells = get_cells(nb)
    n = len(cells)
    if not (0 <= index < n):
        die(f"cell index {index} out of range "
            f"(notebook has {n} cell{'s' if n != 1 else ''}, indices 0–{n - 1}).")
    cell = cells[index]
    cell["source"] = source
    if cell.get("cell_type") == "code":
        cell["outputs"] = []
        cell["execution_count"] = None
    line_count = len("".join(source).splitlines())
    print(f"  patched cell {index} "
          f"({cell.get('cell_type', 'unknown')}, {line_count} line(s))", file=sys.stderr)


def cmd_insert(nb, index, cell_type, source):
    cells = get_cells(nb)
    n = len(cells)
    new_cell = make_cell(cell_type, source)
    if index == -1:
        # Explicit append sentinel
        cells.append(new_cell)
        actual = len(cells) - 1
    elif index < 0:
        die(f"invalid index {index}. Use -1 to append at end.")
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

    nb = load(path)

    if op == "patch":
        if not rest or (rest[0].startswith("-") and rest[0] != "-f"):
            die("patch requires: <index> [-f <source_file>]")
        index = _parse_index(rest[0], "patch index")
        source, _ = read_source(rest[1:])
        cmd_patch(nb, index, source)

    elif op == "insert":
        if len(rest) < 2:
            die("insert requires: <index> <type> [-f <source_file>]")
        index     = _parse_index(rest[0], "insert index")
        cell_type = rest[1]
        if cell_type not in ("code", "markdown", "raw"):
            die(f"unknown cell type '{cell_type}', must be code | markdown | raw.")
        source, _ = read_source(rest[2:])
        cmd_insert(nb, index, cell_type, source)

    elif op == "delete":
        if not rest:
            die("delete requires: <index>")
        index = _parse_index(rest[0], "delete index")
        cmd_delete(nb, index)

    else:
        die(f"unknown operation '{op}', must be patch | insert | delete.")

    save(path, nb)


if __name__ == "__main__":
    main()
