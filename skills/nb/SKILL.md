---
name: nb
description: Read and edit Jupyter notebooks token-efficiently. Activate when the user asks to read, inspect, edit, modify, or create a .ipynb file — use nb-read.py instead of Read, and nb-write.py instead of Edit/Write.
argument-hint: <notebook.ipynb>
allowed-tools: Bash(python3 *) Bash(python *) Bash(ls *) Bash(find *) Read Write
---

## Rules — follow these strictly

1. **Never use `Read` or `Edit` directly on a `.ipynb` file.** Raw notebook JSON is 10–50× more tokens than needed. Always use the scripts below. (`Read` may be used freely on non-`.ipynb` files.)

2. **Always read immediately before writing.** Run `nb-read.py` in the same turn, right before any `nb-write.py` call. A read from a previous message turn is not sufficient — the file may have changed.

3. **Prefer surgical edits.** Patch one cell at a time. Do not reconstruct the whole notebook.

4. **Re-read after every structural change.** After any `insert` or `delete`, you MUST re-read the notebook before issuing further `patch`, `insert`, or `delete` commands. Indices shift after every structural change and your previous index map is stale.

5. **Never patch a cell you have not fully seen.** If `nb-read.py` output shows a truncation warning for a cell you intend to patch, re-read that cell first with `--truncate 0`. Patching from a truncated view destroys the hidden lines with no error.

6. **Always verify after writing.** After every `patch`, `insert`, or `delete`, re-read the affected cell index with `--cells <N>` to confirm the written content matches your intent.

7. **Use `-f <file>` for source input — never heredocs.** If a cell source contains the word `EOF` on its own line, a heredoc silently truncates the input. Use the `Write` tool to create a temp file, then pass `-f` to `nb-write.py`:
   ```bash
   # Write the source with the Write tool (platform-agnostic, no heredoc risk)
   # → writes to $TMPDIR/nb_patch_source.txt (or any temp path)
   python3 "$NB_SCRIPTS/nb-write.py" <notebook.ipynb> patch <index> -f "$TMPDIR/nb_patch_source.txt"
   ```

8. **Cell source lines are prefixed with `│ ` in read output.** Do not include this prefix when writing patches. It is added by `nb-read.py` for structural safety and is stripped automatically.

---

## Script location

The plugin manager installs scripts into a versioned cache directory. Resolve the path
dynamically from the plugin registry — do **not** hardcode it:

```bash
# Linux / macOS — resolves correctly regardless of version or marketplace
NB_SCRIPTS=$(python3 -c "
import json, pathlib, os
cfg = pathlib.Path(os.environ.get('CLAUDE_CONFIG_DIR', str(pathlib.Path.home() / '.claude')))
reg = json.loads((cfg / 'plugins' / 'installed_plugins.json').read_text())
print(next(v[0]['installPath'] for k, v in reg['plugins'].items() if k.startswith('nb@')) + '/scripts')
")

# Windows (PowerShell)
$NB_SCRIPTS = (python3 -c "
import json, pathlib, os
cfg = pathlib.Path(os.environ.get('CLAUDE_CONFIG_DIR', str(pathlib.Path.home() / '.claude')))
reg = json.loads((cfg / 'plugins' / 'installed_plugins.json').read_text())
print(next(v[0]['installPath'] for k, v in reg['plugins'].items() if k.startswith('nb@')) + '/scripts')
")
```

Use this `$NB_SCRIPTS` prefix in all commands below.

**Python command:** Use `python3` on Linux/macOS. Use `py -3` (Python Launcher) or `python` on Windows.

---

## Creating a new notebook

```bash
python3 "$NB_SCRIPTS/nb-write.py" <notebook.ipynb> create
```

- Creates an empty nbformat 4.5 notebook.
- Fails if the file already exists.
- After creating, use `insert` to add cells.

---

## Reading a notebook

```bash
NB_SCRIPTS=$(python3 -c "import json,pathlib,os; cfg=pathlib.Path(os.environ.get('CLAUDE_CONFIG_DIR',str(pathlib.Path.home()/'.claude'))); reg=json.loads((cfg/'plugins'/'installed_plugins.json').read_text()); print(next(v[0]['installPath'] for k,v in reg['plugins'].items() if k.startswith('nb@'))+'/scripts')")

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

**Output format:**
```
notebook.ipynb | 12 cells | python3

[0:code] ──────────────────────────────────────────
│ import pandas as pd
│ df = pd.read_csv('data.csv')
[cell has 2 output(s), 5 lines — not shown]

[1:markdown] ───────────────────────────────────────
│ ## Analysis
```

**Important:**
- Source lines are prefixed with `│ `. Do NOT include this prefix in patches.
- **Cell outputs (stdout, stderr, tracebacks) are not rendered** — only `source` is shown. If a cell produced output, a summary line `[cell has N output(s), M lines — not shown]` appears after the source.
- Truncation warnings appear on **stderr** (not in stdout).

---

## Patching a cell (replacing its source)

**Step 1** — Write the new source using the `Write` tool (preferred: no heredoc risk):

**Step 2** — Call nb-write.py with `-f`:
```bash
NB_SCRIPTS=$(python3 -c "import json,pathlib,os; cfg=pathlib.Path(os.environ.get('CLAUDE_CONFIG_DIR',str(pathlib.Path.home()/'.claude'))); reg=json.loads((cfg/'plugins'/'installed_plugins.json').read_text()); print(next(v[0]['installPath'] for k,v in reg['plugins'].items() if k.startswith('nb@'))+'/scripts')")
python3 "$NB_SCRIPTS/nb-write.py" <notebook.ipynb> patch <index> -f /path/to/source.txt
```

- Outputs and execution_count are cleared automatically on code cells.
- Writes are atomic (no partial writes, no `.bak` file).

---

## Inserting a new cell

**Step 1** — Write the new source using the `Write` tool.

**Step 2:**
```bash
NB_SCRIPTS=$(python3 -c "import json,pathlib,os; cfg=pathlib.Path(os.environ.get('CLAUDE_CONFIG_DIR',str(pathlib.Path.home()/'.claude'))); reg=json.loads((cfg/'plugins'/'installed_plugins.json').read_text()); print(next(v[0]['installPath'] for k,v in reg['plugins'].items() if k.startswith('nb@'))+'/scripts')")

# Insert a code cell before index 3
python3 "$NB_SCRIPTS/nb-write.py" <notebook.ipynb> insert 3 code -f /path/to/source.txt

# Append a markdown cell at the end (-1 = append)
python3 "$NB_SCRIPTS/nb-write.py" <notebook.ipynb> insert -1 markdown -f /path/to/source.txt
```

Cell types: `code` | `markdown` | `raw`

⚠️ **After inserting, re-read the notebook before any further edits (Rule 4).**

---

## Deleting a cell

```bash
NB_SCRIPTS=$(python3 -c "import json,pathlib,os; cfg=pathlib.Path(os.environ.get('CLAUDE_CONFIG_DIR',str(pathlib.Path.home()/'.claude'))); reg=json.loads((cfg/'plugins'/'installed_plugins.json').read_text()); print(next(v[0]['installPath'] for k,v in reg['plugins'].items() if k.startswith('nb@'))+'/scripts')")
python3 "$NB_SCRIPTS/nb-write.py" <notebook.ipynb> delete <index>
```

⚠️ **After deleting, re-read the notebook before any further edits (Rule 4).**

---

## Full workflow example

```bash
NB_SCRIPTS=$(python3 -c "import json,pathlib,os; cfg=pathlib.Path(os.environ.get('CLAUDE_CONFIG_DIR',str(pathlib.Path.home()/'.claude'))); reg=json.loads((cfg/'plugins'/'installed_plugins.json').read_text()); print(next(v[0]['installPath'] for k,v in reg['plugins'].items() if k.startswith('nb@'))+'/scripts')")

# 1. Read the notebook to find the right cell index
python3 "$NB_SCRIPTS/nb-read.py" analysis.ipynb

# 2. If cell 4 is truncated and you need to patch it, read it in full first
python3 "$NB_SCRIPTS/nb-read.py" analysis.ipynb --cells 4 --truncate 0

# 3. Write new source using the Write tool (saves to /tmp/nb_patch_source.txt)

# 4. Patch
python3 "$NB_SCRIPTS/nb-write.py" analysis.ipynb patch 4 -f /tmp/nb_patch_source.txt

# 5. Verify the patch (Rule 6)
python3 "$NB_SCRIPTS/nb-read.py" analysis.ipynb --cells 4
```

---

## Notes

- **JSON indentation:** `nb-write.py` normalises notebook indentation to 1 space on every write. This produces a large diff on first edit. This is intentional and expected.
- **Atomic writes:** No `.bak` file is created. Writes are all-or-nothing via temp file + rename.
- **nbformat compatibility:** Scripts require nbformat 4. nbformat 3 notebooks (with `worksheets`) are not supported — convert first with `jupyter nbconvert`.
- **Known limitation:** The nb-guard hook covers `Read`/`Edit`/`Write`/`MultiEdit`. Reading a notebook via the `Bash` tool (e.g. `cat notebook.ipynb`) bypasses the guard. Avoid this — raw JSON is verbose and unindexed.
- **Windows:** Use `py -3` or `python` instead of `python3`. Resolve `NB_SCRIPTS` with the same `installed_plugins.json` lookup above.
