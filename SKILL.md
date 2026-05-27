---
name: nb
description: Read and edit Jupyter notebooks token-efficiently. Activate when the user asks to read, inspect, edit, modify, or create a .ipynb file — use nb-read.py instead of Read, and nb-write.py instead of Edit/Write.
argument-hint: <notebook.ipynb>
allowed-tools: Bash(python3 *) Bash(ls *) Bash(find *) Read Write
---

## Rules — follow these strictly

1. **Never use `Read` or `Edit` directly on a `.ipynb` file.** Raw notebook JSON is 10–50× more tokens than needed. Always use the scripts below. (`Read` may be used freely on non-`.ipynb` files.)

2. **Always read immediately before writing.** Run `nb-read.py` in the same turn, right before any `nb-write.py` call. A read from a previous message turn is not sufficient — the file may have changed.

3. **Prefer surgical edits.** Patch one cell at a time. Do not reconstruct the whole notebook.

4. **Re-read after every structural change.** After any `insert` or `delete`, you MUST re-read the notebook before issuing further `patch`, `insert`, or `delete` commands. Indices shift after every structural change and your previous index map is stale.

5. **Never patch a cell you have not fully seen.** If `nb-read.py` output shows a truncation warning for a cell you intend to patch, re-read that cell first with `--truncate 0`. Patching from a truncated view destroys the hidden lines with no error.

6. **Always verify after writing.** After every `patch`, `insert`, or `delete`, re-read the affected cell index with `--cells <N>` to confirm the written content matches your intent.

7. **Use `-f <file>` for source input — never heredocs.** If a cell source contains the word `EOF` on its own line, a heredoc silently truncates the input. Always write the new source to a temp file via Bash, then pass `-f` to `nb-write.py`:
   ```bash
   python3 -c "import sys; open('/tmp/nb_patch_source.txt','w').write(sys.stdin.read())" << 'NBCELLSRC'
   <your cell source here>
   NBCELLSRC
   python3 "$NB_SCRIPTS/nb-write.py" <notebook.ipynb> patch <index> -f /tmp/nb_patch_source.txt
   ```
   Use `NBCELLSRC` as the heredoc delimiter (not `EOF`) — it is extremely unlikely to appear in notebook source. If the source does contain `NBCELLSRC` on its own line, choose any other unique delimiter.

---

## Script location

```
NB_SCRIPTS="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/skills/nb/scripts"
```

Use this variable prefix in all commands below.

---

## Reading a notebook

```bash
NB_SCRIPTS="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/skills/nb/scripts"

# Full compact view (source only, truncated at 80 lines/cell)
python3 "$NB_SCRIPTS/nb-read.py" <notebook.ipynb>

# Show only cells 0–4
python3 "$NB_SCRIPTS/nb-read.py" <notebook.ipynb> --cells 0-4

# Show a single cell in full (no truncation)
python3 "$NB_SCRIPTS/nb-read.py" <notebook.ipynb> --cells 3 --truncate 0

# Show specific cells
python3 "$NB_SCRIPTS/nb-read.py" <notebook.ipynb> --cells 0,2,5

# Filter by cell type
python3 "$NB_SCRIPTS/nb-read.py" <notebook.ipynb> --type code
```

Output format:
```
notebook.ipynb | 12 cells | python3

[0:code] ──────────────────────────────────────────
import pandas as pd
df = pd.read_csv('data.csv')

[1:markdown] ───────────────────────────────────────
## Analysis
```

Truncation warnings appear on **stderr** (not in stdout), so they are clearly distinguished from cell source content.

---

## Patching a cell (replacing its source)

**Step 1** — Write the new source to a temp file via Bash (use `NBCELLSRC` as delimiter, not `EOF`):
```bash
NB_SCRIPTS="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/skills/nb/scripts"
python3 -c "import sys; open('/tmp/nb_patch_source.txt','w').write(sys.stdin.read())" << 'NBCELLSRC'
<new cell source here>
NBCELLSRC
```

**Step 2** — Call nb-write.py with `-f`:
```bash
python3 "$NB_SCRIPTS/nb-write.py" <notebook.ipynb> patch <index> -f /tmp/nb_patch_source.txt
```

- Outputs and execution_count are cleared automatically on code cells.
- Writes are atomic (no partial writes, no `.bak` file).

---

## Inserting a new cell

**Step 1** — Write the new source to `/tmp/nb_patch_source.txt`.

**Step 2**:
```bash
NB_SCRIPTS="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/skills/nb/scripts"

# Insert a code cell before index 3
python3 "$NB_SCRIPTS/nb-write.py" <notebook.ipynb> insert 3 code -f /tmp/nb_patch_source.txt

# Append a markdown cell at the end (-1 = append)
python3 "$NB_SCRIPTS/nb-write.py" <notebook.ipynb> insert -1 markdown -f /tmp/nb_patch_source.txt
```

Cell types: `code` | `markdown` | `raw`

⚠️ **After inserting, re-read the notebook before any further edits (Rule 4).**

---

## Deleting a cell

```bash
NB_SCRIPTS="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/skills/nb/scripts"
python3 "$NB_SCRIPTS/nb-write.py" <notebook.ipynb> delete <index>
```

⚠️ **After deleting, re-read the notebook before any further edits (Rule 4).**

---

## Full workflow example

```bash
NB_SCRIPTS="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/skills/nb/scripts"

# 1. Read the notebook to find the right cell index
python3 "$NB_SCRIPTS/nb-read.py" analysis.ipynb

# 2. If cell 4 is truncated and you need to patch it, read it in full first
python3 "$NB_SCRIPTS/nb-read.py" analysis.ipynb --cells 4 --truncate 0

# 3. Write new source to temp file (avoids heredoc EOF issues)
#    [use the Write tool here]

# 4. Patch
python3 "$NB_SCRIPTS/nb-write.py" analysis.ipynb patch 4 -f /tmp/nb_patch_source.txt

# 5. Verify the patch (Rule 6)
python3 "$NB_SCRIPTS/nb-read.py" analysis.ipynb --cells 4
```

---

## Notes

- **JSON indentation:** `nb-write.py` normalises notebook indentation to 1 space on first write. This is intentional for compactness but produces a large initial diff in git. This is expected behaviour.
- **Atomic writes:** No `.bak` file is created. Writes are all-or-nothing via temp file + rename.
- **nbformat compatibility:** Scripts require nbformat 4. nbformat 3 notebooks (with `worksheets`) are not supported — convert first with `jupyter nbconvert`.
