# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Purpose

This repo is the **nb** Claude Code plugin — a token-efficient Jupyter notebook interface. Raw `.ipynb` files are 10–50× larger in tokens than needed; this plugin renders them compactly and enables surgical cell-level edits. It uses the Claude Code native plugin format (`claude plugin install`) and does not require a manual installer.

## Commands

```bash
# Run all tests (no install required)
pytest tests/ -q                          # Linux / macOS
python -m pytest tests/ -q               # Windows

# Run a single test file
pytest tests/test_nb_index.py -q

# Run a single test by name
pytest tests/test_nb_index.py::TestStaleness::test_stale_on_mtime_change -v
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

`skills/nb/SKILL.md` defines the 9 behavioural rules Claude follows when working with `.ipynb` files (never read raw JSON, re-read after insert/delete, use `-f <file>`, etc.).

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
- **File locking:** nb-write.py uses `fcntl.LOCK_EX` on a companion `.nblock` file for the full read-modify-write cycle (POSIX only). nb-index.py uses `fcntl.LOCK_EX | LOCK_NB` (non-blocking) on `symbols.nblock`. Both fall back to no-op on Windows where `fcntl` is unavailable.
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
| `test_nb_guard_hardened.py` | Injection, subdirectory bypass, MultiEdit payloads |
| `test_nb_index.py` | §1–§8, §13–§14: index location, staleness, sections, symbols, outputs, symbols.json |
| `test_nb_search.py` | §12: walk, keyword/symbol/import search, filters, security, streaming |
| `test_windows_compat.py` | Cross-platform encoding, path normalisation, py launcher, atomic-write retry |
| `test_plugin.py` | Plugin manifest, hooks.json, skill file placement, no installer files |

## Plugin details

The plugin uses the Claude Code native plugin format:

- `.claude-plugin/plugin.json` — manifest (name, description, version, author, license)
- `hooks/hooks.json` — declarative `PreToolUse` hook using `${CLAUDE_PLUGIN_ROOT}` for the script path
- `skills/nb/SKILL.md` — the skill file, auto-loaded by Claude Code

The hook command uses `${CLAUDE_PLUGIN_ROOT}/scripts/nb-guard.py`, which Claude Code expands to the plugin's installation directory at runtime. No settings.json patching is required.

Scripts are installed into a versioned cache directory under `~/.claude/plugins/cache/`. The exact path is recorded in `~/.claude/plugins/installed_plugins.json` under the `installPath` key for the `nb@*` entry. The SKILL.md resolves this dynamically at runtime via a `python3 -c` lookup — do not hardcode the path.

## TDD Documents

`TDD.md` — spec for nb-guard.py, nb-read.py safe/outline/outputs, nb-write.py create/locking.

`TDD_INDEX.md` — spec for nb-index.py (§0–§14) and nb-search.py (§12–§15), with full schema, staleness algorithm, symbol extraction regexes, output pipeline, and section hierarchy.

`TDD_AUDIT.md` / `TDD_INDEX_AUDIT.md` / `TDD_INDEX_GAPS.md` — adversarial review findings already resolved in the TDD documents.
