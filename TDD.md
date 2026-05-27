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

1. **Config dir detection:**
   ```python
   if sys.platform == "win32":
       default = Path(os.environ["APPDATA"]) / "Claude"
   else:
       default = Path.home() / ".claude"
   claude_dir = Path(os.environ.get("CLAUDE_CONFIG_DIR", default))
   ```

2. **Python command detection:**
   ```python
   # Prefer python3, fall back to python (Windows)
   import shutil
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

**Problem:** Both files hardcode `/home/anakin/.claude/skills/nb/scripts/nb-read.py`. Fresh-clone `pytest` fails with `FileNotFoundError`.

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
| `install.py` | **New** — cross-platform installer |
| `uninstall.py` | **New** — cross-platform uninstaller |
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

---

## 5. Test plan (new tests to write before implementation)

### `tests/test_read_safe.py`
- `test_ansi_in_cell_source_stripped` — source with `\x1b[31m` → stripped from stdout
- `test_private_csi_in_source_stripped` — `\x1b[?1049h` → stripped
- `test_osc_sequence_in_source_stripped` — `\x1b]0;TITLE\x07` → stripped
- `test_fake_cell_boundary_in_source_prefixed` — source containing `[0:code] ──` → prefixed with `│ `, unrecognised as boundary
- `test_source_int_does_not_crash` — `source: 42` → renders as `"42"`, exits 0
- `test_source_list_with_int_does_not_crash` — `source: ["line\n", 42]` → renders without crash
- `test_source_none_does_not_crash` — `source: null` → renders as empty cell, exits 0
- `test_output_summary_shown_for_code_cell` — cell with `outputs: [{output_type: stream, text: "hello"}]` → stdout contains `│ ── (1 output`
- `test_output_summary_not_shown_for_markdown` — markdown cell → no output summary line
- `test_no_safe_flag_passes_ansi_through` — `--no-safe` → raw ANSI in stdout
- `test_cell_source_lines_prefixed_with_pipe` — all source lines begin with `│ `

### `tests/test_write_new.py`
- `test_create_new_notebook` — `nb-write.py new.ipynb create` on non-existent path → exits 0, valid nbformat 4.5 JSON
- `test_create_fails_if_exists` — create on existing path → exits 1, error message
- `test_patch_negative_one_clear_error` — `patch -1` → error mentions "negative index", not "patch requires: <index>"
- `test_write_locked_file_clear_error` — (POSIX only) lock file with `flock`, attempt patch → exits 1 (or succeeds if lock is cooperative)
- `test_permission_error_message` — mock `os.replace` raising `PermissionError` → error mentions "locked by another process"

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

### `tests/test_install.py`
- `test_install_creates_skill_dir` — install.py to temp dir → `skills/nb/` populated
- `test_install_writes_hook_to_settings` — settings.json contains nb-guard.py hook entry
- `test_install_idempotent` — run twice → exactly one hook entry
- `test_install_removes_legacy_sh_entry` — settings.json with old `.sh` entry → replaced with `.py`
- `test_install_creates_settings_if_missing` — missing settings.json → created with `{}`
- `test_uninstall_removes_skill_dir` — after install, uninstall → `skills/nb/` gone
- `test_uninstall_removes_hook_entry` — hook entry removed from settings.json
- `test_windows_config_dir` — mock `sys.platform == "win32"` → uses `%APPDATA%\Claude`
- `test_settings_json_permissions` — created settings.json has mode 0o600 (POSIX only)
- `test_temp_file_cleaned_on_jq_failure` — simulate json write failure → no `.nb_tmp` left

---

## 6. Risks

| Risk | Mitigation |
|------|-----------|
| `│ ` prefix breaks existing scripts that parse `nb-read.py` output | `--no-safe` disables it; document in SKILL.md |
| `fcntl.flock` on NFS may deadlock | flock only taken if same PID doesn't already hold it; NFS flock semantics are inherently unreliable — document |
| `install.py` shell wrappers confuse users running `bash install.sh` on Windows | Add clear "Please run: python install.py" banner at top of `.sh` wrapper |
| `create` subcommand could be misused to overwrite (we forbid if exists) | Existing-file check is first; no race: `O_EXCL` semantics via temp+rename |
