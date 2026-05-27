# TDD_INDEX Audit Report

Three adversarial agents reviewed the TDD: security, architecture/design, and performance.
Findings are de-duplicated and sorted by priority. The **Disposition** column records
what the updated TDD does about each finding.

---

## CRITICAL — must resolve before any code is written

| # | Source | Finding | Disposition |
|---|--------|---------|-------------|
| C1 | Security | **Path traversal in index path construction (§1.4).** A notebook path with `../` components resolves outside `.nb_index/`. Combines with `mkdir(parents=True)` to create arbitrary directories. | Add `Path.resolve()` normalization at entry point + containment assertion after constructing index path. |
| C2 | Security | **Shell injection via notebook path in subprocess.Popen (§8/A6).** If `shell=True` or a shell string is used, a crafted path like `/tmp/x; rm -rf ~; echo .ipynb` executes arbitrary commands. | Mandate `shell=False`, list form `[sys.executable, nb_index_abs, os.path.abspath(nb_path)]`. Add test. |

---

## HIGH — fix in TDD before implementation

| # | Source | Finding | Disposition |
|---|--------|---------|-------------|
| H1 | Security | **`.gitignore` symlink → arbitrary file write (§2/A2).** Pre-placed `.gitignore -> ~/.ssh/authorized_keys` causes indexer to append to attacker-chosen file. | Check `os.path.islink()` before opening; refuse and warn if symlink. |
| H2 | Security | **ReDoS in symbol extraction (§6/A5).** Patterns like `[^)]*` on 10k-char lines with no closing delimiter cause catastrophic backtracking. | Per-line cap of 500 chars before regex. Explicit non-nested patterns specified in TDD (not prose). Add adversarial test inputs. |
| H3 | Security | **Output text JSON corruption (§7/A4).** Null bytes (`\x00`) and lone surrogates in cell output can corrupt the index or raise on encode. | Strip null bytes and replace lone surrogates with `�` before storage. Mandate `ensure_ascii=False` (also fixes Performance H1). Outputs stored as JSON strings, never raw objects — add explicit test. |
| H4 | Security | **Zip-slip / symlink escape in nb-search directory walk (§12).** Symlink `.nb_index/evil.json -> /etc/passwd` causes searcher to parse an attacker-controlled file. `notebook_path` field in index used to build read paths can escape the search root. | Use `os.walk(followlinks=False)`. Validate that resolved `notebook_path` is within the expected search root before any further reads. |
| H5 | Architecture | **Git-root walk escapes into home-dir `.git/` (§A1/§1).** User with `git init ~` causes all notebooks to dump into `~/.nb_index/`. No depth limit or filesystem-boundary stop. | Add max_depth=20 and `st_dev` boundary check. Stop if device changes between levels. Cap walk; fall back to notebook-dir if limit hit. |
| H6 | Architecture | **Concurrent indexer write race (§A6/§8).** Two rapid writes spawn two indexers; last-write-wins but the winner may have read stale notebook state. | Before writing index, re-stat the notebook; if mtime changed since the indexer started, abort silently (a newer indexer will write). Spec this as an optimistic-concurrency check. |
| H7 | Architecture | **Symbol extraction patterns unspecified (§A5/§6).** Prose descriptions leave walrus operator, annotated assignments, Julia `!`-functions, multi-dispatch, and `type` aliases undefined. Implementations will diverge. | Replace prose with explicit regex strings in the TDD. Add test cases for each edge case. |
| H8 | Performance | **`ensure_ascii=True` causes 6× size overhead on Unicode output (§7).** A notebook with CJK/emoji outputs produces a 6× larger index than necessary. | Mandate `ensure_ascii=False` on all index JSON writes. (Consolidates with H3.) |
| H9 | Performance | **828 KB index per heavy notebook (§7/A4).** 200 cells × 4 KB output = 828 KB per notebook, 40 MB for 50-notebook project. Project-wide cold-cache search loads 40 MB. | Output text is stored (user decision: 4 KB cap stands). Mitigate with `ensure_ascii=False` (H8) and project-level symbol cache (§13). Document expected index sizes in TDD. |

---

## MEDIUM — fix in TDD before implementation

| # | Source | Finding | Disposition |
|---|--------|---------|-------------|
| M1 | Security | **`.gitignore` write failure in read-only directory (§2/A2).** Unhandled `PermissionError` leaks paths in traceback. | Wrap in `try/except OSError`; print non-fatal stderr warning; continue indexing. |
| M2 | Security | **Missing schema validation on index load (§3/§12).** Type confusion on `notebook_mtime` (string vs float) silently passes staleness check. `version` mismatch not handled. | Specify: load must validate `version` (int) first; unknown version → skip + warn; missing version → treat as corrupt, rebuild. Type-check all numeric fields; fall back to rebuild on error. |
| M3 | Architecture | **Schema versioning has no migration spec (§schema).** `version: "1"` (string, inconsistent) with no forward-compat contract. | Change `version` to integer `1`. Specify: readers must tolerate unknown top-level keys (additionalProperties-permissive). Unknown version → skip, not crash. |
| M4 | Architecture | **mtime alone is unreliable (§A3/§3).** FAT32 has 2-second granularity; NFS mtime is client-cached; Jupyter auto-save touches mtime on execution_count changes causing spurious rebuilds. | Add `nb_file_size` and `nb_content_hash` (MD5 of raw notebook bytes) to index. Staleness = mtime changed OR size changed OR hash changed. mtime is a cheap pre-filter; hash is the authoritative check. |
| M5 | Architecture | **§3.3 test is defective — atomic rename always changes mtime.** `os.replace()` creates a new inode; verifying "index mtime unchanged" always fails after any write. | Reformulate test: assert inode is unchanged (second indexer run must not replace the file at all, so inode number stays the same). OR mock the write path and assert it is not called. |
| M6 | Architecture | **Single output line > 4 KB produces 0 stored bytes (§7.5).** "Last complete line before boundary" yields nothing when the first line is 5 KB. Silent data loss. | Add §7.10: if no complete line fits in 4096 bytes, store the first 4096 bytes hard-truncated with `output_truncated: true` and a `[truncated mid-line]` suffix. Add test. |
| M7 | Architecture | **§12 walk strategy unspecified — no depth limit, no skip list (§12).** Walk into `.venv/`, `node_modules/`, `__pycache__/` can scan millions of files. | Specify walk strategy: scan for `.nb_index/` directories (not `.ipynb` files). Skip `node_modules`, `.venv`, `__pycache__`, `.tox`, `.git` by default. Add `--no-skip` override. Max depth 20. `followlinks=False`. |
| M8 | Architecture | **§11.3/§11.4 contradiction — long section names break 48-char header (§11).** A 57-char section name makes the header 73 chars before any bar is added, causing negative bar length (crash). | Change spec: hard max header line = 72 chars. Section name in header truncated at 20 chars with `…` if needed. Minimum bar = 4 `─` chars. Remove the vague "≈ 48 chars" wording. |
| M9 | Architecture | **Path not normalised before index construction (§1).** Paths like `./data/../nb.ipynb` and `./nb.ipynb` produce different index paths for the same file. | Specify: `nb_path` is passed through `Path.resolve()` at the top of `_find_index_dir()` before any path arithmetic. Add test: two representations of the same notebook → same index path. |
| M10 | Architecture | **Missing test cases (various).** 0-cell notebooks, 0-code-cell notebooks, symlink loops in walk, concurrent indexer invocations, rapid successive writes, single line > 4 KB output, search root = single file path, `../`-component notebook path. | Add §14 — Additional Edge Case Tests covering all of the above. |
| M11 | Performance | **Full rebuild on mtime change; no incremental update (§A3/§3).** For a `patch` on one cell, the entire notebook is re-indexed. For 200 cells this is ~3 ms, acceptable today but scales poorly for automated agents. | Add `source_hash` per cell to index. Specify: on rebuild, if a cell's `source_hash` is unchanged, reuse its existing cell entry (only recompute symbol extraction and output capture for changed cells). |
| M12 | Performance | **`outline` array duplicates `cells` data (§schema).** `outline` is 14% of `cells` serialized size — redundant, adds a staleness risk between the two arrays. | Drop `outline` from the serialized schema. Derive it from `cells` at read time when `--outline` is requested. No spec change needed to §9 (outline *output* format unchanged). |

---

## LOW — fix or accept

| # | Source | Finding | Disposition |
|---|--------|---------|-------------|
| L1 | Architecture | **§9.6 `--outline --no-safe` incompatibility is unjustified.** `--outputs` allows `--no-safe`; outline has the same ANSI risk in `first_line` content. The restriction is inconsistent. | Remove the incompatibility. Apply same safe/no-safe logic to `first_line` content in outline as to cell source in normal mode. |
| L2 | Performance | **No project-level symbol cache — search scales O(N notebooks) per symbol query.** | Add §13: optional `<git-root>/.nb_index/symbols.json` derived cache. Content: `{"polarise": ["analysis.ipynb:22"]}`. Built by indexer as a second output. Fallback to serial scan if missing. |
| L3 | Performance | **20 subprocess launches for 20 sequential patches = ~888 ms.** | Add `batch` to future-work / Out-of-scope. Document in SKILL.md: prefer `insert -1` followed by multiple `patch` in a single planning step rather than interleaved `patch`-per-turn. |
| L4 | Performance | **Serial search with no short-circuit.** | Specify: nb-search prints results as found (streaming), not after all files loaded. Stops after `--limit N` results (default: no limit). |

---

## No-change decisions

| Topic | Decision |
|-------|----------|
| **4 KB output cap** | Confirmed. User explicitly chose this. Performance agent suggestion to default to 0 was noted but overridden. |
| **Regex compilation** | No change. Python's `re` LRU cache handles this; module-level `compile()` for readability only. |
| **Git root walk cost** | Negligible (68 µs). Cap at depth 20 + filesystem boundary is already in H5. |
| **Subprocess startup cost** | 44 ms per write is acceptable for interactive use. `batch` subcommand deferred to future work. |
