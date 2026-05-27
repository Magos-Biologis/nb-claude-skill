# nb — Jupyter Notebook skill for Claude Code

A Claude Code skill that lets Claude read and edit Jupyter notebooks **token-efficiently**, without decimating your context window on raw `.ipynb` JSON.

## Why

A typical notebook with 20 cells can be 50 000+ tokens as raw JSON. `nb-read.py` renders the same notebook as ~400 tokens of indexed cell source. `nb-write.py` makes surgical edits — patch, insert, or delete one cell at a time — without ever loading the whole file into Claude's context as editable text.

A `PreToolUse` hook enforces this: if Claude tries to `Read` or `Edit` a `.ipynb` file directly, the operation is blocked at the harness level and Claude is redirected to the scripts instead.

## Quick start

```bash
git clone git@github.com:you/nb-claude-skill  # or however you host this
cd nb-claude-skill
bash install.sh
# Restart Claude Code, then open any .ipynb file
```

## Requirements

- Claude Code (any recent version)
- `jq` (used by `nb-guard.sh` and `install.sh`)
- Python 3.8+ (standard library only — no extra packages)

## What gets installed

```
~/.claude/skills/nb/
├── SKILL.md              ← auto-loaded by Claude Code as a skill
├── scripts/
│   ├── nb-read.py        ← token-efficient notebook reader
│   ├── nb-write.py       ← surgical cell editor (patch / insert / delete)
│   └── nb-guard.sh       ← PreToolUse hook script
└── tests/                ← full test suite, runs against the installed scripts
```

`install.sh` also patches `~/.claude/settings.json` to register `nb-guard.sh`
as a `PreToolUse` hook on `Read|Edit|Write|MultiEdit`.

## Repository layout

```
Notebookskill/
├── README.md
├── install.sh            ← copies files + patches settings.json (idempotent)
├── uninstall.sh          ← removes files + hook entry
├── SKILL.md
├── scripts/
│   ├── nb-guard.sh
│   ├── nb-read.py
│   └── nb-write.py
└── tests/
    ├── test_scripts.py           ── nb-read.py + nb-write.py core behaviour
    ├── test_encoding.py          ── latin-1 / non-UTF-8 source files
    ├── test_read_independent.py  ── nb-read.py spec-only black-box tests
    ├── test_write_independent.py ── nb-write.py spec-only black-box tests
    ├── test_nb_guard_hook.py     ── nb-guard.sh behaviour + settings registration
    └── test_nb_guard_hardened.py ── security / injection / MultiEdit tests
```

## Running the tests

Tests run against the **repo's own scripts** — no install needed:

```bash
cd Notebookskill
pip install pytest   # once
pytest tests/ -q
```

Post-install verification (checks `settings.json` too):

```bash
pytest ~/.claude/skills/nb/tests/ -q
```

## Updating

```bash
cd Notebookskill
git pull
bash install.sh   # idempotent — safe to re-run
```

`install.sh` detects if the hook is already registered and skips the
`settings.json` patch if nothing has changed.

## Uninstalling

```bash
bash uninstall.sh
```

Removes `~/.claude/skills/nb/` and the hook entry from `settings.json`.
Restart Claude Code afterwards.

## How it works

### The skill (`SKILL.md`)

Claude Code auto-loads `SKILL.md` and activates it when it detects you're
working with `.ipynb` files. The skill's rules instruct Claude to:

1. Never use `Read`/`Edit` directly on `.ipynb` files
2. Always read immediately before writing (stale indices cause silent data loss)
3. Use `-f <file>` instead of heredocs (avoids `EOF`-on-its-own-line truncation)
4. Re-read after every structural change (`insert`/`delete` shifts indices)
5. Verify each write by re-reading the affected cell

### The hook (`nb-guard.sh`)

Runs on every `Read|Edit|Write|MultiEdit` tool call. Exits immediately (0 = allow)
for non-`.ipynb` targets. For `.ipynb` targets, prints a redirect message and
exits 1 (block).

Key design decisions:

- **No `if` conditions in `settings.json`** — per-tool glob conditions like
  `Read(*.ipynb)` only match same-directory files; subdirectory paths like
  `notebooks/analysis.ipynb` are bypassed. The script checks the path itself.
- **MultiEdit uses `edits[]`** — the harness places MultiEdit file paths in
  `tool_input.edits[].file_path`, not `tool_input.file_path`. The script handles
  this separately.
- **Injection hardening** — file paths are sanitised before echoing (strips C0
  control characters including ANSI ESC and newlines) to prevent prompt injection.
- **Fail open** — if the JSON payload can't be parsed, the script exits 0 (allow)
  rather than blocking all file I/O.

### The scripts

`nb-read.py` renders notebooks as indexed, truncated cell source (default 80
lines/cell). All status messages go to stderr; stdout is pure cell content.

`nb-write.py` makes atomic edits: `patch`, `insert`, or `delete` one cell. Writes
use `tempfile.mkstemp` + `fsync` + `os.replace` — no partial writes, no `.bak`.

## Custom config directory

If you use a non-default Claude config directory:

```bash
CLAUDE_CONFIG_DIR=/path/to/config bash install.sh
```

The `settings.json` hook command is written with the **expanded absolute path**
at install time, so it works even if `CLAUDE_CONFIG_DIR` is not set at runtime.
