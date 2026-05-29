# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Purpose

This repo is the **nb** Claude Code skill — a token-efficient Jupyter notebook interface. Raw `.ipynb` files are 10–50× larger in tokens than needed; this skill renders them compactly and enables surgical cell-level edits. The skill is developed here and installed into `~/.claude/skills/nb/` for use.

## Commands

```bash
# Run all tests (no install required)
pytest tests/ -q

# Run a single test file
pytest tests/test_nb_index.py -q

# Run a single test by name
pytest tests/test_nb_index.py::TestStaleness::test_stale_on_mtime_change -v

# Install into ~/.claude/skills/nb/ and register the PreToolUse hook
python3 install.py

# Uninstall
python3 uninstall.py

# Legacy wrappers (call the Python installers above)
bash install.sh
bash uninstall.sh

# Custom config dir
CLAUDE_CONFIG_DIR=/path/to/config python3 install.py

# Post-install verification (tests run against the installed copies)
pytest ~/.claude/skills/nb/tests/ -q
```

Only `pytest` is an external dependency. Everything else is Python stdlib.

## Architecture

Five cooperating scripts:

| Script | Role |
|--------|------|
| `scripts/nb-guard.py` | `PreToolUse` hook — blocks `Read`/`Edit`/`Write`/`MultiEdit` on `.ipynb` files, redirects to the scripts |
| `scripts/nb-read.py` | Renders notebook cells as compact indexed plain text; supports `--outline` and `--outputs` modes |
| `scripts/nb-write.py` | Atomically patches, inserts, deletes, or creates cells; fires `nb-index.py` after every write |
| `scripts/nb-index.py` | Builds a persistent `.nb_index/<notebook>.json` per notebook — enables outline, outputs, and search without re-reading raw JSON |
| `scripts/nb-search.py` | Cross-notebook keyword / symbol / import / section search over indexed notebooks |

`SKILL.md` defines the 9 behavioural rules Claude follows when working with `.ipynb` files (never read raw JSON, re-read after insert/delete, use `-f <file>`, etc.).

### Data flow

```
User edits notebook
  → nb-write.py patch/insert/delete (atomic write)
    → spawns nb-index.py (fire-and-forget, non-blocking)
      → updates .nb_index/<nb>.json + .nb_index/symbols.json

User reads notebook
  → nb-read.py (regular mode: reads .ipynb directly)
  → nb-read.py --outline (reads index if fresh, falls back to .ipynb)
  → nb-read.py --outputs (reads output_text from index if fresh)
  → nb-search.py (reads .nb_index/ dirs; keyword mode also opens .ipynb)
```

### Index location algorithm (§1)

`nb-index.py`, `nb-read.py`, and `nb-search.py` all use identical `_find_index_dir()` / `_index_file_path()` logic (copied verbatim — no shared import between standalone scripts):

1. `Path(nb_path).resolve()` first.
2. Walk upward ≤ 20 levels looking for `.git` that `is_dir() and not is_symlink()`.
3. Stop if `os.stat().st_dev` changes (filesystem boundary).
4. **Git root found:** index at `<git-root>/.nb_index/<relative-path>.json`
5. **No git root:** index at `<nb-dir>/.nb_index/<nb-basename>.json`

After construction, assert `index_path.resolve().is_relative_to(index_dir.resolve())` — exit 1 if the path escapes.

### nb-write.py auto-index

After a successful `save()` for `patch`/`insert`/`delete`, nb-write.py spawns nb-index.py:

```python
_NB_INDEX_SIBLING = Path(__file__).parent / "nb-index.py"  # unresolved; module-level
# resolved at call time, shell=False, sys.executable, both stdio → DEVNULL
```

`create` does **not** trigger indexing. If nb-index.py is absent, a warning is printed to stderr and the write still exits 0.

### nb-read.py output format

Standard mode — code cell headers include execution count since the §11 change:
```
notebook.ipynb | 12 cells | python3

[0:code:run=1] ──────────────────────────────────
│ import pandas as pd
│ ── (2 outputs, 5 lines) ──

[1:markdown] ────────────────────────────────────
│ ## Analysis
```

`│ ` prefix on source lines is structural (prevents fake boundary injection). Do not include it when writing patches.

`--outline` mode: one compact line per cell, reads from fresh index when available:
```
[0:code:run=1 ] import pandas as pd
[1:markdown   ] ## Analysis
[2:code:run=——] (empty)
```

`--outputs` mode: renders `output_text` from index (or notebook fallback) with `[output] ───` header.

### Security invariants shared across all scripts

- ANSI/CSI/OSC sequences and C0 control characters are stripped from any user-controlled string before echoing (prevents terminal injection).
- Symlinks are rejected for both notebooks and `.gitignore` (no write-through).
- `notebook_path` fields read from index JSON are validated with `is_relative_to(search_root.resolve())` before any file open.
- `nb-index.py` recomputes `notebook_path` from the actual file argument when updating `symbols.json` — never trusts the stored value (prevents cross-notebook symbol poisoning).

## Key Constraints / Invariants

- **nbformat 4 only.** Rejects v3 (`worksheets` key) and malformed files.
- **100 MB file size limit** on all scripts.
- **UTF-8-sig** used for reading (handles BOM transparently); latin-1 fallback with warning in nb-write.py source input.
- **`nb-write.py patch` clears outputs and execution_count** — intentional, matches Jupyter convention.
- **Atomic writes everywhere:** `tempfile.mkstemp` in the target directory → `fsync` → `os.replace`. No partial writes, no `.bak`.
- **POSIX file locking:** nb-write.py uses `fcntl.LOCK_EX` on a companion `.nblock` file for the full read-modify-write cycle. nb-index.py uses `fcntl.LOCK_EX | LOCK_NB` (non-blocking) on `symbols.nblock` — skips silently if unavailable.
- **`ensure_ascii=False`** in all `json.dump` calls (prevents 6× size inflation on CJK/Unicode output).
- **stdout is always silent on success** for all scripts; all status messages go to stderr.

## Test Coverage

Tests are written TDD-first against the spec before implementation. All black-box via subprocess.

| File | What it covers |
|------|----------------|
| `test_scripts.py` | Core read/write happy paths and flags |
| `test_encoding.py` | UTF-8 BOM, latin-1 fallback |
| `test_read_independent.py` | Full nb-read.py spec (filtering, truncation, edge cases) |
| `test_read_safe.py` | ANSI sanitisation, `│ ` prefix, output summary format |
| `test_read_outline.py` | `--outline` mode: format, fallback, stale index |
| `test_read_outputs.py` | `--outputs` mode: rendering, ANSI, binary outputs |
| `test_write_independent.py` | Full nb-write.py spec (atomicity, cell IDs, locking) |
| `test_write_new.py` | `create`, `patch -1` error, PermissionError message, concurrent writes |
| `test_nb_guard_py.py` | nb-guard.py exit codes, path sanitisation, fail-open |
| `test_nb_guard_hook.py` | Hook exit codes, settings.json registration (nb-guard.py) |
| `test_nb_guard_hardened.py` | Injection, subdirectory bypass, MultiEdit payloads |
| `test_nb_index.py` | §1–§8, §13–§14: index location, staleness, sections, symbols, outputs, symbols.json |
| `test_nb_search.py` | §12: walk, keyword/symbol/import search, filters, security, streaming |
| `test_install.py` | install.py / uninstall.py cross-platform behaviour |

`TestSettingsRegistration` (in `test_nb_guard_hook.py`) and `TestSettingsHardenedApproach` are post-install-only — they check `~/.claude/settings.json` and are skipped when settings.json is absent.

## TDD Documents

`TDD.md` — spec for nb-guard.py, install.py, nb-read.py safe/outline/outputs, nb-write.py create/locking.

`TDD_INDEX.md` — spec for nb-index.py (§0–§14) and nb-search.py (§12–§15), with full schema, staleness algorithm, symbol extraction regexes, output pipeline, and section hierarchy.

`TDD_AUDIT.md` / `TDD_INDEX_AUDIT.md` / `TDD_INDEX_GAPS.md` — adversarial review findings already resolved in the TDD documents.

## Install details

`install.py` (cross-platform, no `jq` dependency):
- Detects config dir: `$CLAUDE_CONFIG_DIR` → `~/.claude` (POSIX) or `%APPDATA%\Claude` (Windows).
- Copies files with `shutil.copytree(dirs_exist_ok=True)`.
- Patches `settings.json` atomically (temp file + `os.replace`).
- Removes stale `nb-guard.sh` entries if upgrading from the shell version.
- Creates `settings.json` with mode `0o600` if absent.

The hook command written into `settings.json` references `nb-guard.py` (not `nb-guard.sh`). `nb-guard.sh` is kept in the repo as a legacy POSIX fallback but is no longer the default.
