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

## TODO — open findings from 2026-06-10 review

Catalogue of known limitations/faults from an adversarial code review. Severity reflects practical impact. Remove items as they are fixed (and bump the plugin version so installed copies update).

### Critical — guard is currently ineffective

- [x] **Guard uses exit 1 + stdout; PreToolUse blocking requires exit 2 + stderr** (`nb-guard.py:148-156`). As shipped, the guard blocks nothing — the tool call proceeds and Claude never sees the redirect message.
- [x] **MultiEdit detection uses a nonexistent schema** (`nb-guard.py:95-100`). It looks for `file_path` inside `edits[]`, but real MultiEdit payloads have one top-level `file_path`; the branch returns `""` and exits early, so MultiEdit is never detected even with the exit code fixed.
- [x] **`hooks/hooks.json` hardcodes `python3`** — absent on standard Windows installs, so the hook errors and the guard never runs there. Use the exec-form `command`/`args` pattern (as lean-build does) with a portable interpreter.
- [x] **`NotebookEdit` bypasses everything**: not in the hook matcher and explicitly fail-opened by `KNOWN_TOOLS` (`nb-guard.py:136-138`). Native notebook edits skip nb-write and auto-indexing, so indexes silently go stale.
- [x] **Case-sensitive `.ipynb` checks** (`nb-guard.py:124`, `nb-read.py:404`): `foo.IPYNB` bypasses the guard *and* cannot be read by nb-read — both unguarded and unusable.
- [x] Guard docstring stale: claims invocation is "written into settings.json by install.py" (`nb-guard.py:13`); the plugin uses declarative hooks.json.
- [x] `Write` to a *new* notebook is redirected to `patch` (`nb-guard.py:151-154`); the correct suggestion is `create`.

### High — SKILL.md interface gaps

- [ ] **SKILL.md never mentions `nb-search.py`, `nb-index.py`, `--outline`, or `--outputs`** — Claude cannot discover them and falls back to grep/cat on raw JSON, the exact token blowup the plugin prevents. The "Notes" section even says outputs are "not shown" with no sanctioned alternative.
- [ ] **`allowed-tools` frontmatter likely malformed**: space-separated `Bash(python3 *) Bash(python *)` instead of comma-separated `Bash(cmd:*)` entries; also no `Bash(py *)` despite instructing Windows users to run `py -3`.
- [ ] **Documented output format is stale**: shows `[0:code]` headers but code emits `[0:code:run=1]`; shows `[cell has 2 output(s) … not shown]` but code emits `│ ── (2 outputs, 5 lines) ──`.
- [ ] The output-summary line carries the same `│ ` prefix as source lines — prefix-stripping consumers absorb it as a fake source line, and genuine source starting with `│ ` is ambiguous after stripping.
- [ ] The `python3 -c` install-path lookup raises bare `StopIteration` for dev checkouts not in `installed_plugins.json`, picks arbitrarily if two marketplaces ship an `nb` plugin, and is duplicated 6× in the file. No failure guidance anywhere in SKILL.md.
- [ ] No SKILL.md guidance on concurrency (lock contention, stale `.nblock`) or what to do when a script fails.
- [ ] This file says SKILL.md defines "9 behavioural rules"; it defines 8.

### High — search returns silently wrong results

- [ ] **Searching from a repo subdirectory finds nothing** (`nb-search.py:155-175`): indexes live at `<git-root>/.nb_index/`, above the search root, so the walk never reaches them — exit 1, "no matches".
- [ ] **Relative `notebook_path` resolved against search root, not git root** (`nb-search.py:214`): searching from a parent of several repos resolves every path wrong and silently excludes the notebooks as unsafe.
- [ ] **`--symbol`/`--import` modes never check staleness** — only keyword mode calls `_check_staleness`; stale results print with no `[STALE]` warning.
- [ ] **Unindexed notebooks excluded from keyword results** (`nb-search.py:409-423`) even though keyword mode opens the `.ipynb` anyway; `[UNINDEXED]` goes to stderr only. In symbol/import modes they are invisible with no warning at all.
- [ ] symbols.json fast path drops `--type`/`--section` filtering when the per-notebook index is missing/unreadable (`nb-search.py:535-553, 663-677`) — results that should be excluded are included.
- [ ] Inconsistent case sensitivity: keyword search case-insensitive, symbol/import exact-match only; undocumented.
- [x] `MAX_FILE_SIZE` (`nb-search.py:33`) defined but never enforced — violates the 100 MB invariant; keyword mode `json.load`s and staleness hashing reads every notebook in full.
- [ ] Staleness "fast path" comment is false (`nb-search.py:136-148`): the full notebook is hashed even when mtime+size match, making every keyword search O(total notebook bytes).
- [x] `.ipynb_checkpoints` not in `SKIP_DIRS` — Jupyter checkpoint copies generate `[UNINDEXED]` noise and potential duplicate hits.
- [x] Unguarded `c["i"]` access crashes the whole search on a malformed-but-parseable index file (`nb-search.py:434, 604, 730`); corrupt notebooks in pass 3 are skipped with no signal (`:428-431`).
- [x] `--limit 0`/negative behaves inconsistently across modes (`nb-search.py:477, 557-561`); no argparse validation.
- [x] `display` path printed unsanitised in results (`nb-search.py:253-256`) — breaks the sanitise-before-echo invariant.
- [ ] Symlinked directories never followed (`followlinks=False`), no override flag, undocumented.

### High — write/index pipeline reliability

- [ ] **Index failures are invisible** (`nb-write.py:449-457`): nb-index.py spawned fire-and-forget, both stdio → DEVNULL, exit code never collected — symbols.json silently diverges with no retry.
- [ ] **symbols.json updates silently dropped under contention** (`nb-index.py:601-607`): `LOCK_EX | LOCK_NB` returns silently if the lock is held; rapid consecutive writes routinely lose symbol updates.
- [ ] **Lock-file unlink race** (`nb-index.py:688-693`): deleting `symbols.nblock` after release lets two processes both hold "the" lock (different inodes) and clobber symbols.json.
- [ ] **Windows lost-update**: no `fcntl` → read-modify-write unserialised (`nb-write.py:53-54`); the second of two concurrent writes silently discards the first. Atomic rename masks the loss.
- [ ] **`patch` unconditionally destroys outputs/execution_count** (`nb-write.py:351-353`) even for a no-op patch — with no `.bak`/undo, one mistaken patch on an untracked notebook irreversibly loses computed outputs.
- [ ] Cell `id` vs nbformat_minor: `make_cell` always emits `id` but insert/patch never bump `nbformat_minor` (`nb-write.py:261-271`) — inserting into a 4.0–4.4 notebook fails strict validation; pre-existing duplicate ids never detected.
- [ ] Exclusive `.nblock` acquired *before* `read_source()` (`nb-write.py:409,415`): a hung stdin producer holds the lock indefinitely, blocking all other writers.
- [ ] latin-1 fallback for `-f` source writes mojibake with only a stderr warning and exit 0 (`nb-write.py:243-251`); inconsistent with nb-write rejecting non-UTF-8 notebooks while nb-index accepts them.
- [ ] Per-notebook index write takes no lock; stat→`os.replace` window (`nb-index.py:917-976`) lets a stale index clobber a fresher one.
- [ ] symbols.json has no garbage collection — deleted/renamed notebooks pollute symbol search forever.
- [ ] `.nblock` files never unlinked — litter working trees. (Gitignore entry now added by `_update_gitignore`; unlink cleanup still open.)
- [x] `except OSError` around `os.link` swallows `FileExistsError`, making the dedicated handler dead code and degrading to a TOCTOU-racy fallback (`nb-write.py:314-326`).
- [ ] 100 MB limit enforced only on load (`nb-write.py:146-149`): a patch can grow the file past the limit, after which no nb-write/nb-index operation can touch it again.
- [ ] `nb["cells"]` checked for presence, not type — malformed notebooks produce raw tracebacks (`nb-write.py:274-278`).

### Medium — rendering correctness (nb-read.py)

- [x] **Truncation warnings carry no cell index and go to stderr** (`nb-read.py:222-225`) — SKILL.md Rule 5 ("re-read truncated cells before patching") is unfollowable; patch-after-truncation source loss is plausible.
- [ ] **Documented index-backed read path not implemented**: the data-flow section above claims `--outline`/`--outputs` read from the index, but nb-read.py unconditionally `json.load`s the full notebook first and `--outputs` never consults the index — zero I/O savings.
- [ ] **Image/HTML/JSON-only outputs vanish silently in `--outputs`** (`nb-read.py:274-297`): `_render_output_block` returns None and nothing prints — "no output" indistinguishable from "plot exists".
- [ ] **`--outputs` applies no truncation** (`nb-read.py:306-320`): a 100k-line stream output prints in full; `--truncate` only affects source. Very wide single lines never wrapped/capped.
- [ ] Output summary reads `out["text"]` instead of `data["text/plain"]` (`nb-read.py:241-253`) — execute_results report "0 lines"; traceback line count uses list length, not actual lines; summary disagrees with `--outputs` for the same cell.
- [ ] Safe mode strips ANSI from source but not C0 controls (`nb-read.py:213-214`) — raw BEL/backspace/`\r` in source pass through, weakening the documented sanitisation invariant.
- [ ] Outline mode trusts index structure: cells missing `"i"` raise uncaught `KeyError` instead of falling back (`nb-read.py:454-455`); freshness check validates mtime/size only, not schema.
- [ ] Freshness is mtime+size only (`nb-read.py:129-137`): same-size writes within mtime granularity (or `cp -p`/git checkout) yield falsely-fresh outlines with no `[STALE INDEX]` warning.
- [ ] `_extract_output_text` joins parts with `""` (`nb-read.py:299`) — outputs without trailing newlines glue onto one line.
- [ ] `MAX_RANGE_SIZE = 10_000` rejects valid `--cells` ranges on genuinely huge notebooks, no override (`nb-read.py:159-161`).
- [ ] Markdown `attachments` (embedded images) and raw-cell mimetypes never rendered or summarised.

### Medium — indexing quality (nb-index.py)

- [ ] **Git worktrees and submodules not recognised** (`.git`-as-file rejected, `nb-index.py:146`): submodule notebooks index under the superproject; worktrees fall back to per-directory `.nb_index` — same notebook indexed in different places.
- [x] R-kernel check is a substring test (`"r" in lang and "ir" not in lang`, `nb-index.py:434`) — Rust/Ruby/Perl/Erlang kernels misclassified as R, garbage symbols indexed; the `ir` exclusion excludes nothing useful (IRkernel's language is `"R"`).
- [ ] Python symbol extraction misses `async def`, tuple assignment, `import a, b` beyond the first module, `import x as y` aliases, and annotations containing `.`/`|`/quotes (`nb-index.py:74-77`); false positives for `def`/`=` at column 0 inside triple-quoted strings.
- [ ] ReDoS guard drops lines > 500 chars entirely (`nb-index.py:109-118`) — symbols on long generated lines silently vanish.
- [x] mtime+size match with unreadable file reports the index **fresh** (`nb-index.py:297-300`).
- [ ] `_update_gitignore` is a non-atomic unlocked read-modify-write of the repo's `.gitignore` (`nb-index.py:202-225`).
- [ ] symbols.json location strings use `:` separator, ambiguous when paths contain colons (`nb-index.py:569-580`).
- [ ] >20 directory levels, symlinked `.git`, or st_dev boundary silently fragments to per-directory `.nb_index` with no warning (`nb-index.py:139-162`).

### Suggested fix order

1. Guard: exit 2 + stderr, top-level `file_path` for MultiEdit, exec-form hook command, add `NotebookEdit` to matcher, case-insensitive suffix check.
2. SKILL.md: document `--outline`/`--outputs`/`nb-search`, fix `allowed-tools`, update output-format examples.
3. nb-read: cell index in truncation warnings; placeholder block for non-text outputs; truncate `--outputs`.
4. Search: index discovery from subdirectories, staleness checks in symbol/import modes, include unindexed notebooks in keyword results.
5. Write/index: surface nb-index failures, fix symbols.json locking, gitignore + clean up `.nblock`.
