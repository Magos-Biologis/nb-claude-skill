# nb — Jupyter Notebook skill for Claude Code

A Claude Code skill that lets Claude read and edit Jupyter notebooks **token-efficiently**, without decimating your context window on raw `.ipynb` JSON.

## Why

A typical notebook with 20 cells can be 50 000+ tokens as raw JSON. `nb-read.py` renders the same notebook as ~400 tokens of indexed cell source. `nb-write.py` makes surgical edits — patch, insert, or delete one cell at a time — without ever loading the whole file into Claude's context as editable text.

A `PreToolUse` hook enforces this: if Claude tries to `Read` or `Edit` a `.ipynb` file directly, the operation is blocked at the harness level and Claude is redirected to the scripts instead.

## Requirements

- Claude Code (any recent version)
- Python 3.8+ (standard library only — no extra packages, no `jq`, no `bash`)
- `pytest` for running the test suite

## Quick start

```bash
# Linux / macOS
git clone <repo-url>
cd nb-claude-skill
python3 install.py
# Restart Claude Code, then open any .ipynb file

# Windows (PowerShell)
git clone <repo-url>
cd nb-claude-skill
python install.py
# Restart Claude Code, then open any .ipynb file
```

## What gets installed

```
~/.claude/skills/nb/         (Windows: %USERPROFILE%\.claude\skills\nb\)
├── SKILL.md                 ← auto-loaded by Claude Code as a skill
├── install.py               ← re-runnable installer (idempotent)
├── uninstall.py             ← removes files + hook entry
├── scripts/
│   ├── nb-guard.py          ← PreToolUse hook (blocks direct .ipynb access)
│   ├── nb-read.py           ← token-efficient notebook reader
│   ├── nb-write.py          ← surgical cell editor (patch / insert / delete / create)
│   ├── nb-index.py          ← persistent JSON index builder (fire-and-forget)
│   └── nb-search.py         ← cross-notebook keyword / symbol / import search
└── tests/                   ← full test suite, runs against the installed scripts
```

`install.py` also patches `~/.claude/settings.json` to register `nb-guard.py` as a `PreToolUse` hook on `Read|Edit|Write|MultiEdit`.

## Repository layout

```
nb-claude-skill/
├── README.md
├── SKILL.md
├── CLAUDE.md                ← guidance for Claude Code when working in this repo
├── install.py               ← copies files + patches settings.json (idempotent)
├── uninstall.py             ← removes files + hook entry
├── _nb_install_common.py    ← shared utilities for install.py / uninstall.py
├── scripts/
│   ├── nb-guard.py
│   ├── nb-read.py
│   ├── nb-write.py
│   ├── nb-index.py
│   ├── nb-search.py
│   └── nb-guard.sh          ← legacy POSIX fallback (not the default)
├── tests/
│   ├── test_scripts.py
│   ├── test_encoding.py
│   ├── test_read_independent.py
│   ├── test_read_safe.py
│   ├── test_read_outline.py
│   ├── test_read_outputs.py
│   ├── test_write_independent.py
│   ├── test_write_new.py
│   ├── test_nb_guard_py.py
│   ├── test_nb_guard_hook.py
│   ├── test_nb_guard_hardened.py
│   ├── test_nb_index.py
│   ├── test_nb_search.py
│   ├── test_windows_compat.py
│   └── test_install.py
└── TDD.md / TDD_INDEX.md    ← technical design documents
```

## Running the tests

Tests run against the **repo's own scripts** — no install needed:

```bash
# Linux / macOS
pytest tests/ -q

# Windows
python -m pytest tests/ -q
```

Post-install verification (also checks `settings.json` registration):

```bash
pytest ~/.claude/skills/nb/tests/ -q
```

## Custom config directory

```bash
# Linux / macOS
CLAUDE_CONFIG_DIR=/path/to/config python3 install.py

# Windows (PowerShell)
$env:CLAUDE_CONFIG_DIR = "C:\path\to\config"; python install.py
```

The `settings.json` hook command is written with the **expanded absolute path** at install time, so it works even if `CLAUDE_CONFIG_DIR` is not set at runtime.

## Updating

```bash
git pull
python3 install.py   # idempotent — safe to re-run
```

## Uninstalling

```bash
# Linux / macOS
python3 uninstall.py

# Windows
python uninstall.py
```

Removes `~/.claude/skills/nb/` and the hook entry from `settings.json`. Restart Claude Code afterwards.

## How it works

### The skill (`SKILL.md`)

Claude Code auto-loads `SKILL.md` and activates it when it detects you're working with `.ipynb` files. The skill's rules instruct Claude to:

1. Never use `Read`/`Edit` directly on `.ipynb` files
2. Always read immediately before writing (stale indices cause silent data loss)
3. Use `-f <file>` instead of heredocs (avoids `EOF`-on-its-own-line truncation)
4. Re-read after every structural change (`insert`/`delete` shifts indices)
5. Verify each write by re-reading the affected cell

### The hook (`nb-guard.py`)

Runs on every `Read|Edit|Write|MultiEdit` tool call. Exits immediately (0 = allow) for non-`.ipynb` targets. For `.ipynb` targets, prints a redirect message and exits 1 (block).

Key design decisions:

- **No `if` conditions in `settings.json`** — per-tool glob conditions like `Read(*.ipynb)` only match same-directory files; subdirectory paths like `notebooks/analysis.ipynb` are bypassed. The script checks the path itself.
- **MultiEdit uses `edits[]`** — the harness places MultiEdit file paths in `tool_input.edits[].file_path`, not `tool_input.file_path`. The script handles this separately.
- **Injection hardening** — file paths are sanitised before echoing (strips C0 control characters and ANSI sequences) to prevent prompt injection.
- **Fail open** — if the JSON payload can't be parsed, the script exits 0 (allow) rather than blocking all file I/O.
- **Cross-platform** — pure Python stdlib, no `jq` or `bash` required.

### The scripts

`nb-read.py` renders notebooks as indexed, truncated cell source (default 80 lines/cell). All status messages go to stderr; stdout is pure cell content. Source lines are prefixed with `│ ` to prevent cell content from being mistaken for cell boundary markers.

`nb-write.py` makes atomic edits: `patch`, `insert`, `delete`, or `create`. Writes use `tempfile.mkstemp` + `fsync` + `os.replace` — no partial writes, no `.bak`. After each write, it fire-and-forgets `nb-index.py` to keep the index current.

`nb-index.py` builds a compact JSON index per notebook (cell metadata, first lines, sections, symbols, output text) stored under `.nb_index/`. Enables fast outline, output, and search queries without re-parsing the full notebook JSON.

`nb-search.py` searches across all indexed notebooks in a directory tree for keywords, symbol definitions, or import statements.
