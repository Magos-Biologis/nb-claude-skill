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
  --outline         Print compact one-line-per-cell table (no source body)
  --outputs         Show cell outputs after each source block
  --no-safe         Disable source sanitisation and │ line-prefix (raw output).
                    WARNING: disabling safe mode passes ANSI escape sequences and
                    raw control characters from untrusted notebook content through
                    to the terminal unchanged. Use only with trusted notebooks.
"""
# NOTE: raw docstring (r"""...""") prevents \p \a invalid-escape warnings on
# Python 3.12+ / 3.14.

from __future__ import annotations

import hashlib
import json
import sys
import argparse
import os
import re
from pathlib import Path

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8')

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
# Index discovery (§15)
# ---------------------------------------------------------------------------

def _find_index_dir(nb_path: Path) -> Path:
    """Walk upward from nb_path to find git root; fall back to nb_path.parent."""
    nb_path = nb_path.resolve()
    current = nb_path.parent
    current_dev = os.stat(current).st_dev
    for _ in range(20):
        git_dir = current / ".git"
        if git_dir.is_dir() and not git_dir.is_symlink():
            return current / ".nb_index"
        parent = current.parent
        if parent == current:
            break
        try:
            if os.stat(parent).st_dev != current_dev:
                break
        except OSError:
            break
        current = parent
        current_dev = os.stat(current).st_dev
    return nb_path.parent / ".nb_index"


def _index_file_path(nb_path: Path) -> Path:
    """Return the expected index JSON path for the given notebook."""
    nb_path = nb_path.resolve()
    index_dir = _find_index_dir(nb_path)
    # git case: index_dir is <git-root>/.nb_index
    # Need to check if index_dir is at git root (not nb parent)
    if index_dir.parent != nb_path.parent:
        # git root case: use relative path from git root
        rel = nb_path.relative_to(index_dir.parent)
        return index_dir / (str(rel).replace(os.sep, "/") + ".json")
    else:
        # no-git case: flat
        return index_dir / (nb_path.name + ".json")


def _load_fresh_index(nb_path: Path):
    """Load and return (data, reason) where reason is 'fresh', 'absent', 'stale',
    'corrupt', or 'error'.  Returns (data, 'fresh') only when all staleness
    signals match.
    """
    idx_path = _index_file_path(nb_path)
    if not idx_path.exists():
        return None, "absent"
    try:
        data = json.loads(idx_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None, "corrupt"
    try:
        stat = os.stat(nb_path)
        stored_mtime = data.get("notebook_mtime")
        stored_size = data.get("notebook_size")
        if stat.st_mtime != stored_mtime:
            return None, "stale"
        if stat.st_size != stored_size:
            return None, "stale"
        # mtime + size both match — treat as fresh (skip hash for nb-read)
        return data, "fresh"
    except OSError:
        return None, "error"


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


def render_source(lines, truncate, safe=True, cell_index=None):
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
        cell_info = f"[cell {cell_index}] " if cell_index is not None else ""
        print(f"  *** {cell_info}source truncated to {truncate} lines ({hidden} more not shown). "
              f"Re-read with --cells {cell_index} --truncate 0 for full source. ***"
              if cell_index is not None
              else f"  *** TRUNCATED: {hidden} more line(s) not shown. "
                   f"Re-read with --truncate 0 before patching this cell. ***",
              file=sys.stderr)
        all_lines = all_lines[:truncate]

    if safe:
        return "\n".join("│ " + l for l in all_lines)
    return "\n".join(all_lines)


def _output_summary(cell) -> str | None:
    """
    Return a one-line summary of cell outputs, or None if there are no outputs.
    Format: '│ ── (N outputs, M lines) ──'  (§2.6 canonical)
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
    plural = "output" if n_entries == 1 else "outputs"
    return f"│ ── ({n_entries} {plural}, {n_lines} lines) ──"


# ---------------------------------------------------------------------------
# Output rendering helpers (for --outputs mode)
# ---------------------------------------------------------------------------

def _extract_output_text(cell, safe=True) -> str | None:
    """
    Extract and render the text portions of a cell's outputs.
    Returns None if there is no renderable text output.
    """
    outputs = cell.get("outputs", [])
    if not outputs:
        return None

    parts = []
    for out in outputs:
        otype = out.get("output_type", "")
        if otype in ("stream", "execute_result", "display_data"):
            if otype in ("execute_result", "display_data"):
                text_val = out.get("data", {}).get("text/plain", out.get("text", []))
            else:
                text_val = out.get("text", [])
            if isinstance(text_val, list):
                text = "".join(str(t) for t in text_val)
            elif isinstance(text_val, str):
                text = text_val
            else:
                text = str(text_val)
            if text:
                parts.append(text)
        elif otype == "error":
            tb = out.get("traceback", [])
            if isinstance(tb, list):
                text = "\n".join(str(t) for t in tb)
            else:
                text = str(tb)
            if text:
                parts.append(text)

    if not parts:
        return None

    combined = "".join(parts)
    if safe:
        combined = _ANSI_RE.sub('', combined)
        combined = _CTRL_RE.sub('', combined)
    return combined


def _render_output_block(cell, safe=True) -> str | None:
    """
    Render the [output] block for a cell.  Returns None if no text output.
    """
    text = _extract_output_text(cell, safe=safe)
    if text is None:
        return None

    lines = text.splitlines()
    header = "[output] " + "─" * 40
    if safe:
        body = "\n".join("│ " + l for l in lines)
    else:
        body = "\n".join(lines)
    return header + "\n" + body


# ---------------------------------------------------------------------------
# Outline helpers (for --outline mode)
# ---------------------------------------------------------------------------

def _first_line_from_source(source_raw, safe=True) -> str:
    """Extract the first non-empty line from raw cell source."""
    text = _coerce_source(source_raw)
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            result = stripped[:120]
            if safe:
                result = _sanitise(result)
            return result
    return "(empty)"


def _format_outline_line(i: int, ctype: str, exec_count, first_line: str,
                         section: str | None = None) -> str:
    """Format a single outline line per §9/§11 spec.

    §11.3: section name shown as '§Name' (soft max 20 chars with '…').
    §11.4: total bracket must not exceed 72 chars (no bar in --outline mode,
           but bracket width is still limited to 72).
    """
    if ctype == "code":
        run_str = str(exec_count) if exec_count is not None else "——"
        bracket_core = f"{i}:{ctype}:run={run_str}"
    else:
        bracket_core = f"{i}:{ctype}"

    if section is not None:
        # Soft 20-char max with '…'
        if len(section) > 20:
            section_display = section[:20] + "…"
        else:
            section_display = section
        # Check if adding § section would exceed 72 chars for the bracket
        # Bracket format: [<core> §<section>]
        candidate = f"[{bracket_core} §{section_display}]"
        if len(candidate) > 72 and len(section_display) > 1:
            # Shrink section name further
            budget = 72 - len(f"[{bracket_core} §]")
            if budget > 0:
                section_display = section_display[:budget]
                candidate = f"[{bracket_core} §{section_display}]"
            else:
                candidate = f"[{bracket_core}]"
        bracket = candidate
    else:
        bracket = f"[{bracket_core}]"

    return f"{bracket} {first_line}"


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
    parser.add_argument("--outline", action="store_true",
                        help="Print compact one-line-per-cell table")
    parser.add_argument("--outputs", action="store_true",
                        help="Show cell outputs after each source block")
    parser.add_argument("--no-safe", dest="no_safe", action="store_true",
                        help="Disable ANSI sanitisation and │ line-prefix (raw output)")
    args = parser.parse_args()

    safe = not args.no_safe
    path = args.notebook

    if args.truncate < 0:
        sys.exit(f"Error: --truncate must be >= 0 (use 0 for unlimited), got {args.truncate}.")

    if not path.lower().endswith(".ipynb"):
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

    # Try to load a fresh index (used by --outline, --outputs, and §11 headers)
    nb_path = Path(path)
    index_data, index_reason = _load_fresh_index(nb_path)
    if index_reason == "stale":
        print(f"[STALE INDEX] {path}", file=sys.stderr)

    # Header
    print(f"{path} | {total} cell{'s' if total != 1 else ''} | {kernel}{lang_str}\n")

    # ------------------------------------------------------------------
    # --outline mode
    # ------------------------------------------------------------------
    if args.outline:
        # Build a lookup from cell index to index data when fresh
        index_cell_map = {}
        if index_data is not None:
            for c in index_data.get("cells", []):
                index_cell_map[c["i"]] = c

        for i, cell in enumerate(cells):
            if cell_filter is not None and i not in cell_filter:
                continue
            ctype = _sanitise(cell.get("cell_type", "unknown"))
            if args.cell_type and ctype != args.cell_type:
                continue

            exec_count = cell.get("execution_count", None)

            section = None
            if index_cell_map and i in index_cell_map:
                first_line = index_cell_map[i].get("first_line", "(empty)")
                if safe:
                    first_line = _sanitise(first_line)
                idx_exec = index_cell_map[i].get("exec")
                if exec_count is None and idx_exec is not None:
                    exec_count = idx_exec
                # §11.3: show section name from index
                section = index_cell_map[i].get("section")
                if section is not None and safe:
                    section = _sanitise(section)
            else:
                first_line = _first_line_from_source(cell.get("source", []), safe=safe)

            print(_format_outline_line(i, ctype, exec_count, first_line, section=section))

        return

    # ------------------------------------------------------------------
    # Normal and --outputs rendering
    # ------------------------------------------------------------------
    shown = 0
    for i, cell in enumerate(cells):
        if cell_filter is not None and i not in cell_filter:
            continue
        ctype = _sanitise(cell.get("cell_type", "unknown"))
        if args.cell_type and ctype != args.cell_type:
            continue

        # Build header with :run= for code cells (§11)
        if ctype == "code":
            exec_count = cell.get("execution_count", None)
            run_str = str(exec_count) if exec_count is not None else "——"
            meta = f"[{i}:{ctype}:run={run_str}]"
        else:
            meta = f"[{i}:{ctype}]"

        bar_len = max(1, 44 - len(meta))
        bar = "─" * bar_len
        print(f"{meta} {bar}")

        source_text = render_source(cell.get("source", []), args.truncate, safe=safe, cell_index=i)
        print(source_text)

        if ctype == "code":
            if args.outputs:
                # --outputs mode: render full output block
                output_block = _render_output_block(cell, safe=safe)
                if output_block:
                    print(output_block)
            else:
                # Normal mode: show summary line (§2.6)
                summary = _output_summary(cell)
                if summary:
                    print(summary)

        print()
        shown += 1

    if shown == 0:
        print("(no cells matched the filter)")


if __name__ == "__main__":
    main()
