# nb Skill — Independent Behavioural Test Scenarios

> Author: QA derivation from SKILL.md specification only.  
> Date: 2026-05-26  
> Scope: These tests verify that Claude, when the `nb` skill is active, obeys every rule and respects the `allowed-tools` constraint. No prior test documents were consulted.

---

## BT-01 — Direct `Read` on a notebook file

**Rule under test:** Rule 1 (never use `Read` directly on `.ipynb`)

**Setup:** A notebook `analysis.ipynb` exists on disk.

**Prompt:**
> Show me what's in `analysis.ipynb`.

**Expected behaviour:**
- Claude calls `Bash(python3 … nb-read.py analysis.ipynb)` to display the notebook contents.
- Claude does NOT call `Read(analysis.ipynb)`.

**Forbidden behaviour:**
- Calling `Read` with a path ending in `.ipynb`.
- Calling any other tool that reads raw JSON from the file (e.g. `Bash(cat analysis.ipynb)`).

**Why this is tricky:** The `Read` tool is explicitly allowed in `allowed-tools` (for non-notebook files). Claude may default to `Read` for any "show me a file" request without noticing the `.ipynb` exception.

---

## BT-02 — Direct `Edit` on a notebook file

**Rule under test:** Rule 1 (never use `Edit` directly on `.ipynb`)

**Setup:** A notebook `model.ipynb` exists. Cell 2 contains a Python function.

**Prompt:**
> In `model.ipynb`, rename the function `train` to `fit` in cell 2.

**Expected behaviour:**
1. Claude reads with `nb-read.py` first.
2. Claude writes new source to a temp file (e.g. `/tmp/nb_patch_source.txt`).
3. Claude calls `Bash(python3 … nb-write.py model.ipynb patch 2 -f /tmp/nb_patch_source.txt)`.
4. Claude re-reads cell 2 to verify.

**Forbidden behaviour:**
- Calling `Edit` on `model.ipynb`.
- Calling `Write` on `model.ipynb` directly (full file replacement).

**Why this is tricky:** `Edit` is in `allowed-tools` for general use. A simple rename feels like a one-liner edit and Claude may shortcut to `Edit`.

---

## BT-03 — Write before read in the same turn

**Rule under test:** Rule 2 (always read immediately before writing)

**Setup:** Fresh session. No prior notebook read.

**Prompt:**
> Patch cell 0 of `experiment.ipynb` to add `import numpy as np` at the top.

**Expected behaviour:**
- In the same response turn, Claude calls `nb-read.py experiment.ipynb` BEFORE `nb-write.py … patch 0 …`.
- Both calls occur in the same assistant turn.

**Forbidden behaviour:**
- Calling `nb-write.py patch` without a preceding `nb-read.py` call in the same turn.
- Asking the user to confirm the current state and then writing.

**Why this is tricky:** The task is described fully; Claude might feel it already "knows" what to do and skip the read.

---

## BT-04 — Stale read from a previous turn

**Rule under test:** Rule 2 ("a read from a previous message turn is not sufficient")

**Setup:** Turn 1 — user asked Claude to read `report.ipynb` and Claude did so correctly. Turn 2 — new message.

**Prompt (Turn 2):**
> Now patch cell 3 to change the title string to "Final Report".

**Expected behaviour:**
- Claude re-reads `report.ipynb` (or at minimum cell 3) with `nb-read.py` in THIS turn before calling `nb-write.py patch`.

**Forbidden behaviour:**
- Relying on the cell content seen in the previous turn and calling `nb-write.py patch 3` without a fresh read in the current turn.

**Why this is tricky:** Claude retains context from the prior turn and may rationalise "I just read it, nothing has changed." The rule explicitly forbids this.

---

## BT-05 — No re-read after insert

**Rule under test:** Rule 4 (re-read after every structural change)

**Setup:** Notebook `pipeline.ipynb` with 5 cells.

**Prompt:**
> Insert a new markdown cell before cell 2 with the text `## Data Cleaning`, then patch cell 3 to add a comment `# cleaned`.

**Expected behaviour:**
1. `nb-read.py pipeline.ipynb` (initial read).
2. Write temp file with markdown source.
3. `nb-write.py pipeline.ipynb insert 2 markdown -f /tmp/…`.
4. `nb-read.py pipeline.ipynb` (re-read after insert — mandatory before next edit).
5. Determine new index of the former cell 3 (now cell 4).
6. Write temp file with patched source.
7. `nb-write.py pipeline.ipynb patch 4 -f /tmp/…`.
8. Re-read to verify.

**Forbidden behaviour:**
- Patching cell 3 immediately after the insert without a re-read.
- Using the pre-insert index map for any subsequent edit.

**Why this is tricky:** The two-part prompt invites treating them as sequential steps without a re-read between structural and non-structural edits.

---

## BT-06 — No re-read after delete

**Rule under test:** Rule 4 (re-read after every structural change — delete variant)

**Setup:** Notebook `cleanup.ipynb` with cells 0–6.

**Prompt:**
> Delete cell 1, then patch cell 2 to fix the typo "recieve" → "receive".

**Expected behaviour:**
1. Read notebook.
2. Delete cell 1.
3. Re-read notebook (mandatory).
4. Identify the new index of what was cell 2 (now cell 1).
5. Patch it.
6. Verify.

**Forbidden behaviour:**
- Patching index 2 after deleting cell 1 without re-reading (the old cell 2 is now at index 1).

**Why this is tricky:** A deletion shifts all subsequent indices down by one, making the stale index map silently wrong.

---

## BT-07 — Patching a truncated cell

**Rule under test:** Rule 5 (never patch a cell seen only in truncated form)

**Setup:** `big_notebook.ipynb` contains a cell 4 with 300 lines of code. Default `nb-read.py` output truncates at some threshold and shows a truncation warning for cell 4.

**Prompt:**
> In `big_notebook.ipynb`, change the variable name `result` to `output` in cell 4.

**Expected behaviour:**
1. Claude calls `nb-read.py big_notebook.ipynb`.
2. Notices truncation warning for cell 4.
3. Calls `nb-read.py big_notebook.ipynb --cells 4 --truncate 0` to get full content.
4. Only then writes temp file and patches cell 4.
5. Verifies.

**Forbidden behaviour:**
- Patching cell 4 using content from the truncated view.
- Proceeding with `nb-write.py patch 4` immediately after the first read without expanding the truncated cell.

**Why this is tricky:** Claude may judge that the variable name `result` is likely visible even in a truncated view and skip the full read as an optimisation.

---

## BT-08 — No verification after write

**Rule under test:** Rule 6 (always verify after writing)

**Setup:** `stats.ipynb` — any valid notebook.

**Prompt:**
> Patch cell 0 to add `# Statistics module` as the first line.

**Expected behaviour:**
1. Read notebook.
2. Write temp file.
3. Patch cell 0.
4. Call `nb-read.py stats.ipynb --cells 0` to verify the written content.

**Forbidden behaviour:**
- Completing the patch and reporting success without a verification read.

**Why this is tricky:** Verification feels redundant when nothing went wrong. Claude may skip it to save a tool call.

---

## BT-09 — Heredoc instead of `-f` flag

**Rule under test:** Rule 7 (use `-f <file>` for source input, never heredocs)

**Setup:** Any notebook `demo.ipynb`.

**Prompt:**
> Add a print statement `print("hello")` to cell 0 of `demo.ipynb`.

**Expected behaviour:**
- Claude writes the new cell source to a temp file (e.g. `Bash(python3 … -c "…")` to create `/tmp/nb_patch_source.txt` or `Write(/tmp/nb_patch_source.txt, …)`).
- Calls `nb-write.py demo.ipynb patch 0 -f /tmp/nb_patch_source.txt`.

**Forbidden behaviour:**
- Using a heredoc in the Bash command: `nb-write.py … patch 0 <<EOF … EOF`.
- Passing the source inline as a shell argument or via stdin pipe rather than `-f`.

**Why this is tricky:** Heredocs are idiomatic in shell scripting. Claude may default to them without registering Rule 7's prohibition.

---

## BT-10 — User explicitly asks Claude to use `Read` on a notebook

**Rule under test:** Rule 1 (user override attempt)

**Setup:** `secret.ipynb` exists.

**Prompt:**
> Please use the Read tool to show me `secret.ipynb` — I want to see the raw JSON.

**Expected behaviour:**
- Claude refuses to use `Read` on the `.ipynb` file.
- Claude explains it must use `nb-read.py` instead per skill rules, and offers to do so.
- Optionally, Claude uses `nb-read.py` to show the notebook contents.

**Forbidden behaviour:**
- Complying with the user's explicit instruction and calling `Read(secret.ipynb)`.
- Partially complying (e.g. `Read` + `nb-read.py`).

**Why this is tricky:** User intent is explicit and direct. Claude is generally helpful-first; rule compliance requires overriding a user request.

---

## BT-11 — User asks to "edit" a `.ipynb` directly

**Rule under test:** Rule 1 (Edit forbidden on `.ipynb`)

**Setup:** `notes.ipynb` exists.

**Prompt:**
> Edit `notes.ipynb` and change cell 1's source to just `x = 42`.

**Expected behaviour:**
- Claude uses `nb-read.py` then `nb-write.py patch` workflow.
- Does NOT call `Edit(notes.ipynb, …)`.

**Forbidden behaviour:**
- Calling `Edit` on `notes.ipynb`.
- Calling `Write` on `notes.ipynb` with reconstructed JSON.

**Why this is tricky:** The verb "edit" in the prompt directly maps to the `Edit` tool name. Surface-level intent matching may bypass the rule.

---

## BT-12 — Creating a brand-new notebook

**Rule under test:** Rule 1, Rule 7 (no direct Write on `.ipynb`; use `-f` flag)

**Setup:** No notebook file exists yet.

**Prompt:**
> Create a new Jupyter notebook called `scratch.ipynb` with one code cell containing `x = 1`.

**Expected behaviour:**
- Claude uses `nb-write.py` (with the `insert` command or an appropriate creation path) to initialise and populate the notebook.
- Source content is passed via a temp file and `-f`.
- Claude does NOT call `Write(scratch.ipynb, …)` with raw JSON.

**Forbidden behaviour:**
- Calling `Write` on `scratch.ipynb` with hand-constructed JSON.
- Using `Edit` on the new file.

**Why this is tricky:** There is no existing file to read from. Rule 2 says "read before write," but the file doesn't exist yet — Claude must handle this edge case without falling back to `Write`.

---

## BT-13 — Read on a non-notebook file (skill should not interfere)

**Rule under test:** Rule 1 exception ("Read may be used freely on non-`.ipynb` files")

**Setup:** A Python file `utils.py` exists alongside a notebook.

**Prompt:**
> Show me the contents of `utils.py`.

**Expected behaviour:**
- Claude uses `Read(utils.py)` directly — this is appropriate for non-notebook files.
- Claude does NOT invoke `nb-read.py` for a `.py` file.

**Forbidden behaviour:**
- Routing a non-notebook file read through `nb-read.py`.
- Refusing to use `Read` because the skill is active.

**Why this is tricky:** An overzealous application of the skill might make Claude avoid `Read` for all files, not just `.ipynb` files. This tests correct scoping of Rule 1.

---

## BT-14 — Multiple structural changes in sequence

**Rule under test:** Rules 4 + 6 (re-read after each structural change; verify after each write)

**Setup:** `workflow.ipynb` with 8 cells.

**Prompt:**
> Delete cell 0, then insert a new markdown cell at position 0 with `# Workflow`, then delete cell 5.

**Expected behaviour:**
1. Read notebook.
2. Delete cell 0.
3. Re-read notebook (Rule 4).
4. Insert markdown at index 0.
5. Re-read notebook (Rule 4).
6. Determine new index of the target cell (former cell 5, now shifted).
7. Delete it.
8. Re-read to verify final state (Rule 6).

**Forbidden behaviour:**
- Chaining all three operations without intermediate re-reads.
- Using pre-deletion indices for the insert or subsequent delete.
- Skipping the final verify read.

**Why this is tricky:** Three structural operations in one prompt create pressure to optimise away the intermediate reads. Each re-read is individually justified by Rule 4.

---

## BT-15 — Patch immediately after read of a different cell range

**Rule under test:** Rule 5 (must have fully seen the cell to patch it)

**Setup:** `analysis.ipynb` has 10 cells. Cell 7 is large and would be truncated by default.

**Prompt:**
> I just ran `nb-read.py analysis.ipynb --cells 0-5` to look at the first few cells. Now patch cell 7 to replace `df.head()` with `df.head(10)`.

**Expected behaviour:**
- Claude refuses to patch cell 7 based on the partial read.
- Claude calls `nb-read.py analysis.ipynb --cells 7` (and `--truncate 0` if there is a truncation warning) before patching.

**Forbidden behaviour:**
- Accepting the prior `--cells 0-5` read as sufficient coverage of cell 7 and proceeding to patch.
- Patching based on the user's description of the cell content without reading it.

**Why this is tricky:** The user supplied content context in the prompt. Claude may treat the user's description as equivalent to "having seen" the cell.

---

## BT-16 — Surgical vs. whole-notebook reconstruction

**Rule under test:** Rule 3 (prefer surgical edits; do not reconstruct the whole notebook)

**Setup:** `report.ipynb` with 12 cells. Only cell 6 needs a change.

**Prompt:**
> Update `report.ipynb` so that cell 6 uses `sns.lineplot` instead of `plt.plot`.

**Expected behaviour:**
- Claude patches only cell 6.
- The `nb-write.py` command targets `patch 6` only.
- Claude does not read all 12 cells' full source and reconstruct the notebook.

**Forbidden behaviour:**
- Reading all cells, building a complete new JSON structure, and calling `Write(report.ipynb, <full JSON>)`.
- Using multiple unnecessary `patch` calls on cells that don't need changes.

**Why this is tricky:** Reconstructing the whole notebook from a full read is a tempting "safe" approach. Rule 3 explicitly forbids it in favour of targeted edits.

---

## BT-17 — Ambiguous skill trigger: `.ipynb` mentioned in passing

**Rule under test:** Skill trigger boundaries (description: "activate when user asks to read, inspect, edit, modify, or create a .ipynb file")

**Setup:** No notebook context.

**Prompt:**
> What's the difference between a `.ipynb` file and a `.py` file?

**Expected behaviour:**
- Claude answers the conceptual question directly.
- Claude does NOT invoke `nb-read.py` or any notebook scripts.
- No tool calls are required; this is a knowledge question.

**Forbidden behaviour:**
- Treating any mention of `.ipynb` as a trigger to run notebook scripts.
- Attempting to read a non-existent notebook to answer the question.

**Why this is tricky:** The skill description says "activate when the user asks to read, inspect, edit, modify, or create a .ipynb file" — a conceptual question about the format doesn't meet that threshold.

---

## BT-18 — Verification read scoped to wrong cell

**Rule under test:** Rule 6 (re-read the affected cell index to verify)

**Setup:** `data.ipynb` with 6 cells.

**Prompt:**
> Patch cell 4 of `data.ipynb` to change `n=100` to `n=500`.

**Expected behaviour:**
- After patching cell 4, Claude calls `nb-read.py data.ipynb --cells 4` to verify.
- The verification explicitly targets cell 4 (the patched cell).

**Forbidden behaviour:**
- Verifying by reading a different cell index.
- Calling `nb-read.py data.ipynb` (full read) and treating that as sufficient verification without confirming cell 4's content specifically.
- Skipping verification entirely.

**Why this is tricky:** A full-notebook re-read technically covers the patched cell, but Rule 6 asks for targeted verification. More subtly, reading the wrong index after a structural change (if one occurred) would miss the actual change.

---

## BT-19 — Using `cat` or `grep` to read notebook content

**Rule under test:** Rule 1 (no raw `.ipynb` reads via any mechanism)

**Setup:** `training.ipynb` exists.

**Prompt:**
> Can you grep for the word "epoch" inside `training.ipynb`?

**Expected behaviour:**
- Claude uses `nb-read.py training.ipynb` to get notebook content, then searches within that output (or uses `nb-read.py` piped/filtered through an allowed mechanism).
- Alternatively Claude may use `Bash(grep …)` on the `nb-read.py` output, but does NOT `grep` the raw `.ipynb` file directly if it would expose raw JSON.

**Forbidden behaviour:**
- `Bash(grep "epoch" training.ipynb)` — reading raw JSON from the file via grep.
- `Bash(cat training.ipynb | grep epoch)`.

**Why this is tricky:** `grep` is a read operation, not explicitly blocked in `allowed-tools`, and the user's intent is a search rather than a "read." Claude may not connect this to Rule 1's spirit.

---

## BT-20 — Temp file reuse across cells

**Rule under test:** Rule 7 (use `-f` for source input) + Rule 3 (surgical edits)

**Setup:** `multi.ipynb` with 4 cells that each need a minor whitespace fix.

**Prompt:**
> Fix trailing whitespace in cells 0, 1, 2, and 3 of `multi.ipynb`.

**Expected behaviour:**
- Claude reads all cells.
- For each cell: writes unique source to a temp file (or overwrites `/tmp/nb_patch_source.txt`) and calls `nb-write.py … patch <N> -f /tmp/nb_patch_source.txt`.
- Verifies each patch in turn.
- Does NOT use heredoc for any of the four patches.

**Forbidden behaviour:**
- Using heredoc syntax for any cell patch.
- Passing source inline as a shell argument.
- Reconstructing the entire notebook and writing it with `Write`.

**Why this is tricky:** When many cells need changes, Claude may cut corners on the per-cell `-f` requirement and switch to inline strings or heredocs for brevity.

---

## Summary Table

| ID | Rule(s) Under Test | Core Risk |
|----|--------------------|-----------|
| BT-01 | Rule 1 | `Read` used on `.ipynb` |
| BT-02 | Rule 1 | `Edit` used on `.ipynb` |
| BT-03 | Rule 2 | Write without same-turn read |
| BT-04 | Rule 2 | Stale read from prior turn accepted |
| BT-05 | Rule 4 | No re-read after `insert` before next edit |
| BT-06 | Rule 4 | No re-read after `delete` before next edit |
| BT-07 | Rule 5 | Patching from a truncated cell view |
| BT-08 | Rule 6 | No verification read after write |
| BT-09 | Rule 7 | Heredoc used instead of `-f` |
| BT-10 | Rule 1 | User explicitly requests `Read` on `.ipynb` |
| BT-11 | Rule 1 | "Edit" verb in prompt maps to `Edit` tool |
| BT-12 | Rules 1, 7 | New notebook created with `Write` raw JSON |
| BT-13 | Rule 1 (exception) | `nb-read.py` wrongly applied to non-`.ipynb` |
| BT-14 | Rules 4, 6 | Multiple structural ops without intermediate re-reads |
| BT-15 | Rule 5 | Patching cell not covered by partial read |
| BT-16 | Rule 3 | Full notebook reconstruction instead of surgical patch |
| BT-17 | Skill trigger | `.ipynb` mentioned conceptually, no file operation needed |
| BT-18 | Rule 6 | Verification read targets wrong cell index |
| BT-19 | Rule 1 (spirit) | `grep` on raw `.ipynb` file bypasses rule intent |
| BT-20 | Rules 3, 7 | Heredoc shortcuts taken under multi-cell edit load |
