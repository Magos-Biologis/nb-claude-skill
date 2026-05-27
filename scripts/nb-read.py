#!/usr/bin/env python3
r"""
nb-read.py — token-efficient Jupyter notebook reader.

Usage:
  nb-read.py <notebook.ipynb> [options]

Options:
  --cells N         Show only cell N  (e.g. --cells 3)
  --cells N-M       Show cells N through M inclusive (e.g. --cells 0-4)
  --cells N,M,K     Show specific cells (e.g. --cells 0,2,5)
  --type TYPE       Filter by cell type: code | markdown | raw
  --truncate N      Truncate cell source at N lines (default: 80, 0 = unlimited)
  --no-safe         Disable source sanitisation and │ line-prefix (raw output).
                    WARNING: disabling safe mode passes ANSI escape sequences and
                    raw control characters from untrusted notebook content through
                    to the terminal unchanged. Use only with trusted notebooks.
"""
# NOTE: raw docstring (r"""...""") prevents \p \a invalid-escape warnings on
# Python 3.12+ / 3.14.

from __future__ import annotations

import json
import sys
import argparse
import os
import re

MAX_FILE_SIZE = 100 * 1024 * 1024  # 100 MB
MAX_RANGE_SIZE = 10_000             # guard against billion-element set allocation

# ---------------------------------------------------------------------------
# ANSI sanitisation
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# ANSI / control-character sanitisation
#
# Comprehensive ECMA-48 regex.  Branch ORDER matters — OSC must precede Fe
# because ']' (0x5D) would otherwise match the Fe range [\-_] (0x5C-0x5F).
#
#   OSC  \x1b ] body BEL|ST     — terminal title injection etc.
#   Fe   \x1b [@-Z\^_]          — 2-char Fe sequences (excludes ] and [)
#   CSI  \x1b [ params final    — colour, cursor, private-mode (?/>) etc.
#   misc \x1b <any other char>  — remaining 2-char escapes
#
# Applied to any user-controlled string before echoing.
# ---------------------------------------------------------------------------
_ANSI_RE = re.compile(
    r'\x1b(?:'
    r'\][^\x07\x1b]{0,512}(?:\x07|\x1b\\)'  # OSC first (body + required terminator)
    r'|[@-Z\\^_]'                            # Fe: @-Z, \(ST), ^(PM), _(APC) — no ]
    r'|\[[0-?]*[ -/]*[@-~]'                  # CSI: covers ?, >, = params
    r'|[^@-_]'                               # other 2-char escapes
    r')'
)
_CTRL_RE = re.compile(r'[\x00-\x1f\x7f]')  # C0 controls + DEL


def _sanitise(s: str) -> str:
    """Strip ANSI sequences then any remaining control characters."""
    return _CTRL_RE.sub('', _ANSI_RE.sub('', str(s)))


# ---------------------------------------------------------------------------
# Cell parsing
# ---------------------------------------------------------------------------

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


def _coerce_source(lines) -> str:
    """
    Convert cell source to a plain string, defensively handling non-standard
    values (int, float, None, list-with-non-strings) found in corrupted or
    unusual notebooks.
    """
    if lines is None:
        return ""
    if isinstance(lines, list):
        return "".join(str(s) for s in lines)
    return str(lines)


def render_source(lines, truncate, safe=True):
    """
    Render cell source as a string.

    In safe mode (default):
      - ANSI/CSI/OSC sequences are stripped.
      - Every source line is prefixed with '│ ' to prevent cell content from
        being confused with the [N:type] ─── header lines or injected as
        fake boundaries.

    Truncation notices are printed to stderr so they are never captured as
    cell content.

    Returns the rendered string, or '│ (empty)' / '(empty)' for empty cells.
    """
    source = _coerce_source(lines)
    if safe:
        source = _ANSI_RE.sub('', source)

    if not source.strip():
        return "│ (empty)" if safe else "(empty)"

    all_lines = source.splitlines()
    if truncate and len(all_lines) > truncate:
        hidden = len(all_lines) - truncate
        # stderr: never mixed into captured stdout output that Claude might treat as source
        print(f"  *** TRUNCATED: {hidden} more line(s) not shown. "
              f"Re-read with --truncate 0 before patching this cell. ***",
              file=sys.stderr)
        all_lines = all_lines[:truncate]

    if safe:
        return "\n".join("│ " + l for l in all_lines)
    return "\n".join(all_lines)


def _output_summary(cell) -> str | None:
    """
    Return a one-line summary of cell outputs, or None if there are no outputs.
    Example: '[cell has 3 output(s), 5 lines — not shown]'
    """
    outputs = cell.get("outputs", [])
    if not outputs:
        return None
    n_entries = len(outputs)
    n_lines = 0
    for out in outputs:
        # stream / display_data / execute_result use 'text'
        text = out.get("text", [])
        if isinstance(text, list):
            n_lines += sum(len(str(t).splitlines()) for t in text)
        elif isinstance(text, str):
            n_lines += len(text.splitlines())
        # error outputs use 'traceback'
        tb = out.get("traceback", [])
        if isinstance(tb, list):
            n_lines += len(tb)
    return (f"[cell has {n_entries} output(s), {n_lines} lines — not shown]")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Token-efficient notebook reader")
    parser.add_argument("notebook", help="Path to .ipynb file")
    parser.add_argument("--cells", default=None, help="Cell filter: N, N-M, or N,M,K")
    parser.add_argument("--type", dest="cell_type", choices=["code", "markdown", "raw"],
                        help="Only show cells of this type")
    parser.add_argument("--truncate", type=int, default=80,
                        help="Max lines per cell (0 = unlimited, default: 80)")
    parser.add_argument("--no-safe", dest="no_safe", action="store_true",
                        help="Disable ANSI sanitisation and │ line-prefix (raw output)")
    args = parser.parse_args()

    safe = not args.no_safe
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
        source_text = render_source(cell.get("source", []), args.truncate, safe=safe)
        print(source_text)

        # Output summary for code cells (shown in both safe and no-safe modes)
        if ctype == "code":
            summary = _output_summary(cell)
            if summary:
                print(summary)

        print()
        shown += 1

    if shown == 0:
        print("(no cells matched the filter)")


if __name__ == "__main__":
    main()
