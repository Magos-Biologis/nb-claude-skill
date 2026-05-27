#!/usr/bin/env python3
"""
nb-read.py — token-efficient Jupyter notebook reader.

Usage:
  nb-read.py <notebook.ipynb> [options]

Options:
  --cells N         Show only cell N  (e.g. --cells 3)
  --cells N-M       Show cells N through M inclusive (e.g. --cells 0-4)
  --cells N,M,K     Show specific cells (e.g. --cells 0,2,5)
  --type TYPE       Filter by cell type: code | markdown | raw
  --truncate N      Truncate cell source at N lines (default: 80, 0 = unlimited)
"""

import json
import sys
import argparse
import os
import re

MAX_FILE_SIZE = 100 * 1024 * 1024  # 100 MB
MAX_RANGE_SIZE = 10_000             # guard against billion-element set allocation

# Strip ANSI escape sequences from strings sourced from untrusted notebook metadata
_ANSI_RE = re.compile(r'\x1b(?:\[[0-9;]*[a-zA-Z]|\][^\x07]*\x07|[()][AB012])')

def _sanitise(s):
    return _ANSI_RE.sub('', str(s))


def parse_cell_filter(spec):
    """Return a set of indices, or None to mean 'all'."""
    if not spec:
        return None
    try:
        if "-" in spec and "," not in spec:
            parts = spec.split("-", 1)
            if parts[0] == "" or parts[1] == "":
                sys.exit(f"Error: invalid cell range '{spec}'. Use N-M format (e.g. 0-4). "
                         f"Negative indices are not supported.")
            lo, hi = int(parts[0]), int(parts[1])
            if lo < 0 or hi < lo:
                sys.exit(f"Error: invalid range '{spec}': lo must be >= 0 and <= hi.")
            if hi - lo >= MAX_RANGE_SIZE:
                sys.exit(f"Error: range '{spec}' spans {hi - lo + 1} cells; "
                         f"max is {MAX_RANGE_SIZE}.")
            return set(range(lo, hi + 1))
        if "," in spec:
            indices = set()
            for part in spec.split(","):
                part = part.strip()
                if not part:
                    continue
                n = int(part)
                if n < 0:
                    sys.exit(f"Error: negative index '{n}' is not supported in --cells.")
                indices.add(n)
            return indices
        # Single index
        n = int(spec)
        if n < 0:
            sys.exit(f"Error: negative index '{n}' is not supported in --cells.")
        return {n}
    except ValueError:
        sys.exit(f"Error: invalid --cells value '{spec}'. Expected N, N-M, or N,M,K "
                 f"(non-negative integers only).")


def render_source(lines, truncate):
    """
    lines is a list of strings (notebook cell source format) or a single string.
    Truncation notices are printed to stderr so they are never captured as cell content.
    """
    source = "".join(lines) if isinstance(lines, list) else (lines or "")
    if not source.strip():
        return "(empty)"
    all_lines = source.splitlines()
    if truncate and len(all_lines) > truncate:
        hidden = len(all_lines) - truncate
        # stderr: never mixed into captured stdout output that Claude might treat as source
        print(f"  *** TRUNCATED: {hidden} more line(s) not shown. "
              f"Re-read with --truncate 0 before patching this cell. ***",
              file=sys.stderr)
        return "\n".join(all_lines[:truncate])
    return "\n".join(all_lines)


def main():
    parser = argparse.ArgumentParser(description="Token-efficient notebook reader")
    parser.add_argument("notebook", help="Path to .ipynb file")
    parser.add_argument("--cells", default=None, help="Cell filter: N, N-M, or N,M,K")
    parser.add_argument("--type", dest="cell_type", choices=["code", "markdown", "raw"],
                        help="Only show cells of this type")
    parser.add_argument("--truncate", type=int, default=80,
                        help="Max lines per cell (0 = unlimited, default: 80)")
    args = parser.parse_args()

    path = args.notebook

    if args.truncate < 0:
        sys.exit(f"Error: --truncate must be >= 0 (use 0 for unlimited), got {args.truncate}.")

    if not path.endswith(".ipynb"):
        sys.exit(f"Error: expected a .ipynb file, got '{path}'.")

    try:
        size = os.path.getsize(path)
    except FileNotFoundError:
        sys.exit(f"Error: file not found: {path}")
    except OSError as e:
        sys.exit(f"Error: cannot stat '{path}': {e}")

    if size > MAX_FILE_SIZE:
        sys.exit(f"Error: file too large ({size:,} bytes). Max is {MAX_FILE_SIZE:,} bytes.")

    try:
        # utf-8-sig handles UTF-8 BOM transparently
        with open(path, encoding="utf-8-sig") as f:
            nb = json.load(f)
    except FileNotFoundError:
        sys.exit(f"Error: file not found: {path}")
    except json.JSONDecodeError as e:
        sys.exit(f"Error: invalid JSON in {path}: {e}")

    if "cells" not in nb:
        sys.exit(f"Error: notebook has no top-level 'cells' key "
                 f"(nbformat 3 or malformed? nb-read only supports nbformat 4).")

    cells = nb["cells"]
    total = len(cells)
    kernel = _sanitise(nb.get("metadata", {}).get("kernelspec", {}).get("name", "unknown"))
    lang   = _sanitise(nb.get("metadata", {}).get("kernelspec", {}).get("language", ""))
    lang_str = f" ({lang})" if lang and lang != kernel else ""

    cell_filter = parse_cell_filter(args.cells)

    # Header
    print(f"{path} | {total} cell{'s' if total != 1 else ''} | {kernel}{lang_str}\n")

    shown = 0
    for i, cell in enumerate(cells):
        if cell_filter is not None and i not in cell_filter:
            continue
        ctype = _sanitise(cell.get("cell_type", "unknown"))
        if args.cell_type and ctype != args.cell_type:
            continue

        bar = "─" * max(1, 44 - len(str(i)) - len(ctype))
        print(f"[{i}:{ctype}] {bar}")
        source_text = render_source(cell.get("source", []), args.truncate)
        print(source_text)
        print()
        shown += 1

    if shown == 0:
        print("(no cells matched the filter)")


if __name__ == "__main__":
    main()
