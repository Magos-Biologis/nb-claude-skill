# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Purpose

This repo is the **nb** Claude Code skill — a token-efficient Jupyter notebook interface. Raw `.ipynb` files are 10–50× larger in tokens than needed; this skill renders them compactly and enables surgical cell-level edits. The skill lives here during development and is installed into `~/.claude/skills/nb/` for use.

## Running Tests

```bash
pytest tests/ -q                  # all tests
pytest tests/test_scripts.py -q   # core read/write behaviour
pytest tests/test_nb_guard_hook.py tests/test_nb_guard_hardened.py -q  # hook tests
```

No external dependencies — only Python stdlib + `pytest`. No `requirements.txt` needed.

## Installation / Uninstall

```bash
bash install.sh        # idempotent; copies files into ~/.claude/skills/nb/ and patches settings.json
bash uninstall.sh      # reverses the above
```

After install, restart Claude Code and verify with:
```bash
pytest ~/.claude/skills/nb/tests/ -q
```

Custom config dir: `CLAUDE_CONFIG_DIR=/path/to/config bash install.sh`

## Architecture

Three cooperating components:

| Script | Role |
|--------|------|
| `scripts/nb-read.py` | Presentation — renders notebook cells as indexed, truncated plain text |
| `scripts/nb-write.py` | Persistence — atomically patches, inserts, or deletes individual cells |
| `scripts/nb-guard.sh` | Access control — `PreToolUse` hook that blocks direct `Read`/`Edit`/`Write`/`MultiEdit` on `.ipynb` files and redirects Claude to use the scripts |

`SKILL.md` defines the 9 behavioural rules Claude must follow when the skill activates (never read raw JSON, always re-read after insert/delete because indices shift, use `-f <file>` not heredocs, etc.).

### nb-read.py

```
python3 scripts/nb-read.py <notebook.ipynb> [--cells N | N-M | N,M,K] [--type code|markdown|raw] [--truncate N]
```

Default truncation is 80 lines/cell. Truncation warnings go to **stderr only**. Output format:

```
notebook.ipynb | 12 cells | python3

[0:code] ──────────────────────
import pandas as pd
```

### nb-write.py

```
python3 scripts/nb-write.py <notebook> patch  <index>        -f /tmp/source.txt
python3 scripts/nb-write.py <notebook> insert <index> <type> -f /tmp/source.txt
python3 scripts/nb-write.py <notebook> delete <index>
```

Writes are atomic: temp file → fsync → `os.replace`. Rejects symlinks. Generates random 8-char cell IDs (required by nbformat ≥ 4.5). All status messages go to **stderr**; stdout is silent on success.

### nb-guard.sh

A `PreToolUse` hook registered in `~/.claude/settings.json`. It checks every `Read`/`Edit`/`Write`/`MultiEdit` call; if the target is a `.ipynb` file it exits 1 with a redirect message. The check is done inside the script (not via settings.json glob conditions) so subdirectory paths like `notebooks/analysis.ipynb` are caught correctly.

**Fail-open:** if `jq` parsing fails, exits 0 to avoid blocking all file I/O.

## Key Constraints / Invariants

- Supports **nbformat 4 only** (rejects v3 and malformed files).
- Rejects files **> 100 MB**.
- Handles UTF-8 BOM transparently; falls back to latin-1 with a warning if the file is not valid UTF-8.
- Sanitises ANSI escape sequences and control characters from any user-supplied content echoed back (prevents injection in error messages).
- `nb-write.py patch` **clears outputs and execution_count** on code cells — intentional, matches Jupyter convention.

## Test Coverage

| File | What it tests |
|------|--------------|
| `test_scripts.py` | Core read/write happy paths and flags |
| `test_encoding.py` | UTF-8 BOM, latin-1 fallback |
| `test_read_independent.py` | Full nb-read.py spec (cell filtering, truncation, edge cases) |
| `test_write_independent.py` | Full nb-write.py spec (patch/insert/delete, atomicity, cell IDs) |
| `test_nb_guard_hook.py` | Hook exit codes, settings.json registration |
| `test_nb_guard_hardened.py` | ANSI/newline injection, subdirectory path bypass, MultiEdit payloads, jq failures |
