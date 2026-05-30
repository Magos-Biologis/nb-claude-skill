#!/usr/bin/env python3
r"""
nb-guard.py -- PreToolUse hook that blocks direct Read/Edit/Write/MultiEdit
on .ipynb files and redirects Claude to the nb skill scripts.

Cross-platform replacement for nb-guard.sh. Requires only Python stdlib --
no jq, no bash, works on Linux, macOS, and Windows.

Exit codes:
  0 -- non-.ipynb target (or fail-open on parse error): allow the operation
  1 -- .ipynb target detected: block and print redirect message on stdout

Invocation (written into settings.json by install.py):
  Linux/macOS:  python3 /abs/path/to/nb-guard.py
  Windows:      python  C:\abs\path\to\nb-guard.py
"""
# NOTE: raw docstring (r"""...""") above prevents \p \a invalid-escape warnings
# on Python 3.12+ / 3.14.

from __future__ import annotations

import json
import os
import re
import shlex
import sys

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8')

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
# Config / path detection
# ---------------------------------------------------------------------------

def _nb_scripts_dir() -> str:
    """Absolute path to the skills/nb/scripts directory."""
    # nb-guard.py lives inside scripts/; its parent is skills/nb/scripts
    return os.path.dirname(os.path.abspath(__file__))


def _python_cmd() -> str:
    """Return the best Python 3 command for the current platform."""
    import shutil as _shutil
    if sys.platform == "win32" and _shutil.which("py"):
        return "py -3"
    return "python3" if _shutil.which("python3") else "python"


# ---------------------------------------------------------------------------
# Payload parsing
# ---------------------------------------------------------------------------

def _extract_ipynb_path(data: dict) -> str:
    """
    Return the first .ipynb file path found in the payload, or '' if none.

    Handles:
      - Read / Edit / Write:  tool_input.file_path  (fallback: tool_input.path)
      - MultiEdit:            tool_input.edits[].file_path
    """
    tool = data.get("tool_name", "")
    ti = data.get("tool_input", {})

    if tool == "MultiEdit":
        for edit in ti.get("edits", []):
            fp = edit.get("file_path", "")
            if isinstance(fp, str) and fp.endswith(".ipynb"):
                return fp
        return ""
    else:
        # Use explicit None-check to avoid silently skipping empty-string file_path
        fp = ti.get("file_path")
        if fp is None:
            fp = ti.get("path")
        return fp if isinstance(fp, str) else ""


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    payload = sys.stdin.read()

    try:
        data = json.loads(payload)
    except (json.JSONDecodeError, ValueError):
        sys.exit(0)  # fail open: malformed payload, allow the operation

    tool = str(data.get("tool_name", ""))
    file_path = _extract_ipynb_path(data)

    if not file_path.endswith(".ipynb"):
        sys.exit(0)

    # Sanitise before echoing to prevent ANSI / newline injection
    safe_file = _sanitise(file_path)
    safe_tool = re.sub(r'[^A-Za-z]', '', tool)

    nb_scripts = _nb_scripts_dir()
    py = _python_cmd()

    # Only block the four known file-access tools. For unrecognised / missing
    # tool names, fail open rather than blocking all file I/O unexpectedly.
    KNOWN_TOOLS = {"Read", "Edit", "Write", "MultiEdit"}
    if safe_tool not in KNOWN_TOOLS:
        sys.exit(0)

    # Use shlex.quote for both the script path and the file path so the printed
    # command is safe to paste into a shell and cannot be used for injection via
    # a crafted filename containing $(), backticks, or other metacharacters.
    read_cmd  = f"{py} {shlex.quote(nb_scripts + '/nb-read.py')} {shlex.quote(safe_file)}"
    write_cmd = (f"{py} {shlex.quote(nb_scripts + '/nb-write.py')} {shlex.quote(safe_file)}"
                 f" patch <index> -f <source_file>")

    if safe_tool == "Read":
        print("Blocked: do not use Read on .ipynb files — raw JSON is ~15x more tokens than needed.")
        print("Use the nb skill instead:")
        print(f"  {read_cmd}")
    else:  # Edit | Write | MultiEdit
        print(f"Blocked: do not use {safe_tool} on .ipynb files directly.")
        print("Use the nb skill instead:")
        print(f"  {write_cmd}")

    sys.exit(1)


if __name__ == "__main__":
    main()
