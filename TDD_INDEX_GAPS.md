# TDD_INDEX Gap Analysis

Findings from systematic cross-referencing of TDD_INDEX.md against existing scripts,
tests, and implementation requirements. Gaps are classified by severity and each has
a proposed resolution that will be applied directly to TDD_INDEX.md.

---

## CRITICAL — Blocks correct implementation

### G1 — `first_line` not in index schema; `_derive_outline` cannot work from index alone

**Location:** §4 + Schema  
**Problem:** `_derive_outline(cells)` is specified to return `{"line": "first source line…"}`
for each cell. But the schema stores only `source_hash` (MD5[:8]) — not source text or any
first-line extract. §9.2 mandates "Must NOT open the .ipynb file when a fresh index exists"
for `--outline` mode. These two constraints are contradictory — outline requires source
content, but the index has none.  
**Resolution:** Add `"first_line"` (str, max 120 chars, stripped, ANSI-sanitised) to the
per-cell schema entry. Set at index build time. `_derive_outline` reads it from the index
cell dict. Notebook-fallback path (`_derive_outline_from_nb`) reads source directly and
truncates to 120 chars.

---

### G2 — §12.1 keyword search mechanism completely unspecified

**Location:** §12.1  
**Problem:** `nb-search.py "process" /path` is specified to "find all cells whose source
contains 'process'" but the index stores only `source_hash`, `symbols_defined`, and
`symbols_imported` — not source text. There is no mechanism in the spec for keyword
search to find matching cells without opening notebook files.  
**Resolution:** Explicitly spec that keyword search (bare query without `--symbol` or
`--import`) opens the `.ipynb` files for each indexed notebook and scans source text
directly. The index is used only to locate which notebooks exist in the project and
to supply metadata (section, exec, status) for the result lines. Add §12.13: keyword
search opens the notebook file; `--symbol` and `--import` use only the index.

---

## HIGH — Will cause incorrect or unimplementable code

### G3 — nb-index.py CLI interface completely absent

**Location:** Entire TDD  
**Problem:** The spec defines behaviour for `nb-index.py` across §1–§8, §13–§14 but
never specifies the command-line interface: usage line, positional args, optional flags
(`--force`, `--verbose`?), exit code table, or stdout/stderr contract.  
**Resolution:** Add §0 — nb-index.py CLI:
```
Usage: python3 nb-index.py <notebook.ipynb> [--force]

Arguments:
  <notebook.ipynb>  Path to notebook to index.
  --force           Always rebuild even if index is fresh.

Exit codes:
  0 — index written or already fresh (success)
  1 — unrecoverable error (bad path, containment violation, I/O error)

stdout: silent
stderr: status messages on success; error messages on failure
```

---

### G4 — nb-read.py index discovery logic never specified

**Location:** §9, §10, §11  
**Problem:** §9.2 says nb-read.py "Must NOT open the .ipynb file when a fresh index
exists" and §9.3 says it "falls back to notebook when index absent." But HOW nb-read.py
finds the index is never specified:
- Does it call the same `_find_index_dir()` / `_index_file_path()` logic as nb-index.py?
- Is that logic duplicated or in a shared module?
- Where is the staleness check performed in nb-read.py?  
**Resolution:** Add §15 — Shared Index Discovery. Specify that `_find_index_dir()` and
`_index_file_path()` are defined in `nb-index.py` and re-implemented identically in
`nb-read.py` and `nb-search.py` (stdlib-only, no import between scripts). nb-read.py
performs the three-signal staleness check before deciding which path to take.

---

### G5 — Stale index handling in nb-read.py unspecified

**Location:** §9, §10, §11  
**Problem:** §9.2 covers "fresh index" and §9.3 covers "index absent" but the case
"index exists and is stale" is never handled. Three plausible behaviours exist:
(a) use stale index (fast, possibly wrong), (b) fall back to notebook (safe, slower),
(c) trigger synchronous rebuild (slow).  
**Resolution:** Add §9.4: when the index exists but is stale, nb-read.py falls back
to reading the notebook directly (same as absent). A `[STALE INDEX]` warning is printed
to stderr. No synchronous rebuild is triggered (that is nb-index.py's job). Same rule
applies to §10 and §11.

---

### G6 — nb_index_script_abs discovery in nb-write.py never specified

**Location:** §8, A6  
**Problem:** A6 states "nb_index_script_abs is the absolute path of nb-index.py in the
same scripts/ directory as nb-write.py (resolved at import time, not at call time)" but
never shows the code pattern to achieve this. Implementations will vary.  
**Resolution:** Add explicit code pattern in A6:
```python
_NB_INDEX_SCRIPT = Path(__file__).parent.resolve() / "nb-index.py"
```
Evaluated at module import time. If the file does not exist at that path, the Popen
call logs a warning to stderr and returns without error.

---

### G7 — install.py and uninstall.py not in Modified Files table

**Location:** "Modified files" table at end of TDD  
**Problem:** `scripts/nb-index.py` and `scripts/nb-search.py` are new scripts that must
be installed to `~/.claude/skills/nb/scripts/`. The "New files" table lists them but
`install.py` is never listed as a file that needs modification to copy them.  
**Resolution:** Add `install.py` and `uninstall.py` to the Modified Files table with
note "Copy nb-index.py and nb-search.py to scripts_dst; make executable on POSIX."

---

## MEDIUM — Causes bugs or test failures if not addressed

### G8 — `display_data` output type not handled in §7

**Location:** §7, A4, Schema  
**Problem:** A4 lists `stream`, `execute_result`, and `error` as handled output types.
`display_data` is a fourth valid Jupyter output type with the same structure as
`execute_result` (`data: {"text/plain": "..."}`) and is commonly produced by matplotlib,
rich, etc. It is not mentioned anywhere in §7 or A4.  
**Resolution:** Add `display_data` to the list of handled types in A4. Add test §7.14:
`display_data` with `text/plain` is stored as output text.

---

### G9 — §5.3 "same or higher level" is ambiguous

**Location:** §5.3  
**Problem:** "Section spans to next heading of same or higher level" — h1 is level 1,
h2 is level 2. Is level 1 "higher" (bigger) or "lower" (fewer #'s, semantically higher)?
In HTML/Markdown, h1 is a higher-ranking heading with a smaller level number. The phrase
is ambiguous and will cause inconsistent implementations.  
**Resolution:** Replace §5.3 with: "Section spans from its heading cell to the cell
immediately before the next heading whose level number is ≤ the current section's
level number (i.e., a heading of equal or greater semantic rank closes the section).
The last section extends to the end of the notebook."

---

### G10 — §9 outline format and §11 regular header format are inconsistent

**Location:** §9 (outline spec), §11 (normal cell header spec)  
**Problem:** §9 example shows: `[0 :code:1   ] import pandas as pd`  
§11 spec shows: `[3:code:run=5] ──────────────────────────────────`  
Two different formats for displaying execution count — `exec` number as third field vs
`run=N` prefix. One has fixed-width padding, one does not. These must be the same or
explicitly different.  
**Resolution:** Unify. Normal cell header (§11) uses `[N:type:run=N]`. Outline (§9) also
uses `run=N` form. The `──` bar is absent in outline mode (one line per cell, no bar).
Update the §9 example to match.

---

### G11 — §11 header format change breaks 100+ existing test assertions

**Location:** §11, tests/test_read_independent.py, tests/test_scripts.py  
**Problem:** Current nb-read.py header format: `[0:code] ────────────────────`  
After §11: `[0:code:run=——] ────────────────────` (code cells gain `run=N` field)  
Markdown cells currently: `[1:markdown] ────────────` → `[1:markdown] ────────────` (unchanged)  
Every test in test_read_independent.py that asserts `"[0:code]"` will fail.  
**Resolution:** Note in §11 that the existing test files `test_read_independent.py` and
`test_scripts.py` must be updated when nb-read.py is modified. List the exact format
change: code cells gain `:run=N` (or `:run=——`); markdown/raw cells gain nothing.

---

### G12 — symbols.json write atomicity and concurrent access unspecified

**Location:** §13.2  
**Problem:** §13.2 says symbols.json is "updated incrementally when a notebook is
re-indexed" but the write pattern is unspecified. Two concurrent indexers for different
notebooks in the same project both update symbols.json — last-write-wins may drop entries.
No atomic write pattern is specified (unlike notebook writes which use temp+rename).  
**Resolution:** Add §13.6: symbols.json is written atomically using the same
temp-file+fsync+os.replace pattern as notebook writes. For concurrent multi-notebook
indexing: acquire the same companion lock file (`.nb_index/symbols.nblock`) before
reading and writing symbols.json. If the lock is unavailable (non-blocking try),
skip the symbols.json update silently — nb-search falls back to serial scan (§13.3).

---

### G13 — §12.2 `--symbol` fast path via symbols.json not specified

**Location:** §12.2  
**Problem:** §13 creates a project-level symbols.json exactly to enable O(1) symbol
lookup in nb-search. But §12.2 `--symbol` mode says nothing about using symbols.json —
it just says "finds cells where symbol_index contains the queried name." If nb-search
always does per-notebook index scanning, symbols.json provides no benefit.  
**Resolution:** Add to §12.2: when `symbols.json` is present and fresh (its
`generated_at` is newer than all per-notebook index mtimes in the project), use it
for O(1) lookup. Fall back to serial per-notebook scan when absent or stale.

---

### G14 — §4.6 status derivation: index-stored vs. derived inconsistency

**Location:** §4.6, Schema  
**Problem:** §4.6 specifies `_derive_outline()` logic for computing `status` from exec
count and outputs. But the schema shows `"status": "ok"` as a STORED field in the cell
entry. The status is computed at INDEX BUILD TIME and stored — not re-derived from
outputs at outline-derivation time. §4.6 misleadingly implies it is derived at read time.
When using the index path, status comes from the stored field; when falling back to
notebook, status must be derived from the cell's `execution_count` and `outputs`.  
**Resolution:** Clarify §4.6: "When reading from index, `status` comes from the stored
`cells[i].status` field. When deriving outline from raw notebook (fallback path), compute
status from: exec not null + no error outputs → 'ok'; exec not null + error outputs →
'error'; exec null → 'not_run'."

---

### G15 — §7: mixed text + binary output cell behavior unspecified

**Location:** §7.4  
**Problem:** §7.4 says binary outputs → `has_output: true`, type in `output_types`, no
`output_text` key. But a cell might have both binary outputs AND text outputs (e.g.,
`print("hello")` followed by `plt.show()` which emits `image/png`). The spec does not
say whether to store the text portion and set `has_output: true` alongside the binary
type, or to omit `output_text` because any binary is present.  
**Resolution:** Add §7.15: Text and binary outputs are processed independently. If a
cell has any text outputs (stream, execute_result, error, display_data with text/plain),
the combined text is stored in `output_text` subject to the 4 KB cap. Binary outputs
add their type to `output_types` but do not affect `output_text`. A cell with one stream
output and one image/png output will have both `output_text` and `image/png` in
`output_types`.

---

## LOW — Minor inconsistencies and missing detail

### G16 — §13.2 fast-path inconsistency: should symbols.json be validated for version?

**Location:** §13  
**Problem:** The per-notebook index has `version: 1` and a version compatibility spec.
symbols.json has `version: 1` but no compatibility rules. If nb-search loads a
symbols.json from a future version, it should fall back gracefully.  
**Resolution:** Add to §13: version compatibility follows the same rule as per-notebook
indices — `version > 1` → skip (warn), fall back to serial scan. `version` missing or
non-integer → treat as corrupt, rebuild.

### G17 — Python `type` alias (Python 3.12+) not addressed in A5

**Location:** §A5, §6  
**Problem:** TDD_INDEX_AUDIT.md H7 specifically listed "`type` aliases" as undefined.
`type Vector = list[float]` (PEP 695) would match ASSIGN_RE (`^(\w+)\s*...=...`) via
`type` → `"type"` as the captured name, which is wrong. This is a Python keyword
since 3.12 that should be excluded.  
**Resolution:** Add to A5 Python section: "ASSIGN_RE excludes the `type` soft-keyword:
add negative lookahead `(?!type\s+\w+\s*=)` before the capture group, or
post-filter: remove `'type'` from `symbols_defined` results."

### G18 — `indexed_at` ISO 8601 generation not specified for Python 3.8 compat

**Location:** Schema  
**Problem:** Python 3.8/3.9 `datetime.now(timezone.utc).isoformat()` produces
`+00:00` suffix, not `Z`. Python 3.11+ has `datetime.now(UTC).isoformat()`. The spec
says "ISO 8601 UTC" with `Z` suffix in the example but doesn't specify the generation
pattern.  
**Resolution:** Add to schema spec: use
`datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")` for Python 3.8 compatibility
(avoids timezone import inconsistencies).

### G19 — `notebook_path` field: relative vs absolute not consistently defined

**Location:** Schema  
**Problem:** Schema comment says "relative to git root, or absolute" but doesn't specify
the algorithm. When a git root is found, path should be relative to git root. When no
git root, it should be the resolved absolute path. nb-search uses this to find the
notebook file for keyword search (G2 resolution), so the spec must be precise.  
**Resolution:** Tighten the schema comment: "When git root was found: POSIX-style
relative path from git root (e.g. `data/explore.ipynb`). When no git root: absolute
resolved path (e.g. `/home/user/standalone/nb.ipynb`). Always forward slashes even
on Windows (for cross-platform index portability)."

### G20 — §3.6 `--force` mentioned only in tests; not in CLI spec

**Location:** §3.6  
**Problem:** §3.6 is a test ("inode changes after --force") that implies `--force` is
a CLI flag, but without the CLI spec (G3 gap), there's no formal definition.  
Resolved by G3 (CLI spec adds `--force`). Noted here for cross-reference only.

---

## Resolution Summary

| Gap | Severity | Action |
|-----|----------|--------|
| G1 | CRITICAL | Add `first_line` to per-cell schema |
| G2 | CRITICAL | Spec that keyword search opens notebook files |
| G3 | HIGH | Add §0 — nb-index.py CLI |
| G4 | HIGH | Add §15 — shared index discovery logic |
| G5 | HIGH | Add §9.4 — stale index falls back to notebook |
| G6 | HIGH | Add `_NB_INDEX_SCRIPT` pattern to A6 |
| G7 | HIGH | Add install.py to Modified Files table |
| G8 | MEDIUM | Add `display_data` to §7 and A4 |
| G9 | MEDIUM | Rewrite §5.3 with unambiguous level comparison |
| G10 | MEDIUM | Unify outline and normal header format |
| G11 | MEDIUM | Note test update requirement in §11 |
| G12 | MEDIUM | Add §13.6 — symbols.json atomic write + lock |
| G13 | MEDIUM | Add symbols.json fast path to §12.2 |
| G14 | MEDIUM | Clarify §4.6 index-stored vs derived status |
| G15 | MEDIUM | Add §7.15 — mixed text+binary output cell |
| G16 | LOW | Add symbols.json version compatibility |
| G17 | LOW | Exclude `type` soft-keyword from ASSIGN_RE |
| G18 | LOW | Specify `indexed_at` generation pattern |
| G19 | LOW | Tighten `notebook_path` relative/absolute spec |
| G20 | LOW | Covered by G3 |
