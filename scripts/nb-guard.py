#!/usr/bin/env python3
"""
nb-guard.py — PreToolUse hook that blocks direct Read/Edit/Write/MultiEdit
on .ipynb files and redirects Claude to the nb skill scripts.

Cross-platform replacement for nb-guard.sh. Requires only Python stdlib —
no jq, no bash, works on Linux, macOS, and Windows.

Exit codes:
  0 — non-.ipynb target (or fail-open on parse error): allow the operation
  1 — .ipynb target detected: block and print redirect message on stdout

Invocation (written into settings.json by install.py):
  Linux/macOS:  python3 /abs/path/to/nb-guard.py
  Windows:      python  C:\abs\path\to\nb-guard.py
"""

import json
import os
import re
import sys

# ---------------------------------------------------------------------------
# ANSI / control-character sanitisation
# Comprehensive ECMA-48 regex: covers standard CSI, private-mode CSI (?/>),
# OSC sequences, Fe escape sequences, and single-char escapes.
# Applied to any user-controlled string before echoing.
# ---------------------------------------------------------------------------
_ANSI_RE = re.compile(
    r'\x1b(?:'
    r'[@-Z\\-_]'                            # Fe: ESC @..Z, ESC \, ESC _
    r'|\[[0-?]*[ -/]*[@-~]'                 # CSI: covers ?, >, = params
    r'|\][^\x07\x1b]{0,512}(?:\x07|\x1b\\)?' # OSC: cap at 512 chars
    r'|[^@-_]'                              # other 2-char escapes
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
    """Return 'python3' or 'python' depending on what's available."""
    import shutil
    return "python3" if shutil.which("python3") else "python"


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
        fp = ti.get("file_path") or ti.get("path") or ""
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

    if safe_tool == "Read":
        print("Blocked: do not use Read on .ipynb files — raw JSON is ~15x more tokens than needed.")
        print("Use the nb skill instead:")
        print(f'  {py} "{nb_scripts}/nb-read.py" "{safe_file}"')
    else:  # Edit | Write | MultiEdit
        print(f"Blocked: do not use {safe_tool} on .ipynb files directly.")
        print("Use the nb skill instead:")
        print(f'  {py} "{nb_scripts}/nb-write.py" "{safe_file}" patch <index> -f <source_file>')

    sys.exit(1)


if __name__ == "__main__":
    main()
