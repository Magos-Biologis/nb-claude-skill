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

`skills/nb/SKILL.md` defines the 11 behavioural rules Claude follows when working with `.ipynb` files (never read raw JSON, re-read after insert/delete, use `-f <file>`, etc.).

### Data flow

```
User edits notebook
  → nb-write.py patch/insert/delete (atomic write)
    → runs nb-index.py synchronously (failures surface as [warn] on stderr)
      → updates .nb_index/<nb>.json + .nb_index/symbols.json

User reads notebook
  → nb-read.py (regular mode: reads .ipynb directly)
  → nb-read.py --outline (index-backed fast path when fresh — notebook never opened; falls back to .ipynb)
  → nb-read.py --outputs (notebook-backed BY DESIGN — index output_text is capped at 4 KB)
  → nb-search.py (reads .nb_index/ dirs; keyword mode also opens .ipynb)
```

### Index location algorithm (§1)

`nb-index.py`, `nb-read.py`, and `nb-search.py` all use identical `_find_index_dir()` / `_index_file_path()` logic (copied verbatim — no shared import between standalone scripts):

1. `Path(nb_path).resolve()` first.
2. Walk upward ≤ 20 levels looking for a `.git` entry that is a real (non-symlink) directory OR a regular (non-symlink) file starting with `gitdir:` (worktrees/submodules — the pointer is never followed; the root is the dir containing the entry). Symlinked `.git` is always rejected. When the walk gives up (depth cap / st_dev boundary), a `[note]` explains the per-directory fallback.
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
- `notebook_path` fields read from index JSON are resolved against the parent of their own `.nb_index` dir (`index_base`) and containment-checked with `is_relative_to(index_base.resolve())` before any file open; a separate `_in_scope` filter then restricts results to the requested `search_root` (out-of-scope is a silent skip, escaping the index base is a warned skip).
- `nb-index.py` recomputes `notebook_path` from the actual file argument when updating `symbols.json` — never trusts the stored value (prevents cross-notebook symbol poisoning).

## Key Constraints / Invariants

- **nbformat 4 only.** Rejects v3 (`worksheets` key) and malformed files.
- **100 MB file size limit** on all scripts.
- **UTF-8-sig** used for reading (handles BOM transparently); non-UTF-8 `-f` source files are a hard error in nb-write.py (no latin-1 fallback).
- **`nb-write.py patch` clears outputs and execution_count** — intentional, matches Jupyter convention. Exception: a patch whose source is identical to the cell's current source is a no-op (no clear, no write, no reindex).
- **Atomic writes everywhere:** `tempfile.mkstemp` in the target directory → `fsync` → `os.replace`. No partial writes, no `.bak`.
- **File locking:** both scripts share a verbatim-copied portable lock helper — `fcntl` on POSIX, `msvcrt.locking` on Windows, no-op only if neither exists. nb-write holds the notebook's `.nblock` exclusively for the read-modify-write cycle (source is read *before* locking). nb-index takes `symbols.nblock` blocking-with-timeout (~10 s, then `[warn]` + skip) and holds the notebook's `.nblock` across the final stat + index write. Lockfiles are never unlinked (unlink-after-release is an inode race); `*.nblock` is gitignored.
- **`ensure_ascii=False`** in all `json.dump` calls (prevents 6× size inflation on CJK/Unicode output).
- **stdout is always silent on success** for all scripts; all status messages go to stderr.
- **Conventions are test-enforced** (`tests/test_conventions.py`): Python 3.8 floor (AST parse + banned-API list — extend the list when a new post-3.8 API is found), byte-identity of all verbatim-shared helpers, explicit `encoding=` on every text open, `newline="\n"` on every text-mode write, `sys.executable` in test runners.

## Test Coverage

Tests are written TDD-first against the spec before implementation. All black-box via subprocess.

| File | What it covers |
|------|----------------|
| `test_scripts.py` | Core read/write happy paths and flags |
| `test_encoding.py` | UTF-8 BOM, non-UTF-8 hard error |
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
- `.claude-plugin/marketplace.json` — self-hosting single-plugin marketplace catalog (`source: "./"`); required because `claude plugin install` only resolves plugins through marketplaces, never a bare repo URL
- `hooks/hooks.json` — declarative `PreToolUse` hook using `${CLAUDE_PLUGIN_ROOT}`; shell-form with a `command -v python3 || command -v python` fallback chain (exec-form cannot solve interpreter naming: `python3` is absent on stock Windows, and shell-form runs under Git Bash there, which Claude Code requires)
- `skills/nb/SKILL.md` — the skill file, auto-loaded by Claude Code

The hook command uses `${CLAUDE_PLUGIN_ROOT}/scripts/nb-guard.py`, which Claude Code expands to the plugin's installation directory at runtime. No settings.json patching is required.

Scripts are installed into a versioned cache directory under `~/.claude/plugins/cache/`. The exact path is recorded in `~/.claude/plugins/installed_plugins.json` under the `installPath` key for the `nb@*` entry. The SKILL.md resolves this dynamically at runtime via a `python3 -c` lookup — do not hardcode the path.

## TDD Documents

`TDD.md` — spec for nb-guard.py, nb-read.py safe/outline/outputs, nb-write.py create/locking.

`TDD_INDEX.md` — spec for nb-index.py (§0–§14) and nb-search.py (§12–§15), with full schema, staleness algorithm, symbol extraction regexes, output pipeline, and section hierarchy.

`TDD_AUDIT.md` / `TDD_INDEX_AUDIT.md` / `TDD_INDEX_GAPS.md` — adversarial review findings already resolved in the TDD documents.


## Review findings (2026-06-10/11) — CLOSED

An adversarial review catalogued 74 findings across all five scripts, SKILL.md,
and hooks.json. **All are resolved** — fixed with tests, or accepted with
rationale (see the gaps section below). The full itemised catalogue lives in
git history (commits 27dd570..af304ee, the five `fix:` commits); do not
re-report these. Highlights of what changed: the PreToolUse guard actually
blocks now (exit 2 + stderr, real tool schemas, NotebookEdit covered,
case-insensitive); search is git-root-aware with staleness/dedup/scope
correctness; `--outline` is index-backed and `--outputs` renders placeholders
+ truncation; writes are serialised cross-platform (fcntl/msvcrt), indexing is
synchronous, non-UTF-8 source is a hard error, no-op patches don't destroy
outputs; worktree/submodule `.git` files are recognised as repo roots.

### Harness-assumption vetting (2026-06-11, against code.claude.com docs + live schemas)

Claude Code behaviors the plugin depends on, and how each was verified:

| Assumption | Status |
|---|---|
| PreToolUse exit 2 blocks + stderr fed to Claude; exit 1 non-blocking (guard) | **Verified** — quoted verbatim in hooks docs |
| Shell-form hooks run under bash (POSIX) / Git Bash (Windows); `${CLAUDE_PLUGIN_ROOT}` expands in commands | **Verified** — hooks/plugins references. Exec form is documented too, but cannot express interpreter fallback; `python3`-on-Windows hook failure is a known ecosystem issue (claude-plugins-official #85), hence shell-form with `command -v` chain |
| Env vars do not persist across Bash tool calls (SKILL.md `NB_SCRIPTS` guidance) | **Verified** — tools reference: "An export in one command will not be available in the next" |
| Pipe-separated hook matchers incl. `NotebookEdit`; `"if"` filter field | **Verified** — plugins/hooks references |
| `allowed-tools` format (skills frontmatter) | **Partially documented** — "space- or comma-separated string, or a YAML list"; in-paren example form is `Bash(git add *)`. Current frontmatter uses that form. |
| NotebookEdit payload field is `notebook_path` (guard) | **Verified 2026-06-11** — live tool schema: `notebook_path` is a required property |
| MultiEdit payload = one top-level `file_path` + `edits[]` (guard) | **Unverifiable** — MultiEdit is absent from current Claude Code toolsets (not in docs, not loadable via tool search); the guard's handling is legacy compat for older versions and fails open |

### Documented-discretionary gaps (vetted 2026-06-11 against nbformat/JEP-62 and Python docs — accepted, not bugs)

- **Duplicate cell ids: warn, never repair.** The nbformat spec requires unique ids in written notebooks, but JEP-62 leaves repair to the tool's discretion; "match the file" (minimal-diff surgical editing) was the chosen policy. Revisit only if a downstream consumer rejects the warned files.
- **NFS caveat on locking:** where `flock` is emulated via `fcntl()` (some NFS mounts), cross-host exclusion is not guaranteed and closing any fd to the file releases the lock. Each process opens its `.nblock` exactly once and all close-without-unlock paths exit immediately, so the designed degradation (warned skip / process-exit release) covers it.

- **mtime+size freshness (nb-read outline, nb-search):** same-size writes within mtime granularity can read as fresh. Accepted for read/search speed (consistent fast-path decision across both); the indexer's §A3 hash remains the authoritative rebuild check.
- **Spoofable synthetic lines / `│ ` prefix ambiguity:** output text identical to a placeholder/truncation-marker (or source beginning `│ `) is byte-indistinguishable from the synthetic line. Inherent without an escaping scheme that would break the compact format; SKILL.md Rule 9 warns consumers. Revisit only if an injection incident occurs.
- **Triple-quoted-string false positives in symbol extraction:** `def`/`=` at column 0 inside docstrings index phantom symbols. Fixing requires a real parser; regex extraction is the deliberate trade-off.
- **Symlinked directories not followed in search walks** (`followlinks=False`, except explicitly-yielded `.nb_index` symlinks): deliberate security stance against walk cycles/escapes.
- **Colon location strings in symbols.json:** analysed 2026-06-11 — `rsplit(":", 1)` + int validation cannot mis-parse because keys always end in `.ipynb` (never `:<digits>`); documented at the construction site.
