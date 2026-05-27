# TDD — nb-index: Persistent RAG Index for Jupyter Notebooks

*Revised after three-agent adversarial audit (security · architecture · performance).*
*See TDD_INDEX_AUDIT.md for the full finding catalogue.*
*Gap-analysis pass applied — see TDD_INDEX_GAPS.md for the 20-item gap catalogue.*

---

## Purpose

Extend the nb skill with a persistent, session-spanning index that enables
retrieval-augmented reading of notebooks:

- Instant outline without reading the full `.ipynb`
- Stored cell outputs that survive across sessions (4 KB cap per cell)
- Symbol and import lookup across all indexed notebooks
- Section/heading-aware navigation
- Auto-updated after every write; auto-gitignored at project root

---

## Architecture Decisions

### A1 — Index location

```
nb-index.py resolves the notebook path with Path.resolve() first, then
walks up from the notebook's parent directory looking for a .git/ entry.

Walk constraints:
  - Maximum 20 directory levels upward
  - Stop if os.stat().st_dev changes between levels (filesystem boundary)
  - Never follow symlinks during the walk (Path.parents operates on
    lexical path strings)
  - .git detection: a directory qualifies as a git root ONLY when
    `(d / '.git').is_dir() and not (d / '.git').is_symlink()`.
    A .git symlink (even one pointing to a real git dir) is skipped;
    walk continues upward. `os.path.lexists()` must NOT be used —
    it returns True for dangling symlinks, allowing an attacker to
    forge a git root by placing a .git symlink anywhere on the path.

If found:  <git-root>/.nb_index/<relative-path-to-notebook>.json
           e.g. project/.nb_index/data/exploration.ipynb.json

If not (walk exhausted or hit boundary):
           <notebook-dir>/.nb_index/<notebook-basename>.json
           e.g. standalone/.nb_index/analysis.ipynb.json

After constructing the candidate index path, verify containment:
  assert Path(index_path).resolve().is_relative_to(
      Path(index_dir).resolve()
  )
Abort with exit 1 if the assertion fails.
```

### A2 — .gitignore management

On first creation of a `.nb_index/` directory the indexer appends
`.nb_index/` to the `.gitignore` at the same level (git root or notebook
directory). Rules:

- If `.gitignore` is a symlink: print a warning to stderr and skip the
  gitignore update. Never write through symlinks.
- If the directory is read-only or the write fails: print a non-fatal
  warning to stderr and continue. Indexing proceeds regardless.
- If the entry already exists: do not duplicate it.
- If no `.gitignore` exists: create one containing `.nb_index/`.
- **The string written is always the literal `.nb_index/`**, never a
  path computed from the notebook location. This prevents newline
  injection if the notebook directory path contains newline characters.

### A3 — Staleness detection

The index stores three staleness signals:

```json
"notebook_mtime":    1748354400.123,   // float: os.path.getmtime()
"notebook_size":     28672,            // int:   os.path.getsize()
"nb_content_hash":   "a3f2c1d4e5b6c7d8"  // str: first 16 hex chars of SHA-256
                                           //      of raw notebook bytes
                                           // (SHA-256[:16] = 64-bit hash space,
                                           //  not MD5[:8] which is only 32-bit
                                           //  and brute-forceable in < 1 second)
```

Staleness check (in order, cheapest first):

1. If index file is missing → stale (rebuild).
2. If stored `notebook_mtime` != current mtime → stale (rebuild).
3. If stored `notebook_size` != current size → stale (rebuild).
4. If stored `nb_content_hash` != `hashlib.sha256(raw).hexdigest()[:16]` →
   stale (rebuild).
5. All match → fresh (skip rebuild).

mtime is a cheap pre-filter. The hash is the authoritative freshness
check and handles FAT32/NFS/Docker volume edge cases where mtime is
unreliable.

### A4 — Output storage

`stream`, `execute_result`, `error`, and `display_data` outputs are
serialised to plain text and stored in the index. Per-cell cap:
**4096 bytes** of UTF-8 text.

Exact processing pipeline — implementations must follow this order:

1. Concatenate all text outputs for the cell in order.
2. Strip null bytes (`\x00`).
3. Replace lone surrogate code points with `�`.
4. **ANSI-sanitise:** apply the same `_ANSI_RE` + `_CTRL_RE` stripping
   used for `first_line`. This prevents terminal-hijack sequences
   (e.g. xterm title injection `\x1b]0;...\x07`, alternate screen
   `\x1b[?1049h`) stored in cell output from reaching the terminal
   when `nb-search.py` prints results or the index is consumed by
   external tools.
5. Apply 4096-byte UTF-8 cap to the **fully processed** string:
   - If ≤ 4096 bytes: store verbatim, `output_truncated: false`.
   - If > 4096 bytes and a complete line fits before byte 4096: store
     up to and including the last such line, `output_truncated: true`.
   - If no complete line fits (first line ≥ 4096 bytes): store the first
     4096 bytes hard-truncated with suffix `\n[truncated mid-line]`,
     `output_truncated: true`.

Binary outputs (`image/png`, `application/json`, etc.) are not stored;
`has_output: true` and the type in `output_types` are still recorded.

Text and binary outputs in the same cell are processed independently:
if any text outputs are present, their combined text is stored in
`output_text` subject to the 4 KB cap. Binary outputs add their MIME
type to `output_types` regardless.

All index JSON is written with `ensure_ascii=False` to avoid 6× size
inflation on Unicode/CJK output text.

### A5 — Symbol extraction

Extraction is regex-based (no AST dependency, works for all kernel
languages). Patterns are compiled once at module level. Every line
exceeding **500 characters** is skipped before applying any pattern
(ReDoS protection — a 10k-char line with no closing delimiter cannot
cause catastrophic backtracking if it is never passed to the regex
engine).

**Python** (kernel language contains "python"):

```python
# Defined symbols
DEF_RE    = re.compile(r'^def\s+(\w+)\s*\(', re.MULTILINE)
CLASS_RE  = re.compile(r'^class\s+(\w+)\s*[:\(]', re.MULTILINE)
ASSIGN_RE = re.compile(r'^(\w+)\s*(?::[\w\[\], ]{0,200})?\s*=(?!=)', re.MULTILINE)
# Note: ASSIGN_RE uses negative lookahead (?!=) to exclude ==
# Annotated assignment: x: int = 5  captured as "x"
# Walrus (:=) is NOT at line start so ASSIGN_RE won't match it
# The annotation subpattern is capped at 200 chars {0,200} to prevent
# catastrophic backtracking on lines like "x: int[[[[[..." of ~499 chars
# (the 500-char line skip protects long lines, but 499-char annotation
#  with no "=" causes O(n^2) backtracking in the optional group).
# Post-filter: remove the string "type" from symbols_defined results.
# (Python 3.12+ soft-keyword: `type Vector = list[float]` would otherwise
#  capture "type" as a defined symbol, which is wrong.)

# Imported symbols
IMPORT_RE = re.compile(r'^import\s+([\w.]+)', re.MULTILINE)
FROM_RE   = re.compile(r'^from\s+([\w.]+)\s+import', re.MULTILINE)
```

**Julia** (kernel language contains "julia"):

```python
JULIA_FUNC_RE   = re.compile(r'^function\s+([\w!]+)\s*\(', re.MULTILINE)
JULIA_SHORT_RE  = re.compile(r'^([\w!]+)\s*\([^)]{0,200}\)\s*=', re.MULTILINE)
JULIA_STRUCT_RE = re.compile(r'^(?:mutable\s+)?struct\s+(\w+)', re.MULTILINE)
JULIA_ASSIGN_RE = re.compile(r'^(\w+)\s*=(?!=)', re.MULTILINE)
JULIA_USING_RE  = re.compile(r'\busing\s+([\w.]+(?:\s*,\s*[\w.]+)*)', re.MULTILINE)
JULIA_IMPORT_RE = re.compile(r'\bimport\s+([\w.]+)', re.MULTILINE)
# Split JULIA_USING_RE matches on commas for multi-module: "using A, B" → ["A","B"]
```

**R** (kernel language contains "r" and not "ir" — to exclude iR):

```python
R_FUNC_RE = re.compile(r'^(\w+)\s*(?:<-|=)\s*function\s*\(', re.MULTILINE)
R_LIB_RE  = re.compile(r'(?:library|require)\s*\(\s*["\']?([\w.]+)', re.MULTILINE)
```

**Unknown language:** `symbols_defined: []`, `symbols_imported: []`,
`symbols_extracted: false`.

**Symbol name and count caps (all languages):**

```python
MAX_SYMBOL_LEN   = 256   # captured group longer than this is discarded
MAX_SYMBOLS_PER_CELL = 500  # cap per cell across all patterns combined

def _cap_symbols(names: list[str]) -> list[str]:
    return [n for n in names if len(n) <= MAX_SYMBOL_LEN][:MAX_SYMBOLS_PER_CELL]
```

Apply `_cap_symbols` to `symbols_defined` and `symbols_imported` separately
after extraction. Without these caps, a notebook cell with 10,000
single-assignment lines or one 499-char identifier becomes a DoS vector
for index size and search performance.

### A6 — Auto-index on write

`nb-write.py` spawns `nb-index.py` after every successful `save()` for
`patch`, `insert`, and `delete` operations.

The absolute path to `nb-index.py` is resolved **once at module import
time** using:

```python
_NB_INDEX_SCRIPT = (Path(__file__).parent / "nb-index.py").resolve()
```

The Popen call must be:

```python
if _NB_INDEX_SCRIPT.exists():
    subprocess.Popen(
        [sys.executable, str(_NB_INDEX_SCRIPT), str(Path(nb_path).resolve())],
        shell=False,          # NEVER shell=True — path may contain metacharacters
        close_fds=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
else:
    print(f"[warn] nb-index.py not found at {_NB_INDEX_SCRIPT}; "
          f"skipping auto-index", file=sys.stderr)
```

If `_NB_INDEX_SCRIPT` does not exist, a stderr warning is printed but
the write operation is unaffected (indexing is best-effort).

**Optimistic concurrency:** At the start of the index write, re-stat
the notebook. If its mtime/size differ from the values read at the
start of the indexer run, abort silently — a concurrent indexer started
later will write a more current index.

Note: this is a best-effort heuristic, not a hard guarantee. Multiple
concurrent indexers can all pass the re-stat check if the notebook is
not modified during the check window. The true invariant is: 'a notebook
modified during indexing may produce a transiently stale index that will
be detected and rebuilt on next access.' The lock on the notebook's
`.nblock` file (from nb-write.py) ensures sequential writes; the
optimistic check is a second-layer defence for external modifications.

**Indexing failures must not fail the write:** if the Popen call raises
or the child exits non-zero, `nb-write.py` continues normally. Indexing
is best-effort.

---

## Index File Schema (v1)

```jsonc
{
  "version": 1,                          // integer, not string
  "notebook_path": "data/explore.ipynb", // git-root-relative POSIX path when git root
                                         // found; absolute resolved path otherwise.
                                         // Always forward slashes, even on Windows.
  "indexed_at": "2025-05-27T14:00:00Z",  // ISO 8601 UTC — generated with:
                                         // datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
  "notebook_mtime": 1748354400.123,
  "notebook_size":  28672,
  "nb_content_hash": "a3f2c1d4e5b6c7d8",

  "kernel_language": "julia",
  "cell_count": 118,

  // Per-cell metadata (outline is derived from this at read time — not stored separately)
  "cells": [
    {
      "i": 0,
      "type": "code",
      "exec": 1,
      "status": "ok",              // "ok" | "error" | "not_run"
      "source_hash": "b2e1a9f3",   // MD5[:8] of cell source text
      "first_line": "import pandas as pd",  // first non-empty source line, stripped,
                                            // ANSI-sanitised, max 120 chars
      "section": "Packages",       // null if before first heading
      "symbols_defined": ["load_data", "df"],
      "symbols_imported": ["pandas", "numpy"],
      "symbols_extracted": true,
      "has_output": true,
      "output_types": ["execute_result"],  // deduplicated, order of first appearance;
                                           // valid values: stream execute_result error
                                           // display_data image/png application/json …
      "output_text": "   col_a  col_b\n0  1  2\n...",
      "output_truncated": false
    },
    {
      "i": 1,
      "type": "markdown",
      "exec": null,
      "status": null,
      "source_hash": "c4d3e2f1",
      "first_line": "## Data Loading",     // heading line stored verbatim (after sanitise)
      "section": null,
      "heading": 2,                // present only when type=markdown and first line is a heading
      "heading_text": "Data Loading",
      "symbols_defined": [],
      "symbols_imported": [],
      "symbols_extracted": false,
      "has_output": false
    }
  ],

  // Section boundaries (derived from markdown headings)
  "sections": [
    {"heading": "Data Loading", "level": 2, "first_cell": 1, "last_cell": 5},
    {"heading": "Analysis",     "level": 2, "first_cell": 6, "last_cell": 14}
  ],

  // Inverted indices for fast lookup
  "symbol_index": {
    "load_data": [0],
    "df":        [0, 3, 7]
  },
  "import_index": {
    "pandas": [0],
    "numpy":  [0]
  }
}
```

**Schema compatibility rules:**
- Reading code checks `version` (must be int) first.
- `version > 1` (future format) → log warning on stderr, skip file,
  do not use.
- Missing or non-integer `version` → treat as corrupt, trigger rebuild.
- Readers must tolerate unknown top-level keys (additionalProperties-
  permissive) to allow forward-compatible additions.

---

## §0 — nb-index.py CLI

```
Usage: python3 nb-index.py <notebook.ipynb> [--force]

Arguments:
  <notebook.ipynb>  Resolved path to the notebook to index.
                    Must end with .ipynb and must not be a symlink.
  --force           Rebuild the index even if it is already fresh.

Exit codes:
  0  Index written, already fresh, or best-effort success (gitignore
     write failed non-fatally — still exits 0).
  1  Unrecoverable error: bad path, containment assertion failed,
     notebook unreadable, or I/O error writing index.

stdout: always silent.
stderr: one status line on success; error message(s) on failure.
        Staleness skip: "[index] fresh — skipping rebuild"
        Success:        "[index] wrote <path>"
        Best-effort:    "[warn] ..."  for non-fatal issues (gitignore, etc.)
```

### 0.1 `_NB_INDEX_SCRIPT` discovery in nb-write.py

The absolute path to `nb-index.py` is computed once at module import time
in `nb-write.py`:

```python
_NB_INDEX_SCRIPT = (Path(__file__).parent / "nb-index.py").resolve()
```

If `_NB_INDEX_SCRIPT` does not exist at that path, the Popen call in §8
prints a warning to stderr and returns without error. The write operation
is not affected.

The existence check in the Popen guard uses the **unresolved sibling path**
so that a symlink `nb-index.py -> real_script.py` is treated as present
even if the resolved target is later replaced:

```python
_NB_INDEX_SIBLING = Path(__file__).parent / 'nb-index.py'  # unresolved
if _NB_INDEX_SIBLING.exists():   # follows symlinks — correct
    subprocess.Popen([sys.executable, str(_NB_INDEX_SCRIPT), ...])
```

---

## §1 — Index Directory Resolution

### 1.1 Path normalised at entry
All code paths pass `nb_path` through `Path(nb_path).resolve()` before
any path arithmetic. Two representations of the same file (`./a/../b.ipynb`
and `./b.ipynb`) must produce the same index path.

### 1.2 Git-root detection
Given a notebook at `/project/data/nb.ipynb` where `/project/.git/`
exists, `_find_index_dir(nb_path)` returns `/project/.nb_index`.

### 1.3 Git-root with nested notebook
Given `/project/sub/deep/nb.ipynb` and `/project/.git/`, returns
`/project/.nb_index` (not `/project/sub/.nb_index`).

### 1.4 No git root fallback
Given a notebook with no `.git/` ancestor within 20 levels or within the
same filesystem mount, returns `<notebook_dir>/.nb_index`.

### 1.5 Index file path — git case
`_index_file_path(nb_path)` returns
`<git-root>/.nb_index/<relative-path>.json`, e.g.
`/project/.nb_index/data/nb.ipynb.json`.

### 1.6 Index file path — no-git case
Returns `<nb-dir>/.nb_index/<nb-basename>.json`, e.g.
`/standalone/.nb_index/nb.ipynb.json`.

### 1.7 Containment assertion
After construction, the resolved index path must be a descendant of the
resolved `.nb_index/` directory. A notebook path with `../` components
that would escape `.nb_index/` must cause exit 1 with a clear error.

### 1.8 Directory creation
`mkdir(parents=True, exist_ok=True)` is used; pre-existing directories
are not an error.

### 1.9 Depth limit
Walk stops after 20 levels upward without finding `.git/`. Falls back to
notebook-dir.

### 1.10 Filesystem boundary stop
Walk stops if `os.stat(current_dir).st_dev != os.stat(parent_dir).st_dev`.

### 1.11 Two-representations test
`_index_file_path("./data/../analysis.ipynb")` and
`_index_file_path("./analysis.ipynb")` from the same working directory
must return the same path.

---

## §2 — .gitignore Management

### 2.1 Entry added when no .gitignore exists
After first index creation, a `.gitignore` containing `.nb_index/` exists
at the same level as `.nb_index/`.

### 2.2 Entry added when .gitignore exists and lacks it
Existing content preserved verbatim; `.nb_index/` appended.

### 2.3 Entry not duplicated
Running the indexer twice must not add a second `.nb_index/` line.

### 2.4 Correct level
`.gitignore` modified is at the **same directory as `.nb_index/`**.

### 2.5 Pre-existing entries preserved
A `.gitignore` with `__pycache__/` and `*.pyc` retains those lines.

### 2.6 Symlink refused
If `.gitignore` is a symlink, the update is skipped and a warning is
printed to stderr. The index write proceeds normally.

### 2.7 Read-only directory handled gracefully
If the `.gitignore` write raises `OSError`, a non-fatal warning is
printed to stderr. Exit code remains 0.

---

## §3 — Staleness and Rebuild

### 3.1 Fresh index stores all three staleness signals
`index["notebook_mtime"]`, `index["notebook_size"]`, and
`index["nb_content_hash"]` must be set and correct after indexing.

### 3.2 Stale on mtime change
Changing the notebook's mtime without changing content still triggers
a rebuild (the hash check acts as a second gate, but mtime mismatch
alone is sufficient to start the rebuild process).

### 3.3 Stale on size change
A notebook whose size changes but whose mtime does not (FAT32 edge case
mock) is detected as stale.

### 3.4 Stale on content change (same mtime and size)
A notebook whose raw bytes change but whose mtime and size happen to be
unchanged is detected as stale via hash comparison.

### 3.5 Fresh index not rebuilt
When all three signals match, a second indexer invocation must NOT
replace the index file. Verified by asserting the index file's inode
number is unchanged after the second run.

### 3.6 `--force` flag bypasses staleness check
Always rebuilds; inode number changes after `--force` even on a fresh
index.

### 3.7 Missing index treated as stale
`_index_is_stale()` returns `True` when no index file exists.

---

## §4 — Outline Generation (derived, not stored)

The `outline` is derived at read time from the `cells` array — it is
**not** a separate serialised field.

`_derive_outline(cells)` takes the `cells` list from a loaded index
(or from a notebook fallback) and returns a compact list, one entry per cell:

```python
{"i": 0, "type": "code", "exec": 1, "status": "ok",   "line": "import Pkg;"}
{"i": 1, "type": "markdown", "exec": None, "status": None,
 "line": "# Packages", "heading": 1}
```

When reading from the **index**, `line` comes from the stored `first_line`
field (see Schema). When deriving from a **raw notebook** (fallback path),
`line` is extracted as the first non-empty line of `cell["source"]`,
stripped of whitespace, ANSI-sanitised, and truncated to 120 chars.

When reading from the **index**, `status` comes from the stored
`cells[i].status` field. When deriving from a **raw notebook** (fallback),
status is computed as:
- `exec` not null, no error outputs → `"ok"`
- `exec` not null, outputs contain an error → `"error"`
- `exec` null → `"not_run"`

### 4.1 One entry per cell
`len(_derive_outline(cells))` == `cell_count`.

### 4.2 Code cell entry fields
`i` (int), `type` ("code"), `exec` (int or null), `status`
("ok"|"error"|"not_run"), `line` (str).

### 4.3 Markdown cell entry: heading field present only when applicable
`i`, `type` ("markdown"), `exec` (null), `status` (null), `line` (str).
`heading` (int 1–6) added only when first non-empty source line is a
heading.

### 4.4 `line` is first non-empty source line, stripped of whitespace
Source `["  \n", "x = 1\n", "y = 2"]` → `line: "x = 1"`.

### 4.5 Empty cell → `"(empty)"`
Source `[]` or whitespace-only → `line: "(empty)"`.

### 4.6 Execution status derivation (fallback path only)
When outline is derived from a raw notebook (no index available), compute
status per the rules above. When reading from the index, use the stored
`status` field directly.

### 4.7 Empty notebook (0 cells)
`_derive_outline([])` returns `[]` without error.

---

## §5 — Section Extraction

### 5.1 Markdown headings detected
A markdown cell whose first non-empty line matches `^#{1,6}\s+(.+)` is
a section boundary; heading level = count of `#` chars.

### 5.2 `sections` ordered by `first_cell` ascending

### 5.3 Section spans to next heading of equal or greater semantic rank
A section spans from its heading cell to the cell immediately before the next
heading whose level number is ≤ the current section's level number (e.g. an
h2 section closes when the next h1 or h2 heading is encountered; an h1 section
closes only when another h1 is encountered). The last section extends to the
final cell of the notebook.

### 5.4 Empty sections list when no headings
`"sections": []`.

### 5.5 Cell `section` field
`cells[i]["section"]` = heading text of innermost containing section, or
`null` if before any heading.

### 5.6 Notebook with no markdown cells
`"sections": []`; all cells have `"section": null`.

---

## §6 — Symbol Extraction

All patterns are defined in A5. Tests use the exact regexes from A5.

### 6.1 Python `def` detected
`def process(x):\n    return x` → `symbols_defined` includes `"process"`.

### 6.2 Python `class` detected
`class MyModel:` → `symbols_defined` includes `"MyModel"`.

### 6.3 Python top-level assignment detected
`result = compute()` → `"result"` in `symbols_defined`.

### 6.4 Python annotated assignment detected
`x: int = 5` → `"x"` in `symbols_defined`.

### 6.5 Python augmented assignment NOT captured
`counter += 1` must NOT produce `"counter"` in `symbols_defined`.

### 6.6 Python `import` detected
`import numpy as np` → `"numpy"` in `symbols_imported`.

### 6.7 Python `from … import` detected
`from sklearn.linear_model import Ridge` → `"sklearn.linear_model"` in
`symbols_imported`.

### 6.8 Python walrus operator NOT captured as assignment
`if (n := len(a)) > 10:` must NOT produce `"n"` in `symbols_defined`
(walrus is never at line start after ASSIGN_RE's `^` anchor).

### 6.9 Julia `function` detected (including bang-names)
`function push!(x, v)\n...\nend` → `"push!"` in `symbols_defined`.

### 6.10 Julia short assignment form
`polarise(x, p) = ...` → `"polarise"` in `symbols_defined`.

### 6.11 Julia `using` — single and multi-module
`using ForwardDiff` → `"ForwardDiff"` in `symbols_imported`.
`using GLMakie, StaticArrays` → both `"GLMakie"` and `"StaticArrays"`.

### 6.12 Julia `import … :` detected
`import CancerResearch: PiecewiseTyson` → `"CancerResearch"` in
`symbols_imported`.

### 6.13 Unknown language: extraction skipped
`symbols_defined: []`, `symbols_imported: []`, `symbols_extracted: false`.

### 6.14 Symbol index built correctly from all cells
`symbol_index["process"]` lists all cell indices that define `"process"`.

### 6.15 Non-code cells skipped
Markdown and raw cells produce `symbols_defined: []`,
`symbols_imported: []`, `symbols_extracted: false`.

### 6.16 Long-line skip (ReDoS protection)
A cell whose source contains a line of 501 characters must complete
indexing without timing out. The long line is silently skipped; other
lines in the cell are still processed.

### 6.17 Adversarial input: no closing delimiter
A cell source `"library(aaaa..."` (10,000 `a` chars, no `)`) must return
in < 100 ms. The line exceeds 500 chars and is skipped.

---

## §7 — Output Storage

### 7.1 Stream output stored as text
`[{"output_type": "stream", "text": ["hello\n", "world"]}]` →
`output_text: "hello\nworld"`.

### 7.2 execute_result text stored
`{"output_type": "execute_result", "data": {"text/plain": "42"}}` →
`output_text` includes `"42"`.

### 7.3 Error traceback stored
`{"output_type": "error", "traceback": ["Traceback...\n", "ValueError: bad"]}` →
`output_text` includes `"ValueError"`.

### 7.4 Binary outputs not stored
Image/JSON outputs: `has_output: true`, type in `output_types`, no
`output_text` key.

### 7.5 4 KB cap — truncation at last complete line
Combined output text > 4096 bytes: stored text ends at last complete
line before byte 4096. `output_truncated: true`.

### 7.6 Exact 4096-byte output
`output_truncated: false`.

### 7.7 Single line > 4096 bytes
When the first output line exceeds 4096 bytes, store the first 4096
bytes hard-truncated, append `\n[truncated mid-line]`,
`output_truncated: true`. Must not produce an empty `output_text`.

### 7.8 No output → no `output_text` key
`outputs: []` → `has_output: false`, no `output_text` key.

### 7.9 Multiple outputs concatenated in order

### 7.10 `output_types` deduplicated in order of first appearance

### 7.11 Null bytes stripped from output
Cell output `"hello\x00world"` → stored as `"helloworld"` (null byte
removed before cap check).

### 7.12 Lone surrogates replaced
Output containing lone surrogate `\ud800` → replaced with `�`
before storage. No `UnicodeEncodeError` is raised.

### 7.13 Output stored as JSON string, never raw object
A cell that prints valid JSON text (`{"key": "val"}`) must have that
text stored as a JSON string value — not deserialized into a dict.

### 7.14 `display_data` with `text/plain` stored as output text
`{"output_type": "display_data", "data": {"text/plain": "42"}}` →
`output_text` includes `"42"`. `display_data` appears in `output_types`.

### 7.15 Mixed text and binary outputs in the same cell
If a cell has both text outputs (stream/execute_result/error/display_data
with text/plain) and binary outputs (image/png, etc.), the text portions
are stored in `output_text` subject to the 4 KB cap, and the binary MIME
types appear in `output_types`. Neither presence blocks the other.

---

## §8 — nb-write.py Integration

### 8.1 Popen after patch: shell=False, list form, absolute paths
The spawned command must be a list `[sys.executable, abs_script, abs_nb]`.
`shell=False` must be explicit or default. A notebook path containing
shell metacharacters (`; & | $ `` > < !`) must be passed as a literal
argument, not interpreted by a shell.

### 8.2–8.3 Popen after insert and delete
Same as 8.1.

### 8.4 `create` does NOT trigger indexing
No subprocess spawned.

### 8.5 Indexing failure does not fail the write
If `nb-index.py` is absent or exits non-zero, `nb-write.py` exits 0.

### 8.6 Uses `sys.executable`
The Python interpreter used for the subprocess is `sys.executable`, not
a hard-coded path.

### 8.7 Metacharacter-in-path test
`nb-write.py path/with spaces and $vars.ipynb patch 0` followed by
inspecting the nb-index.py argv must show the path as a single literal
argument with no shell expansion.

---

## §9 — nb-read.py: `--outline` mode

Output format per cell (derived from index or notebook directly):

```
[N:type:run=N] first_line
```

Where `run=N` is the execution count, or `run=——` for not-run/markdown
cells. Markdown/raw cells omit the `run=` field entirely. No `──` bar
is added in outline mode (one line per cell). Section headings are shown
as-is:

```
analysis.ipynb | 24 cells | python3

[0:code:run=1 ] import pandas as pd
[1:markdown   ] ## Data Loading
[2:code:run=——] (empty)
```

Note: `run=` field only on code cells. Field widths are right-padded
within the bracket to keep columns roughly aligned.

### 9.1 `--outline` prints compact one-line-per-cell table

### 9.2 `--outline` reads from index when fresh
Uses `cells[i].first_line` and `cells[i].status` from the index.
Must NOT open the `.ipynb` file when a fresh index exists.

`first_line` values must be passed through the ANSI sanitiser before
terminal output even when read from the index, because index files may
be externally modified or crafted. `--no-safe` disables this stripping
(consistent with `--no-safe` on cell source).

### 9.3 `--outline` falls back to notebook when index absent
Reads the notebook directly; `first_line` derived from cell source,
exec counts from `execution_count` fields, status derived per §4.6.

### 9.4 `--outline` uses notebook (with warning) when index is stale
When the index exists but is stale (any of the three staleness signals
differ), nb-read.py prints `[STALE INDEX] <path>` to stderr and falls
back to reading the notebook directly. No synchronous rebuild is
triggered.

### 9.5 Heading cells visually distinct (no bar, type shown as `markdown`)

### 9.6 Compatible with `--cells` filter

### 9.7 `--no-safe` applies the same ANSI stripping to `first_line`
content as it does to cell source in normal mode. `--outline --no-safe`
is **valid** and must not error.

---

## §10 — nb-read.py: `--outputs` mode

### 10.1 `[output]` section rendered after source for cells with outputs

```
[3:code:run=5] ──────────────────────────────────
│ df.describe()
[output] ────────────────────────────────────────
│        col_a  col_b
│ count    150    150
```

### 10.2 Reads output text from index when fresh
`.ipynb` not opened for output data.

### 10.3 Falls back to notebook when index absent or stale
Applies same 4 KB cap at render time. Prints `[STALE INDEX] <path>`
to stderr when stale (same rule as §9.4).

### 10.4 Truncated outputs show truncation notice

### 10.5 Cells with no output show no `[output]` section

### 10.6 `--no-safe` passes ANSI through; safe mode (default) strips it

---

## §11 — nb-read.py: Execution metadata in cell header

**Breaking change:** The current header format `[0:code] ────────────────────`
becomes `[0:code:run=——] ──────────────────` for code cells. Markdown and raw
cells are unchanged: `[1:markdown] ────────────────────`. The tests in
`test_read_independent.py` and `test_scripts.py` that assert `"[0:code]"` must
be updated to `"[0:code:run="` when nb-read.py is modified.

### 11.1 Execution count in code cell header
Format: `[3:code:run=5]`. `run=——` when execution_count is null.

### 11.2 Markdown and raw cells show no run field

### 11.3 Section name in header when index available
Format: `[6:code:run=3 §Analysis]`. Section name truncated at **20 chars**
with `…` if longer.

### 11.4 Hard header line limit
Total header line length (including `─` bar) must not exceed **72 chars**.
Minimum bar length is **4 `─` chars**. If the metadata fields alone would
exceed 68 chars, the section name is truncated further (or omitted) to
keep the bar ≥ 4 chars.

### 11.5 Long section name truncation
A section heading of 80 chars produces at most `…` (20-char truncation
rule) in the header. No crash from negative bar length.

### 11.6 Section name absent when index unavailable or stale
When no fresh index exists, section names are omitted from headers (the
`§Section` field requires the index). Prints `[STALE INDEX]` to stderr
when the index is stale (same rule as §9.4).

---

## §12 — nb-search.py

### Walk strategy
nb-search.py locates index files by scanning for `.nb_index/` directories
(not by scanning for `.ipynb` files). For each `.nb_index/` found:
enumerate its `.json` files. This is faster than stat-ing every `.ipynb`.

Walk constraints:
- `os.walk(followlinks=False)` — symlinks in `.nb_index/` not followed.
- Skip directories: `node_modules`, `.venv`, `venv`, `__pycache__`,
  `.tox`, `.git`, `.hg` (add `--no-skip` flag to override).
- Max depth: 20 levels from the search root.
- `notebook_path` field read from any index file is validated: its
  resolved path must satisfy `candidate.is_relative_to(search_root.resolve())`.
  Reject (warn + skip) if it escapes. The phrase 'expected parent
  directory' is intentionally absent — only strict containment within
  the search root is accepted. Null bytes in `notebook_path` must also
  cause the entry to be skipped (they cannot appear in valid POSIX paths).

### 12.1 Keyword search across indexed notebooks
`nb-search.py "process" /path/to/project` finds all cells in indexed
notebooks whose source contains `"process"` (case-insensitive).

Keyword search (bare positional query, no `--symbol`/`--import` flag)
**opens the `.ipynb` file** for each indexed notebook to scan cell source
text directly. The index is used only to locate which notebooks exist in
the project and to supply metadata (section, exec status) for result lines.
See §12.13.

### 12.2 Symbol lookup `--symbol`
Finds cells where `symbol_index` contains the queried name.

**Fast path:** when `symbols.json` is present at the index root and its
`generated_at` timestamp is **strictly greater than** the `indexed_at` of
**every** per-notebook index file currently on disk in the project, use it
for O(1) lookup. This comparison must be re-evaluated on every query —
not cached. Fall back to serial per-notebook index scan when `symbols.json`
is absent, stale, or corrupt. The same version-compatibility rules apply
as for per-notebook indices (unknown version → skip + warn, fallback to
serial scan).

### 12.3 Import lookup `--import`
Finds cells where `import_index` key starts with the queried module name.

### 12.4 Output format: one result per line
`relative/path.ipynb:N: first source line`

### 12.5 `--type` filter (code|markdown|raw)

### 12.6 Stale index: warn on stderr, return results anyway
`[STALE] path/to/nb.ipynb` on stderr; results still printed.

### 12.7 Unindexed notebook: skip with notice
`[UNINDEXED] path/to/nb.ipynb — run nb-index.py first` on stderr.

### 12.8 `--section` filter

### 12.9 Exit codes
- 0: one or more matches
- 1: no matches
- 2: usage error

### 12.10 Streaming output
Results are printed as found, not buffered until all files are loaded.

### 12.11 `--limit N` flag
Stop after N results (default: no limit).

### 12.12 `notebook_path` field validated against search root
A crafted index file with `"notebook_path": "../../../../etc/passwd"`
must not cause nb-search to open that path.

Implementation must use:
```python
candidate = (search_root / index["notebook_path"]).resolve()
if not candidate.is_relative_to(search_root.resolve()):
    warn_and_skip()
    continue
# Only now open `candidate`
```

The `is_relative_to` check must occur **after** `Path.resolve()` on
the joined path — not on the raw string. Null bytes in `notebook_path`
must cause the entry to be skipped before any path construction.

### 12.13 Keyword search reads notebook files; symbol/import search reads index only
For `nb-search.py QUERY` (no flag): open each notebook's `.ipynb` file to
scan source text. For `--symbol` and `--import`: read only index JSON files
(never open `.ipynb`). This distinction preserves the RAG efficiency goal
for the common symbol/import lookup case while enabling full-text keyword
search at the cost of file I/O.

---

## §13 — Project-Level Symbol Cache (optional derived file)

The indexer writes a second file `<index-dir>/symbols.json` as a
derived cache mapping symbol names to all locations across the project.

```json
{
  "version": 1,
  "generated_at": "2025-05-27T14:00:00Z",
  "symbols": {
    "polarise":        ["analysis.ipynb:22"],
    "bistability":     ["analysis.ipynb:34", "plots.ipynb:5"]
  },
  "imports": {
    "ForwardDiff":     ["analysis.ipynb:8"],
    "GLMakie":         ["analysis.ipynb:12", "plots.ipynb:1"]
  }
}
```

### 13.1 Created alongside per-notebook index on first index build
### 13.2 Updated incrementally when a notebook is re-indexed
The stale notebook's entries are removed, new entries added.

The `notebook_path` key used for removal must be **recomputed from
the actual notebook file path** using `_index_file_path()`, not read
from the existing per-notebook index. Reading the existing index's
`notebook_path` field for the removal key allows an attacker who can
write to `.nb_index/` to cause cross-notebook symbol poisoning by
setting one notebook's `notebook_path` to another notebook's path.
### 13.3 Missing symbols.json falls back to serial scan
nb-search.py works correctly without it.
### 13.4 Corrupt symbols.json triggers rebuild from per-notebook indices
### 13.5 symbols.json itself is NOT indexed by nb-search (skip it)

### 13.8 Location strings in symbols.json validated on read
Location strings (e.g. `"analysis.ipynb:22"`) are split on `:` to
extract the notebook path portion. That path is then validated with
the same `is_relative_to(search_root.resolve())` check as §12.12.
A crafted `symbols.json` with `"../../../../etc/passwd:0"` must not
cause nb-search to open `/etc/passwd`.

### 13.6 Atomic write with lock for concurrent indexers
symbols.json is written using the same temp-file + fsync + `os.replace()`
pattern as notebook writes. Before reading and writing symbols.json,
acquire a `LOCK_EX` flock on `.nb_index/symbols.nblock` (companion lock
file). Use non-blocking try (`LOCK_EX | LOCK_NB`): if the lock is
unavailable, skip the symbols.json update silently — the other indexer
will write a consistent version. nb-search falls back to serial scan per
§13.3.

### 13.7 Version compatibility
`version > 1` → log warning to stderr, skip, fall back to serial scan.
`version` missing or non-integer → treat as corrupt, rebuild from
per-notebook indices.

---

## §14 — Additional Edge Case Tests

### 14.1 0-cell notebook
`create` followed by `nb-index.py` → `cell_count: 0`, `cells: []`,
`sections: []`, `symbol_index: {}`, exit 0.

### 14.2 Notebook with 0 code cells (all markdown)
`symbol_index: {}`, `import_index: {}`. No crash.

### 14.3 Notebook path with `../` components
`nb-index.py /project/../../outside/nb.ipynb` must exit 1 with a
containment error, not create files outside the index directory.

### 14.4 `.gitignore` is a symlink
Indexer runs successfully; `.gitignore` is NOT written through;
warning appears on stderr.

### 14.5 Two indexer invocations on the same notebook, overlapping
Simulate by running two `nb-index.py` processes concurrently. Both must
exit 0. The final index must be valid JSON and reflect the notebook's
actual content (no partial write, no corrupt JSON).

### 14.6 Rapid successive writes (10 patches in <1 second)
10 sequential `nb-write.py patch` calls must each exit 0. The final
index must reflect the last patch. No orphaned `.nb_tmp` files.

### 14.7 Single output line > 4096 bytes
A cell whose output is one line of 5000 `a` characters must produce
`output_text` of exactly 4096 bytes ending with `\n[truncated mid-line]`,
`output_truncated: true`.

### 14.8 Search root is a file path, not a directory
`nb-search.py "foo" analysis.ipynb` must exit 2 with a usage error
(search root must be a directory).

### 14.9 Walk depth limit
A directory tree 25 levels deep with a `.nb_index/` only at level 22
must NOT be found (exceeds max depth 20). Walk exits cleanly.

### 14.10 `node_modules` skipped
A project with `node_modules/deep/.nb_index/nb.ipynb.json` must not
have that index loaded by nb-search.

### 14.11 `first_line` stored correctly for all cell types
After indexing: a code cell with source `["x = 1\n", "y = 2"]` has
`first_line: "x = 1"`. A markdown cell with source `"## Heading\n..."` has
`first_line: "## Heading"`. An empty cell has `first_line: "(empty)"`.

### 14.12 `display_data` output treated as text
A cell with `{"output_type": "display_data", "data": {"text/plain": "fig"}}` →
`output_text: "fig"`, `has_output: true`, `"display_data"` in `output_types`.

### 14.13 `--outline` from fresh index never opens notebook file
Patch a test notebook; index it; use `strace` or mock `open()` to verify
nb-read.py does NOT open the `.ipynb` during `--outline` when index is fresh.

---

## §15 — Shared Index Discovery Logic

The functions `_find_index_dir(nb_path)` and `_index_file_path(nb_path)`
are **re-implemented identically** in `nb-index.py`, `nb-read.py`, and
`nb-search.py`. There is no shared import between scripts (all scripts
are stdlib-only, standalone executables). The implementations must be
byte-for-byte equivalent except for module-level constants.

Implementations must follow exactly the algorithm in §1:
1. `Path(nb_path).resolve()` first.
2. Walk upward at most 20 levels, check
   `(d / '.git').is_dir() and not (d / '.git').is_symlink()`
   (see A1 for why `os.path.lexists()` must NOT be used),
   stop if `st_dev` changes.
3. Construct index path per §1.5 or §1.6.
4. Assert containment per §1.7 before any mkdir.

nb-read.py performs the three-signal staleness check (§A3) before
deciding which path to take for `--outline`, `--outputs`, and `§Section`
header features:

| Index state | nb-read.py behaviour |
|-------------|----------------------|
| Fresh | Use index (no .ipynb open) |
| Absent | Fall back to notebook |
| Stale | Fall back to notebook + `[STALE INDEX]` on stderr |

nb-read.py never triggers a synchronous rebuild.

---

## New files

| Path | Description |
|------|-------------|
| `scripts/nb-index.py` | Builds/updates `.nb_index/<path>.json` and `symbols.json` |
| `scripts/nb-search.py` | Keyword / symbol / import / section search |
| `tests/test_nb_index.py` | Full test suite for §1–§8, §13–§14 |
| `tests/test_nb_search.py` | Full test suite for §12 |

## Modified files

| Path | Changes |
|------|---------|
| `scripts/nb-read.py` | `--outline`, `--outputs`, exec+section in header (§9–§11); index discovery (§15) |
| `scripts/nb-write.py` | `_NB_INDEX_SCRIPT` constant (§0.1); fire-and-forget Popen after save (§8) |
| `SKILL.md` | Rule 0 (index first); `nb-index.py` / `nb-search.py` usage |
| `tests/test_scripts.py` | Update `[N:code]` header assertions → `[N:code:run=` (§11 breaking change) |
| `tests/test_read_independent.py` | Same header format update as above |
| `install.py` | Copy `nb-index.py` and `nb-search.py` to `scripts_dst`; make executable on POSIX |
| `uninstall.py` | No functional change; ensure it does not reference removed files |

## Out of scope (v1)

- Vector / semantic embeddings
- `batch` subcommand for nb-write.py (deferred — 44 ms/write acceptable
  for interactive use)
- Incremental per-cell index updates beyond source_hash skip
- Image / rich output description
- Windows path normalisation in index (tracked as known limitation)
- Full AST-based symbol extraction (regex covers 90% of cases)
