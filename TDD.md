# Technical Design Document: nb skill hardening & cross-platform support

**Status:** Draft  
**Date:** 2026-05-27  
**Scope:** All fixes surfaced by the adversarial review (Goals · Linux compat · Windows compat · Security/Robustness)

---

## 1. Goals

1. Make `nb` work on Linux (all major distros), macOS, and Windows without any POSIX-only dependencies.
2. Close the security gap where cell source content reaches Claude's context unsanitised.
3. Fix crashes and incorrect behaviour found in the adversarial review.
4. Fix the test suite so it passes on a fresh clone without a prior `install.sh` run.
5. Keep all changes backward-compatible with the existing `nb-read.py` / `nb-write.py` public CLI.

---

## 2. Changes

### 2.1 Replace `scripts/nb-guard.sh` → `scripts/nb-guard.py`

**Problem:** The shell hook is dead on Windows (`cmd.exe` has no `bash`), silently disables on Ubuntu/Debian/Fedora/Alpine when `jq` is absent, and fails on Alpine where `bash` isn't installed.

**Solution:** Rewrite as a pure-Python script. Python is already a required dependency; it handles JSON natively (no `jq`); it runs identically on all platforms.

**Design:**
- Read stdin as UTF-8, parse with `json.loads`. Fail-open on `JSONDecodeError` (exit 0).
- Extract `tool_name`, `tool_input.file_path` (for Read/Edit/Write), and `tool_input.edits[*].file_path` (for MultiEdit).
- Sanitise extracted paths with the same regex used in the original shell: strip C0 controls, ANSI escapes, newlines.
- If any resolved path ends in `.ipynb`, print the redirect message to stdout and exit 1.
- Detect `python3` vs `python` at install time and embed the correct command in the redirect message.
- The script must be invocable as `python3 nb-guard.py` (POSIX) or `python nb-guard.py` (Windows).

**Hook command (written by install.py):**
```
python3 /abs/path/to/nb-guard.py      # Linux/macOS
python  C:\abs\path\to\nb-guard.py    # Windows
```

**Exit codes:** 0 = allow, 1 = block. (unchanged)

**Removed dependencies:** `jq`, `bash`, `tr`, `set -uo pipefail`.

---

### 2.2 Replace `install.sh` / `uninstall.sh` → `install.py` / `uninstall.py`

**Problem:** Bash scripts can't run on Windows; they depend on `jq` for JSON patching; path logic doesn't handle `%APPDATA%\Claude\` (Windows config dir).

**Solution:** Pure-Python replacements using only `pathlib`, `shutil`, `json`, `sys`, `os`.

**Design for `install.py`:**

1. **Config dir detection** (updated: commit 5523dad changed Windows path):
   ```python
   # Claude Code on Windows uses %USERPROFILE%\.claude, NOT %APPDATA%\Claude
   claude_dir = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude"))
   ```

2. **Python command detection** (updated: py launcher on Windows):
   ```python
   import shutil
   if sys.platform == "win32" and shutil.which("py"):
       py_cmd = "py -3"
   else:
       py_cmd = "python3" if shutil.which("python3") else "python"
   ```

3. **File copy:** `shutil.copytree` / `shutil.copy2` with `dirs_exist_ok=True`.

4. **settings.json patching:** `json.load` → add hook entry → `json.dump` to a `.nb_tmp` temp file → `os.replace` (atomic rename). Add `trap`-equivalent via `try/finally` to clean up temp file.

5. **Idempotency:** Search for existing hook entry that matches `nb-guard.py` (not `nb-guard.sh`). Remove stale `.sh` entries on upgrade.

6. **Preflight checks:** Verify Python version (`>= 3.8`). Warn but don't hard-fail if `CLAUDE_CONFIG_DIR` doesn't exist.

7. **settings.json permissions:** `os.chmod(settings_path, 0o600)` after creation.

8. **Output:** Use ASCII `[OK]` / `[WARN]` / `[ERROR]` prefixes instead of `✓` (broken in Windows CP850).

**Design for `uninstall.py`:**
- Remove `~/.claude/skills/nb/` (or equivalent).
- Remove the nb-guard hook entry from `settings.json` (match on `nb-guard.py`).
- Also clean up any legacy `nb-guard.sh` entry.

**Keep `install.sh` / `uninstall.sh`** as thin wrappers that exec `python3 install.py "$@"` for users who prefer the old invocation, with a deprecation notice.

---

### 2.3 `nb-read.py`: sanitise cell source content

**Problem:** Cell `source` is printed raw. A cell containing `[0:code] ─────────\nFAKE BOUNDARY` or ANSI escape sequences reaches Claude's context verbatim, enabling structural injection and terminal escapes.

**Solution:** Add a `--safe` flag (default `True`) that:
1. Strips ANSI/OSC/CSI sequences from the source text before printing (using the fixed comprehensive regex from 2.4).
2. Prefixes every line of cell source with `│ ` (U+2502, BOX DRAWINGS LIGHT VERTICAL). This makes it structurally impossible for cell content to be confused with the `[N:type] ──────` header lines, since those never start with `│`.

**API:** `--no-safe` flag disables both transforms (for debugging / scripting contexts that want raw content).

**Source text transform:**
```python
def _render_source_lines(source_text, safe=True):
    if safe:
        source_text = _ANSI_RE_FULL.sub('', source_text)
        lines = source_text.splitlines(keepends=True)
        return ['│ ' + l for l in lines]
    return source_text.splitlines(keepends=True)
```

**SKILL.md update:** Note that `│ ` prefix is added; Claude should not include it when writing cell patches.

---

### 2.4 `nb-read.py`: fix ANSI regex to cover private-mode CSI sequences

**Problem:** Current `_ANSI_RE` misses `\x1b[?1049h`, `\x1b=`, single-character `\x1bM`, etc.

**Solution:** Replace with the comprehensive regex from the ECMA-48 spec:
```python
_ANSI_RE_FULL = re.compile(
    r'\x1b(?:'
    r'[@-Z\\-_]'           # Fe escape sequences (ESC @..Z, ESC \.._ )
    r'|\[[0-?]*[ -/]*[@-~]' # CSI sequences (covers ?, >, = params)
    r'|\][^\x07]*(?:\x07|\x1b\\)'  # OSC sequences
    r'|[^@-_]'             # other 2-char sequences
    r')'
)
```

Apply this to both metadata fields and (via 2.3) cell source.

---

### 2.5 `nb-read.py`: fix crash on non-string `source`

**Problem:** `render_source()` crashes with `AttributeError`/`TypeError` when `source` is an int, float, or list containing non-strings.

**Solution:** Coerce to string defensively:
```python
if isinstance(lines, list):
    source = "".join(str(s) for s in lines)
elif lines is None:
    source = ""
else:
    source = str(lines)
```

---

### 2.6 `nb-read.py`: add output summary line

**Problem:** Code cells with stdout/stderr/traceback outputs are silently invisible. Claude cannot tell whether a cell ran successfully.

**Solution:** After each code cell's source block, if `outputs` is non-empty, print:
```
│ ── (3 outputs, 47 lines) ──
```
Count: number of output entries; lines: total `len(text.splitlines())` across all `text`/`traceback` fields.  
The `│ ` prefix keeps it consistent with the source framing (2.3) and distinguishable from cell headers.

**Canonical format:** The exact string is `│ ── (N outputs, M lines) ──` (with the `│ ` prefix, a space, two dashes, a space, and the parenthesised counts). Implementations that render a different format (e.g. `[cell has N output(s), M lines — not shown]`) are non-conforming. Tests must check for the literal substring `│ ── (` rather than a loose `"output" in stdout` to detect format drift.

This is metadata only — no output content is rendered (preserving token efficiency).

---

### 2.7 `nb-write.py`: add `create` subcommand

**Problem:** `BEHAVIOR_TESTS_INDEPENDENT.md` BT-12 implies new-notebook creation is possible, but `nb-write.py` dies on non-existent paths. Blocked by `nb-guard.sh` for `Write` tool, so there's no escape hatch.

**Solution:** Add `nb-write.py <path> create` that writes a minimal valid nbformat 4.5 skeleton:
```json
{
 "nbformat": 4,
 "nbformat_minor": 5,
 "metadata": {"kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"}, "language_info": {"name": "python"}},
 "cells": []
}
```
File must not already exist (fail if it does). Uses the same atomic-write path as save().

---

### 2.8 `nb-write.py`: add file locking (POSIX)

**Problem:** Two concurrent `nb-write.py` processes on the same notebook silently lose data (last rename wins, no error).

**Solution:** Use `fcntl.flock(fd, fcntl.LOCK_EX)` on the notebook file before reading, held until after the rename. On Windows (`fcntl` unavailable), fall through gracefully with a comment.

```python
try:
    import fcntl
    _have_flock = True
except ImportError:
    _have_flock = False  # Windows: no flock, document the limitation

def load(path):
    ...
    with open(path, "r", encoding="utf-8-sig") as f:
        if _have_flock:
            fcntl.flock(f, fcntl.LOCK_EX)
        nb = json.load(f)
    return nb, path
```

The lock is released when the file handle is closed (before the rename). This is sufficient to serialise concurrent readers — each process reads the post-rename state.

---

### 2.9 `nb-write.py`: better error on Windows `os.replace` `PermissionError`

**Problem:** On Windows, `os.replace()` raises `PermissionError` if the notebook is open in Jupyter (exclusive lock).

**Solution:** Catch `PermissionError` separately in `save()`:
```python
except PermissionError:
    os.unlink(tmp_path)
    die(f"cannot write {path!r}: file is locked by another process (is it open in Jupyter?)")
```

---

### 2.10 `nb-write.py`: fix `patch -1` error message

**Problem:** The guard `if rest[0].startswith("-") and rest[0] != "-f"` fires on `-1` before `_parse_index` can give the correct "negative indices not supported" message.

**Solution:** Tighten the guard to only reject flags (two or more chars starting with `-`, not pure negative numbers):
```python
if not rest or (rest[0].startswith("-") and not rest[0][1:].isdigit()):
    die("patch requires: <index> [-f <source_file>]")
```
`_parse_index` already rejects negative values with a clear message.

---

### 2.11 Fix hardcoded paths in `test_read_independent.py` and `test_write_independent.py`

**Problem:** Both files hardcode `/home/<user>/.claude/skills/nb/scripts/nb-read.py`. Fresh-clone `pytest` fails with `FileNotFoundError`.

**Solution:** Use a path relative to the test file:
```python
REPO_ROOT = Path(__file__).parent.parent
SCRIPT = REPO_ROOT / "scripts" / "nb-read.py"   # test_read_independent.py
SCRIPT = REPO_ROOT / "scripts" / "nb-write.py"  # test_write_independent.py
```

---

### 2.12 SKILL.md updates

- Replace all `python3` with a note that Claude should use the command detected at install time (embed as `$NB_PYTHON` or document both forms).
- Replace all `/tmp/nb_patch_source.txt` with `$TMPDIR/nb_patch_source.txt` (or instruct use of the `Write` tool for temp content).
- Remove the "first write normalises indent" claim (it's every write).
- Add section: "Cell outputs are not rendered — only `source` is shown. A summary line `│ ── (N outputs, M lines) ──` appears after code cells if outputs exist."
- Add section: "Cell source lines are prefixed with `│ ` in read output. Do not include this prefix when writing patches."
- Add section: "Known limitation: the nb-guard hook covers Read/Edit/Write/MultiEdit. Reading a notebook via the Bash tool (e.g. `cat`) bypasses the guard."
- Document `create` subcommand.

---

### 2.13 Installer shared module (`_nb_install_common.py`)

**Problem:**

`install.py` and `uninstall.py` share four functions that must behave identically but are duplicated with trivial divergences that will drift over time:

| Function | Divergence |
|---|---|
| `_default_claude_dir` | Error message shorter in `uninstall.py` (drops "Cannot determine Claude config dir.") |
| `_claude_dir` | Identical bodies; differ only in a missing comment separator |
| `_is_nb_guard_hook` | Truly identical — 2 lines |
| `_save_settings` | Identical logic; `uninstall.py` drops docstring and one inline comment |

`uninstall.py`'s `main()` also inlines the full hook-removal loop that `install.py` encapsulates as `_remove_nb_guard_entries()`, adding only a `removed` counter.

Two bugs inside `_save_settings` (present in both copies):

1. `import tempfile` is inside the function body — should be module-level.
2. File descriptor leak: if `os.fdopen(fd, ...)` raises after `mkstemp`, `fd` is never closed (extremely rare OS condition, but real).
3. `except OSError` in the outer handler is too narrow — a `json.dump` failure (e.g. encoding error) escapes cleanup and leaves the temp file on disk. Should be `except Exception`.

**Solution:** Extract shared code into `_nb_install_common.py` at repo root. Both scripts import from it. `install.py` copies it alongside itself when installing to `skill_dir`.

**`_nb_install_common.py` public API:**

```python
import json           # all at module level
import os
import sys
import tempfile       # was inside _save_settings — moved here
from pathlib import Path

def _default_claude_dir() -> Path:
    """Config dir for the current platform. Uses install.py's longer APPDATA error message."""

def _claude_dir() -> Path:
    """Respects CLAUDE_CONFIG_DIR env var; falls back to _default_claude_dir()."""

def _is_nb_guard_hook(cmd: str) -> bool:
    """True if cmd references nb-guard.py or nb-guard.sh."""

def _save_settings(settings_path: Path, data: dict) -> None:
    """Atomic write with fsync + os.replace. 0o600 on POSIX. Fd-leak safe."""

def _remove_nb_guard_entries(settings: dict) -> int:
    """Remove all nb-guard hook entries from settings in-place. Returns count removed."""
```

**`_save_settings` fixes — canonical pattern:**

```python
fd, tmp_path_str = tempfile.mkstemp(dir=dir_, suffix=".nb_tmp")
try:
    try:
        f = os.fdopen(fd, "w", encoding="utf-8")
    except Exception:
        os.close(fd)   # guard: close fd before re-raising
        raise
    with f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path_str, settings_path)
except Exception as e:
    Path(tmp_path_str).unlink(missing_ok=True)  # always try; missing_ok if replace already ran
    sys.exit(f"Error: cannot write {settings_path}: {e}")
```

Note: `ensure_ascii=False` added (consistent with all other `json.dump` calls in the project; guards against 6× size inflation on non-ASCII paths).

**`_remove_nb_guard_entries` signature change:** Returns `int` (count of removed individual hook entries). `install.py` ignores the return value; `uninstall.py` uses it for its status message, replacing the current 8-line inline loop.

**`_load_settings` is NOT shared.** `install.py` treats missing/corrupt settings as a fresh start (`return {}`). `uninstall.py` treats missing as "nothing to do" (early return) and corrupt as "skip removal". These are legitimately different behaviors; sharing would require a callback or enum that adds more complexity than the duplication it removes.

**Changes to `install.py`:**

Remove functions: `_default_claude_dir`, `_claude_dir`, `_is_nb_guard_hook`, `_save_settings`, `_remove_nb_guard_entries`.

Add after stdlib imports:
```python
from _nb_install_common import (
    _claude_dir, _is_nb_guard_hook, _save_settings, _remove_nb_guard_entries,
)
```

Extend the stems loop that copies install/uninstall to `skill_dir`:
```python
for stem in ("install.py", "uninstall.py", "_nb_install_common.py"):
```

**Changes to `uninstall.py`:**

Remove functions: `_default_claude_dir`, `_claude_dir`, `_is_nb_guard_hook`, `_save_settings`.

Add after stdlib imports:
```python
from _nb_install_common import (
    _claude_dir, _is_nb_guard_hook, _save_settings, _remove_nb_guard_entries,
)
```

Replace inline hook-removal loop in `main()` with:
```python
removed = _remove_nb_guard_entries(settings)
```

**Test addition (`tests/test_install.py`):**

- `test_install_copies_common_module` — after install, `skills/nb/_nb_install_common.py` exists in the skill dir (the installed `install.py`/`uninstall.py` import it at runtime).

---

### 2.14 Windows encoding and path compatibility — post-audit fixes

**Context:** Commit 5523dad was a Windows compatibility patch. A post-hoc audit
(including online cross-referencing against CPython issues and PEP 540/686) found
seven gaps and bugs not covered by that commit.

---

**Gap 1 — `nb-index.py` missing `reconfigure` block**

The commit message explicitly states *"all five scripts"* received the encoding
fix, but `nb-index.py` was omitted. When nb-index.py is run directly (by tests
or from the CLI on Windows) and its stderr output contains non-ASCII characters
(e.g. a notebook path in a non-English user directory), Python raises
`UnicodeEncodeError` on cp1252 consoles.

**Fix:** Add the reconfigure block to `nb-index.py` after `import sys`, before
the constants section.

---

**Gap 2 — `reconfigure` guard checks `sys.stdout` but calls `sys.stderr.reconfigure()` unconditionally**

All four scripts that received the fix use:
```python
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')   # unchecked — bug if stderr is a different object
```

If `sys.stderr` does not have `reconfigure` (e.g. a custom logging wrapper was
installed for stderr but not stdout), the second call raises `AttributeError`.

**Fix:** Guard each stream independently:
```python
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8')
```

Apply this pattern in all five scripts.

---

**Gap 3 — `_compute_notebook_key()` absolute-path fallback uses `str(nb_path)`**

`_compute_notebook_key()` in `nb-index.py` (line ~576) returns `str(nb_path)`
when the notebook is not inside a git root. On Windows, `str(Path)` uses
backslashes. This key is stored in `symbols.json`, so the same notebook's key
would be `C:\\Users\\foo\\nb.ipynb` in symbols.json but `C:/Users/foo/nb.ipynb`
in the per-notebook index's `notebook_path` field (which already uses
`as_posix()`). Lookups that join these two fields would silently fail.

**Fix:**
```python
# Before
return str(nb_path)

# After
return nb_path.as_posix()
```

---

**Gap 4 — Inconsistent path normalization (`str(rel).replace` vs `as_posix()`)**

Three sites in `nb-index.py` use `str(rel).replace(os.sep, "/")` while the
newer code at the absolute-path fallbacks already uses `rel.as_posix()`. These
are functionally equivalent on Windows (since `os.sep == "\\"`), but
inconsistent with the established pattern and harder to audit.

**Fix:** Replace all three occurrences with `rel.as_posix()`.

| Line | Context | Old | New |
|------|---------|-----|-----|
| `_index_file_path` | index file path computation | `str(rel).replace(os.sep, "/")` | `rel.as_posix()` |
| `_compute_notebook_key` | relative-path branch | `str(rel).replace(os.sep, "/")` | `rel.as_posix()` |
| `main()` | `notebook_path` field | `str(rel).replace(os.sep, "/")` | `rel.as_posix()` |

---

**Bug B1 — `guard_cmd` uses Windows backslashes in `settings.json`**

`install.py` builds the hook command as:
```python
guard_script = (scripts_dst / "nb-guard.py").resolve()
guard_cmd = f'{py_cmd} "{guard_script}"'
```

On Windows, `.resolve()` returns a `WindowsPath` whose `str()` gives backslashes:
`py -3 "C:\Users\...\nb-guard.py"`. Claude Code's hook runner's treatment of
backslashes in command strings is not guaranteed — using forward slashes avoids
the ambiguity entirely, and Python (and cmd.exe) both accept forward slashes in
file paths on Windows.

**Fix:**
```python
guard_cmd = f'{py_cmd} "{guard_script.as_posix()}"'
```

---

**Bug B2 — `nb-guard.py _python_cmd()` not updated for Windows**

`nb-guard.py` has its own `_python_cmd()` that was not updated alongside
`install.py`'s version. It returns `"python3"` or `"python"`, but on Windows
neither may be on PATH (the Python Launcher `py` is the standard). The redirect
messages shown to Claude would say `python3 nb-read.py ...` on Windows, which
is wrong.

**Fix:** Match the `install.py` pattern:
```python
def _python_cmd() -> str:
    import shutil as _shutil
    if sys.platform == "win32" and _shutil.which("py"):
        return "py -3"
    return "python3" if _shutil.which("python3") else "python"
```

---

**Bug B3 — `os.replace` in `nb-write.py` dies immediately on `PermissionError`**

On Windows, real-time antivirus scanners and the Windows Search indexer
transiently hold an exclusive handle on newly-written files during scanning
(CPython issue #46003; confirmed by multiple downstream projects). This causes
`os.replace()` to raise `PermissionError: [WinError 32]` even when the
notebook is not open in Jupyter. The current code catches `PermissionError` and
dies immediately — this makes the tool unusable on Windows systems with active AV.

AV scans typically complete in < 100 ms. Adding a short retry loop before dying
makes the tool work on Windows without sacrificing the clear error message for the
genuine Jupyter-is-holding-the-file case.

**Fix** — replace the single `os.replace` try/except with a retry loop:
```python
import time

_REPLACE_RETRIES = 3
_REPLACE_RETRY_DELAY = 0.05   # 50 ms — AV scans complete well within this

for attempt in range(_REPLACE_RETRIES):
    try:
        os.replace(tmp_path, path)
        break
    except PermissionError:
        if attempt == _REPLACE_RETRIES - 1:
            os.unlink(tmp_path)
            tmp_path = None
            die(f"cannot write '{path}': file is locked by another process "
                f"(is it open in Jupyter?). Close or checkpoint it first.")
        time.sleep(_REPLACE_RETRY_DELAY)
```

Total worst-case delay: 3 × 50 ms = 150 ms. Jupyter holds its lock for the
duration of a save (seconds), so three retries are sufficient to distinguish AV
(transient, < 100 ms) from Jupyter (persistent, seconds).

**nb-index.py `os.replace`:** The index write is fire-and-forget. A transient
`PermissionError` must not exit 1 (which would be misleading since the write
succeeded). Instead, catch separately and exit 0 with a warning:
```python
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
```

---

**Tests:** See `tests/test_windows_compat.py` (new file) in the test plan below.

---

## 3. Non-changes (explicit decisions)

| Item | Decision |
|------|----------|
| `nb-read.py` `--outputs` full render | Out of scope — would break token efficiency promise |
| `fcntl` locking on Windows | Document as unsupported; no lock-file workaround (adds complexity for rare scenario) |
| `nbformat_minor` version warning | Out of scope for now |
| `notify-send` fallback in global hooks | Out of scope (not this repo) |
| BOM preservation on write | Intentional: BOMs in JSON are non-standard |
| Bash tool bypass enforcement | Document only; adding a Bash hook would be too broad/invasive |

---

## 4. File change summary

| File | Action |
|------|--------|
| `scripts/nb-guard.py` | **New** — replaces `nb-guard.sh` |
| `scripts/nb-guard.sh` | **Kept** as legacy (POSIX-only systems); `install.py` prefers `.py` |
| `_nb_install_common.py` | **New** — shared installer utilities (§2.13) |
| `install.py` | **New** (§2.2) / **Updated** (§2.13) — imports from `_nb_install_common.py`, copies it to skill dir |
| `uninstall.py` | **New** (§2.2) / **Updated** (§2.13) — imports from `_nb_install_common.py`, replaces inline hook loop |
| `install.sh` | **Updated** — thin wrapper calling `python3 install.py` |
| `uninstall.sh` | **Updated** — thin wrapper calling `python3 uninstall.py` |
| `scripts/nb-read.py` | **Updated** — §2.3 §2.4 §2.5 §2.6 |
| `scripts/nb-write.py` | **Updated** — §2.7 §2.8 §2.9 §2.10 |
| `SKILL.md` | **Updated** — §2.12 |
| `tests/test_read_independent.py` | **Updated** — §2.11 |
| `tests/test_write_independent.py` | **Updated** — §2.11 |
| `tests/test_nb_guard_hook.py` | **Updated** — test `nb-guard.py` instead of `.sh` |
| `tests/test_nb_guard_hardened.py` | **Updated** — test `nb-guard.py` |
| `tests/test_nb_guard_py.py` | **New** — tests specific to the Python rewrite |
| `tests/test_read_safe.py` | **New** — §2.3 §2.4 §2.5 §2.6 |
| `tests/test_write_new.py` | **New** — §2.7 §2.8 §2.9 §2.10 |
| `tests/test_install.py` | **New** — install.py / uninstall.py behaviour |
| `tests/test_windows_compat.py` | **New** — §2.14 Windows encoding and path compatibility |

---

## 5. Test plan (new tests to write before implementation)

### `tests/test_windows_compat.py` (§2.14)

**TestReconfigureCompliance:**
- `test_all_five_scripts_have_reconfigure_block` — source check: all five scripts (`nb-guard.py`, `nb-index.py`, `nb-read.py`, `nb-search.py`, `nb-write.py`) contain `reconfigure` in their source
- `test_reconfigure_checks_each_stream_independently` — source check: no script guards `sys.stdout` but calls `sys.stderr.reconfigure()` without its own guard (i.e. the pattern `sys.stdout.reconfigure` must be directly preceded or followed by an independent `hasattr(sys.stderr, ...)` guard, not sharing the stdout check)
- `test_nb_index_stderr_unicode_path` — run nb-index.py on a notebook whose path contains a Unicode directory segment; assert exit code 0 and no `UnicodeEncodeError` on stderr

**TestPathNormalization:**
- `test_notebook_path_field_no_backslashes` — run nb-index.py; read the resulting JSON index; assert `notebook_path` value contains no backslash characters
- `test_symbols_json_key_no_backslashes` — run nb-index.py on a notebook in a tmpdir outside any git root; read `symbols.json`; assert the notebook's key (in the `notebooks` dict) contains no backslash characters
- `test_all_rel_replace_usages_removed` — source check: `nb-index.py` contains zero occurrences of `str(rel).replace(os.sep` (all replaced with `rel.as_posix()`)

**TestInstaller:**
- `test_guard_cmd_no_backslashes` — run `install.py` with `CLAUDE_CONFIG_DIR` pointing to a tmp dir; read the written `settings.json`; assert the hook command string contains no backslash characters
- `test_guard_cmd_is_forward_slash_path` — same setup; assert the hook command string contains a forward-slash path to `nb-guard.py`

**TestNbGuardPythonCmd:**
- `test_redirect_message_uses_platform_python` — feed nb-guard.py a `Read` payload for a `.ipynb` file; assert the printed redirect command contains `python3`, `python`, or `py` (not a bare `py` without the `-3` flag on non-Windows, not `python3` on Windows when only `py` is present — verified by source check of the function body rather than live execution)
- `test_nb_guard_python_cmd_has_py_branch` — source check: `nb-guard.py` `_python_cmd()` function body contains `"py -3"` or `"py"` for Windows (not just `python3`/`python`)

**TestAtomicWriteRetry:**
- `test_nb_write_replace_retry_present` — source check: `nb-write.py` `save()` function body contains `time.sleep` or retry logic around `os.replace`
- `test_nb_write_permission_error_cleared_message` — nb-write.py exits 1 with a clear error mentioning "locked by another process" when `os.replace` consistently fails; verified via a filesystem trick (make target path a directory so replace always fails with EISDIR/PermissionError)
- `test_nb_index_permission_error_exits_zero` — source check: nb-index.py's `os.replace` catch includes `PermissionError` → `sys.exit(0)` path (not `_die`)

---

### `tests/test_read_safe.py`
- `test_ansi_in_cell_source_stripped` — source with `\x1b[31m` → stripped from stdout
- `test_private_csi_in_source_stripped` — `\x1b[?1049h` → stripped
- `test_osc_sequence_in_source_stripped` — `\x1b]0;TITLE\x07` → stripped
- `test_fake_cell_boundary_in_source_prefixed` — source containing `[0:code] ──` → prefixed with `│ `, unrecognised as boundary
- `test_source_int_does_not_crash` — `source: 42` → renders as `"42"`, exits 0
- `test_source_list_with_int_does_not_crash` — `source: ["line\n", 42]` → renders without crash
- `test_source_none_does_not_crash` — `source: null` → renders as empty cell, exits 0
- `test_output_summary_shown_for_code_cell` — cell with `outputs: [{output_type: stream, text: "hello"}]` → stdout contains the literal substring `│ ── (1 output` (not merely the word "output" — this pins the canonical §2.6 format)
- `test_output_summary_not_shown_for_markdown` — markdown cell → no output summary line
- `test_no_safe_flag_passes_ansi_through` — `--no-safe` → raw ANSI in stdout
- `test_cell_source_lines_prefixed_with_pipe` — all source lines begin with `│ `

### `tests/test_write_new.py`
- `test_create_new_notebook` — `nb-write.py new.ipynb create` on non-existent path → exits 0, valid nbformat 4.5 JSON
- `test_create_fails_if_exists` — create on existing path → exits 1, error message
- `test_patch_negative_one_clear_error` — `patch -1` → error mentions "negative index", not "patch requires: <index>"
- `test_write_locked_file_clear_error` — (POSIX only) lock file with `flock`, attempt patch → exits 1 (or succeeds if lock is cooperative)
- `test_permission_error_message` — mock `os.replace` raising `PermissionError` → error mentions "locked by another process"

### `tests/test_read_outline.py` [NEW]
- `test_outline_flag_accepted` — `nb-read.py --outline` exits 0
- `test_outline_one_line_per_cell` — output has exactly one line per cell (after the header line)
- `test_outline_format_code_cell` — format is `[N:code:run=N] first_line`
- `test_outline_format_markdown_cell` — format is `[N:markdown   ] ## Heading` (no `run=` field)
- `test_outline_run_dashes_for_not_run` — `run=——` when `execution_count` is null
- `test_outline_no_bar` — no `─────────` bar line appears in outline mode output
- `test_outline_fallback_when_no_index` — works correctly (exits 0, one line per cell) without `nb-index.py` having been run
- `test_outline_stale_index_warns_stderr` — when index exists but is stale, emits `[STALE INDEX]` on stderr and falls back to reading notebook directly

### `tests/test_nb_guard_py.py`
- `test_read_ipynb_blocked` — payload with `tool_name: Read`, `file_path: test.ipynb` → exit 1
- `test_write_ipynb_blocked` — `tool_name: Write` → exit 1
- `test_edit_non_ipynb_allowed` — `tool_name: Edit`, `file_path: test.py` → exit 0
- `test_multiedit_mixed_blocked` — `tool_name: MultiEdit`, one `.ipynb` in edits → exit 1
- `test_multiedit_no_ipynb_allowed` — `tool_name: MultiEdit`, no `.ipynb` → exit 0
- `test_path_key_fallback` — `tool_input.path` instead of `tool_input.file_path` → still detected
- `test_ansi_in_file_path_sanitised` — path with `\x1b[31m` → stripped from output, exit 1
- `test_newline_in_file_path_sanitised` — path with `\n` → stripped, single-line output
- `test_invalid_json_fail_open` — garbage stdin → exit 0
- `test_missing_jq_no_longer_needed` — script runs with no jq present (by design)
- `test_python_cmd_in_redirect_message` — output contains `python3` or `python` (platform-appropriate)

### `tests/test_nb_guard_hook.py` — settings registration note
- `TestSettingsRegistration` must assert that the hook command registered in `settings.json` references `nb-guard.py`, **not** `nb-guard.sh`. A test that only checks for `"nb-guard"` (without the extension) would silently pass after a regression back to the shell version. The assertion must be: `assert "nb-guard.py" in hook_command`.

### `tests/test_install.py`
- `test_install_creates_skill_dir` — install.py to temp dir → `skills/nb/` populated
- `test_install_writes_hook_to_settings` — settings.json contains nb-guard.py hook entry
- `test_install_idempotent` — run twice → exactly one hook entry
- `test_install_removes_legacy_sh_entry` — settings.json with old `.sh` entry → replaced with `.py`
- `test_install_creates_settings_if_missing` — missing settings.json → created with `{}`
- `test_uninstall_removes_skill_dir` — after install, uninstall → `skills/nb/` gone
- `test_uninstall_removes_hook_entry` — hook entry removed from settings.json
- `test_windows_config_dir` — mock `sys.platform == "win32"` and `CLAUDE_CONFIG_DIR` unset → uses `Path.home() / ".claude"` (NOT `%APPDATA%\Claude`)
- `test_settings_json_permissions` — created settings.json has mode 0o600 (POSIX only)
- `test_temp_file_cleaned_on_jq_failure` — simulate json write failure → no `.nb_tmp` left

---

## 16. Test plan — `--outline` and `--outputs` modes

These features are fully specified in TDD_INDEX.md §9 and §10 but have no tests written yet. Tests belong in `tests/test_read_outline.py` (new file) and `tests/test_read_outputs.py` (new file).

### `tests/test_read_outline.py`

See §5 above for the eight outline tests. Additional coverage:

- `test_outline_reads_index_when_fresh` — after running `nb-index.py`, `--outline` does NOT open the `.ipynb` file (verified by mocking `open` or checking `strace`); uses `cells[i].first_line` from the index
- `test_outline_section_field_absent_without_index` — without a fresh index, no `§SectionName` appears in any outline line
- `test_outline_compatible_with_cells_filter` — `--outline --cells 0,2` shows only cells 0 and 2
- `test_outline_empty_notebook` — 0-cell notebook → only the header line, no cell lines, exits 0
- `test_outline_ansi_in_first_line_stripped` — index `first_line` containing `\x1b[31m` → stripped before output
- `test_outline_no_safe_passes_ansi_through` — `--outline --no-safe` with ANSI in `first_line` → raw ANSI in stdout

### `tests/test_read_outputs.py`

- `test_outputs_flag_accepted` — `nb-read.py --outputs` exits 0
- `test_outputs_section_rendered_after_source` — cell with output → `[output] ───` section appears after source block
- `test_outputs_reads_from_index_when_fresh` — after indexing, `--outputs` does NOT open `.ipynb` for output data
- `test_outputs_falls_back_to_notebook_when_no_index` — without index, output read directly from notebook; exits 0
- `test_outputs_stale_index_warns_stderr` — stale index → `[STALE INDEX]` on stderr, falls back to notebook
- `test_outputs_truncated_notice` — cell with `output_truncated: true` in index → truncation notice shown
- `test_outputs_no_section_for_cells_without_output` — cells with empty `outputs` → no `[output]` section
- `test_outputs_ansi_stripped_in_safe_mode` — ANSI in `output_text` → stripped (default `--safe`)
- `test_outputs_no_safe_passes_ansi_through` — `--outputs --no-safe` → raw ANSI in stdout
- `test_outputs_binary_output_not_rendered` — cell with only `image/png` output → no `[output]` section rendered (binary not stored)

---

## 6. Risks

| Risk | Mitigation |
|------|-----------|
| `│ ` prefix breaks existing scripts that parse `nb-read.py` output | `--no-safe` disables it; document in SKILL.md |
| `fcntl.flock` on NFS may deadlock | flock only taken if same PID doesn't already hold it; NFS flock semantics are inherently unreliable — document |
| `install.py` shell wrappers confuse users running `bash install.sh` on Windows | Add clear "Please run: python install.py" banner at top of `.sh` wrapper |
| `create` subcommand could be misused to overwrite (we forbid if exists) | Existing-file check is first; no race: `O_EXCL` semantics via temp+rename |
