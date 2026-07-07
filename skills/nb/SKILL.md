---
name: nb
description: Read and edit Jupyter notebooks token-efficiently. Activate when the user asks to read, inspect, edit, modify, or create a .ipynb file — use nb-read.py instead of Read, and nb-write.py instead of Edit/Write.
argument-hint: <notebook.ipynb>
allowed-tools: Bash(python3 *), Bash(python *), Bash(py *), Bash(ls *), Bash(find *), Read, Write
---

## Rules — follow these strictly

1. **Never use `Read`, `Edit`, or `cat`/`grep` directly on a `.ipynb` file.** Raw notebook JSON is 10–50× more tokens than needed. Always use the scripts below. (`Read` may be used freely on non-`.ipynb` files.)

2. **Orient with `--outline` first.** On an unfamiliar or large notebook, run `nb-read.py --outline` (one line per cell) before reading full source.

3. **Always read immediately before writing.** Run `nb-read.py` in the same turn, right before any `nb-write.py` call. A read from a previous message turn is not sufficient — the file may have changed.

4. **Prefer surgical edits.** Patch one cell at a time. Do not reconstruct the whole notebook.

5. **Re-read after every structural change.** After any `insert` or `delete`, you MUST re-read the notebook before issuing further `patch`, `insert`, or `delete` commands. Indices shift and your previous index map is stale.

6. **Never patch a cell you have not fully seen.** If `nb-read.py` prints a truncation warning (on stderr) for a cell you intend to patch, re-read it first with `--cells <N> --truncate 0`. Patching from a truncated view destroys the hidden lines with no error.

7. **Always verify after writing.** After every `patch`, `insert`, or `delete`, re-read the affected cell with `--cells <N>` to confirm the result.

8. **Use `-f <file>` for source input — never heredocs.** A heredoc silently truncates if the source contains `EOF` on its own line. Write the source with the `Write` tool to a temp file, then:
   ```bash
   python3 "$NB_SCRIPTS/nb-write.py" <notebook.ipynb> patch <index> -f "$TMPDIR/nb_patch_source.txt"
   ```

9. **Strip the `│ ` prefix when copying source.** `nb-read.py` prefixes every source line with `│ ` for structural safety — it is not part of the cell. **Exception:** the output-summary line `│ ── (N outputs, M lines) ──` also carries the prefix but is NOT source; never include it in a patch.

10. **View outputs with `nb-read.py --outputs`** — never by reading raw notebook JSON.

11. **Search with `nb-search.py`, not grep**, for any cross-notebook lookup (keywords, symbol definitions, imports).

---

## Script location

The plugin manager installs scripts into a versioned cache directory. Resolve the path
dynamically — do **not** hardcode it.

**Shell state does not persist between Bash tool calls** — an exported `$NB_SCRIPTS`
from one call is empty in the next. Resolve the path ONCE with the snippet below, note
the literal path it prints, and use that **literal absolute path** in place of
`$NB_SCRIPTS` in all subsequent commands (it is stable for the whole session). Only
reuse the `$NB_SCRIPTS` variable when chaining commands inside a single Bash call.

```bash
# Linux / macOS (bash/zsh)
NB_SCRIPTS=$(python3 -c "
import json, pathlib, os, sys
cfg = pathlib.Path(os.environ.get('CLAUDE_CONFIG_DIR', str(pathlib.Path.home() / '.claude')))
reg = json.loads((cfg / 'plugins' / 'installed_plugins.json').read_text())
hits = [v[0]['installPath'] for k, v in reg['plugins'].items() if k.startswith('nb@')]
if not hits:
    sys.exit('ERROR: no nb@ plugin entry in installed_plugins.json — '
             'in a dev checkout, set NB_SCRIPTS to the repo scripts/ dir instead.')
print(pathlib.Path(hits[0]) / 'scripts')  # use this literal path in later commands
")
```

Windows (PowerShell): same Python snippet via `$NB_SCRIPTS = (py -3 -c "...")`, and use
`py -3` (or `python`) instead of `python3` in all commands.

**Dev-checkout fallback:** if the plugin is not installed (lookup errors out), point
directly at the repo: `NB_SCRIPTS=/path/to/repo/scripts`.

---

## Reading — `nb-read.py`

```bash
python3 "$NB_SCRIPTS/nb-read.py" <notebook.ipynb> [flags]
```

| Flag | Meaning |
|------|---------|
| `--cells N` / `N-M` / `N,M,K` | Show only these cell indices |
| `--type code\|markdown\|raw` | Filter by cell type |
| `--truncate N` | Max lines per cell (default 80; `0` = unlimited) |
| `--outline` | One compact line per cell (cheap overview) |
| `--outputs` | Render text outputs after each code cell |

**Normal mode output** (real capture):
```
demo.ipynb | 3 cells | python3 (python)

[0:code:run=1] ──────────────────────────────
│ import pandas as pd
│ df = pd.read_csv('data.csv')
│ ── (2 outputs, 1 lines) ──

[1:markdown] ────────────────────────────────
│ ## Analysis

[2:code:run=——] ─────────────────────────────
│ (empty)
```
- Code-cell headers carry the execution count: `run=1`, or `run=——` if never executed.
- `│ ── (N outputs, M lines) ──` is a summary, not source (Rule 9). Use `--outputs` to see the content.
- Truncation warnings go to **stderr**, with the cell index and a re-read hint.

**`--outline` mode** — first line of each cell, cheapest orientation (reads the index when fresh):
```
[0:code:run=1] import pandas as pd
[1:markdown] ## Analysis
[2:code:run=——] (empty)
```
Cells under a markdown heading may show a `§Section` tag in the bracket. When the
index is fresh, `--outline` never opens the notebook at all (cheapest possible read).

**`--outputs` mode** — the sanctioned way to inspect stdout/results/tracebacks:
```
[0:code:run=1] ──────────────────────────────
│ import pandas as pd
│ df = pd.read_csv('data.csv')
[output] ────────────────────────────────────────
│ (100, 4)
│ <DataFrame>
```
Text outputs render in full (stream, text/plain, tracebacks); outputs with no text form show a placeholder like `│ [image/png output — not shown]`. Markdown attachments show `│ [attachment "name": mime — not shown]`; raw cells show their mimetype in the header. Long outputs are cut at `--truncate` lines (default 80; `--truncate 0` = unlimited) with a marker giving the exact command for the full output.

---

## Searching — `nb-search.py`

Searches all indexed notebooks under a directory tree.

```bash
python3 "$NB_SCRIPTS/nb-search.py" [mode] [filters] <query> <search_root>

python3 "$NB_SCRIPTS/nb-search.py" read_csv .              # keyword (case-INsensitive)
python3 "$NB_SCRIPTS/nb-search.py" --symbol load_data .    # def/class/assignment (exact match)
python3 "$NB_SCRIPTS/nb-search.py" --import pandas .       # import statements (exact match)
```

| Flag | Meaning |
|------|---------|
| `--symbol` | Find symbol definitions (index-only) |
| `--import` | Find imports of a module (index-only) |
| `--type code\|markdown\|raw` | Filter by cell type |
| `--section <name>` | Filter by markdown section |
| `--limit N` | Stop after N results (must be ≥ 1) |
| `--in-outputs` | Keyword mode only: also match output text (streams, results, tracebacks); hits show `[output]` |

Result format: `<notebook-path>:<cell-index>: <first line of cell>`. Keyword mode is
case-insensitive; `--symbol` and `--import` are exact-match and case-sensitive.
Unindexed notebooks are still searched in keyword mode (noted `[UNINDEXED] … searched
directly` on stderr); all modes warn `[STALE]` on stderr when an index is out of date
but still return results — re-run `nb-index.py` to refresh. `[DUP]` notes a notebook
shadowed by a second index file.

---

## Indexing — `nb-index.py`

`nb-write.py` fires `nb-index.py` automatically after every write — you normally never run it.
Run it manually once when starting work on a pre-existing repo that has never been indexed:

```bash
python3 "$NB_SCRIPTS/nb-index.py" <notebook.ipynb>   # add --force to rebuild
```

---

## Writing — `nb-write.py`

```bash
python3 "$NB_SCRIPTS/nb-write.py" <notebook.ipynb> <op> [args]
```

| Op | Args | Notes |
|----|------|-------|
| `create` | — | New empty nbformat-4.5 notebook; fails if file exists |
| `patch` | `<index> -f <file>` | Replace cell source; clears outputs + execution_count. Identical source = no-op (file untouched). Source file must be UTF-8 |
| `insert` | `<index> <type> -f <file>` | Insert before `<index>`; `-1` appends; type: `code\|markdown\|raw` |
| `delete` | `<index>` | Remove cell |

- Always pass source via `-f` (Rule 8). Omitting `-f` reads stdin — avoid.
- Writes are atomic (temp file + rename, no `.bak`).
- After `insert`/`delete`: re-read before further edits (Rule 5). After any write: verify (Rule 7).

**Typical workflow:**
```bash
python3 "$NB_SCRIPTS/nb-read.py" analysis.ipynb --outline          # 1. orient
python3 "$NB_SCRIPTS/nb-read.py" analysis.ipynb --cells 4 --truncate 0   # 2. full view of target
# 3. Write new source with the Write tool → /tmp/nb_patch_source.txt
python3 "$NB_SCRIPTS/nb-write.py" analysis.ipynb patch 4 -f /tmp/nb_patch_source.txt
python3 "$NB_SCRIPTS/nb-read.py" analysis.ipynb --cells 4          # 4. verify
```

---

## Failures & concurrency

- **Script exits nonzero:** read its stderr and fix the stated problem (bad index, missing file, malformed notebook…). Do **NOT** fall back to raw `Read`/`cat` of the `.ipynb` — that defeats the plugin.
- **Lock contention:** `nb-write.py` takes an exclusive lock on a companion `.nblock` file for the whole read-modify-write cycle, so concurrent writes block briefly (POSIX `fcntl`, Windows `msvcrt`). Stray `*.nblock` files are normal and gitignored — never delete them manually.
- **Stale index:** `[STALE]`/`[UNINDEXED]` warnings from `--outline` or `nb-search.py` are not errors — results may be outdated. Re-run `nb-index.py <notebook.ipynb>` and retry.

---

## Notes

- **Index location:** `.nb_index/` is created at the git root (or next to the notebook when outside a repo); the indexer adds it to `.gitignore` automatically. Never edit index files by hand.
- **JSON indentation:** `nb-write.py` normalises indentation to 1 space on every write — a large first-edit diff is expected.
- **nbformat 4 only.** nbformat 3 notebooks (`worksheets` key): convert first with `jupyter nbconvert`.
- **Guard limits:** the nb-guard hook covers `Read`/`Edit`/`Write`/`MultiEdit`; `cat`/`grep` via Bash bypasses it. Don't do that (Rule 1).
- **Windows:** use `py -3` or `python` instead of `python3`; PowerShell form of the lookup is in Script location above.
