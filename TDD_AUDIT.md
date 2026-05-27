# TDD Audit

**Date:** 2026-05-27  
**Auditing:** TDD.md

---

## Issues Found

### BLOCKER: §2.8 flock design is incorrect — lock released before rename

**Problem:** The TDD says:
> "Use `fcntl.flock(fd, fcntl.LOCK_EX)` on the notebook file before reading, held until after the rename."

But the code sketch:
```python
with open(path, "r", ...) as f:
    fcntl.flock(f, fcntl.LOCK_EX)
    nb = json.load(f)
# lock released here (with-block closes f)
# ... modify nb ...
# ... write temp, os.replace ...   ← race window here
```
The lock is released the moment `json.load` returns and the `with` block closes `f`. The modify-write-rename happens outside the lock, which doesn't eliminate the race.

**Fix:** Use a dedicated lock file to hold the lock through the entire read-modify-write:
```python
import fcntl, os

def load_locked(path):
    lock_path = path + ".nblock"
    lock_fd = open(lock_path, "w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        with open(path, "r", encoding="utf-8-sig") as f:
            nb = json.load(f)
    except:
        lock_fd.close()
        raise
    return nb, lock_fd  # caller must close lock_fd after rename

def save(path, nb, lock_fd=None):
    ...
    os.replace(tmp_path, path)
    if lock_fd is not None:
        lock_fd.close()  # release lock after rename
```

Also add: clean up `.nblock` files on normal exit (they're empty sentinel files — harmless to leave, but tidy to remove). Don't remove them inside the lock (creates a TOCTOU gap); just leave them.

**Update TDD §2.8** to reflect this design.

---

### BLOCKER: §2.3 `│ ` prefix breaks ALL existing `nb-read.py` output tests

**Problem:** `test_scripts.py` and `test_encoding.py` both assert on exact stdout content. Adding `│ ` prefix to every source line will break every test that checks cell content. These tests use repo-relative paths (not installed paths), so they run on fresh clones. Making `--safe` the default silently breaks the entire existing test suite.

**Options:**
1. **Make `--safe` default, update all affected tests.** — Most correct; forces test review. This is the right call since we're updating tests anyway (§2.11). Choose this.
2. Make `--no-safe` default, `--safe` opt-in. — Weaker protection; requires callers to opt in.
3. Keep old default, add separate `--prefix` flag. — Too many flags.

**Decision: Option 1.** Update `test_scripts.py` and `test_encoding.py` to expect `│ `-prefixed source lines. The `--no-safe` flag allows legacy behaviour.

**Update TDD §2.11** to add `test_scripts.py` and `test_encoding.py` to the files that need updating.

---

### MODERATE: §2.7 `create` subcommand has a TOCTOU race on new file

**Problem:** "File must not already exist (fail if it does)" — but checking existence then writing is a TOCTOU race. Another process could create the file between the check and the write.

**Fix:** Use `open(path, "x")` (exclusive creation mode) which is atomic on POSIX and Windows:
```python
try:
    with open(path, "x", encoding="utf-8") as f:
        json.dump(skeleton, f, indent=1)
except FileExistsError:
    die(f"file already exists: {path!r}")
```
But this means we can't use the atomic temp+rename pattern. Since we're creating a new file, a partial write on crash would leave a corrupt notebook. Use temp+rename+exclusive:
1. Write skeleton to temp file in same dir.
2. Try `os.link(tmp, path)` (POSIX atomic exclusive create).
3. Unlink temp.
4. On `FileExistsError` from `os.link`, clean up temp and die.

On Windows, `os.link` behaves differently; use `open(path, "x")` then write directly (accept tiny non-atomicity for create).

**Update TDD §2.7** to document this.

---

### MODERATE: §2.2 `install.py` wrapper strategy for `install.sh` is confusing

**Problem:** "Keep `install.sh` / `uninstall.sh` as thin wrappers that exec `python3 install.py`" — but the whole reason we're writing `install.py` is that the `.sh` files can't run on Windows. A wrapper `.sh` → `python3 install.py` doesn't help Windows users at all (they still can't run the `.sh`).

**Clarification:** The wrapper strategy is for POSIX users who already use `bash install.sh`. It's fine. Just make this clearer in the TDD — the wrapper is for backward compat on POSIX, not for Windows. Windows users are documented to run `python install.py` directly.

**Minor update only.**

---

### MODERATE: §5 test plan for `test_write_new.py::test_write_locked_file_clear_error` is untestable as written

**Problem:** "lock file with `flock`, attempt patch → exits 1 (or succeeds if lock is cooperative)" — the `(or succeeds if lock is cooperative)` hedge means the test can't assert either exit code. `flock` on Linux is advisory for processes in the same user (cooperative). Two processes calling `flock` on the same file will both block and serialise; neither will fail.

**Fix:** The test for concurrent locking should instead verify serialisation (two calls complete without data loss), not that one fails. Replace with:
- `test_concurrent_writes_serialised` — spawn two `nb-write.py patch` subprocesses on the same notebook with different cell content; verify both changes are present when both complete.
- Remove `test_write_locked_file_clear_error` (it's for Windows `PermissionError`, not POSIX flock).
- Keep `test_permission_error_message` which mocks `os.replace` raising `PermissionError`.

**Update TDD §5 test plan.**

---

### MINOR: §2.6 output summary format undefined — could confuse Claude

**Problem:** `│ ── (3 outputs, 47 lines) ──` looks similar to the `│ ` source prefix from §2.3. Claude needs to distinguish this meta-line from actual source content.

**Fix:** Use a slightly different format that's clearly metadata, not source:
```
# outputs: 3 entries, 47 lines (not shown)
```
Emit as a standalone line after the source block, NOT prefixed with `│ `. It uses `#` which reads as a comment/annotation, never appears at the start of a real source line (Python comments do start with `#` — reconsider).

**Better option:** Emit as a stderr note, or use a clearly-delimited footer:
```
[cell 2 has 3 output(s), 47 lines — not shown]
```
Use `[ ]` brackets which match the cell header style `[N:type]` but are clearly a note, not a header (no `──` separator follows). Goes to stdout so Claude sees it in the tool output.

**Update TDD §2.6.**

---

### MINOR: §2.4 OSC regex is greedy and can consume content past notebook boundaries

**Problem:** The OSC pattern `\][^\x07]*(?:\x07|\x1b\\)` uses `[^\x07]*` which is non-greedy in terms of `\x07` but could consume across multiple lines if an OSC sequence is opened but never closed (malformed input). In a cell with 1000 lines, an unterminated `\x1b]` near the start would cause the regex to consume everything after it.

**Fix:** Add a max length cap or use a non-greedy match on a limited character class:
```python
r'|\][^\x07\x1b]{0,512}(?:\x07|\x1b\\)?'  # cap at 512 chars, make terminator optional
```
Or simply strip all `\x1b]` and everything following up to the next `\x1b\\` or `\x07`, with a 256-char cap.

**Update TDD §2.4.**

---

### MINOR: §2.12 SKILL.md `$NB_PYTHON` variable strategy is unclear

**Problem:** "Replace all `python3` with `$NB_PYTHON` or document both forms" — `$NB_PYTHON` implies a shell variable that would need to be set. Claude isn't running in a shell where such a variable is pre-set. 

**Fix:** Instead of a variable, SKILL.md should say: "Use `python3` on Linux/macOS, `python` on Windows, or use `sys.executable` equivalents." Keep the platform detection in `install.py` and `nb-guard.py`; SKILL.md should document the platform difference simply.

---

### MINOR: §5 `test_install.py::test_temp_file_cleaned_on_jq_failure` wrong name

**Problem:** We're not using `jq` anymore; this should be `test_temp_file_cleaned_on_json_write_failure`.

---

## Summary: Changes to TDD Required

| # | Severity | Section | Change |
|---|----------|---------|--------|
| A1 | BLOCKER | §2.8 | Use lock-file pattern; hold lock through rename |
| A2 | BLOCKER | §2.3 + §2.11 | Add test_scripts.py + test_encoding.py to update list |
| A3 | MODERATE | §2.7 | Use `os.link` for atomic exclusive create on POSIX |
| A4 | MODERATE | §2.2 | Clarify wrapper is POSIX-only compat |
| A5 | MODERATE | §5 | Replace untestable lock test with concurrency serialisation test |
| A6 | MINOR | §2.6 | Change output summary format to `[cell N has X output(s)]` |
| A7 | MINOR | §2.4 | Cap OSC regex to 512 chars; make terminator optional |
| A8 | MINOR | §2.12 | Simplify python cmd to platform note, not `$NB_PYTHON` variable |
| A9 | MINOR | §5 | Rename `test_temp_file_cleaned_on_jq_failure` |

---

## TDD verdict: APPROVED WITH REQUIRED CHANGES

All blockers are fixable before implementation begins. No design-level rethink needed.
Proceed with the corrected design as specified in items A1–A9 above.
