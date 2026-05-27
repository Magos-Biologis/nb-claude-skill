# nb Skill — Behavior Tests

Behavioral assertions for the `nb` skill. These cannot be automated with pytest
because they test *what Claude does*, not what the scripts do. Run them manually
by pasting each prompt into a session where the `nb` skill is active.

For each scenario: ✅ pass if Claude does the **expected** action, ❌ fail if it
does any **forbidden** action.

---

## BT-01 — Must not use `Read` on a .ipynb file

**Prompt:**
> "Show me what's in `analysis.ipynb`"

**Expected:** Runs `nb-read.py analysis.ipynb` via Bash.

**Forbidden:** Calls the `Read` tool with `analysis.ipynb` as the path.

---

## BT-02 — Must not use `Edit` on a .ipynb file

**Prompt:**
> "In `analysis.ipynb`, change cell 2 to print 'hello world'"

**Expected:** Reads with `nb-read.py`, writes new source to a temp file, calls `nb-write.py patch 2 -f ...`, verifies with `nb-read.py --cells 2`.

**Forbidden:** Calls the `Edit` tool with `analysis.ipynb`.

---

## BT-03 — Must re-read immediately before patching (same turn)

**Setup:** In a previous turn, ask Claude to read `analysis.ipynb`. Then in a new turn:

**Prompt:**
> "Now patch cell 0 to say `x = 99`"

**Expected:** Claude re-reads `analysis.ipynb` in this turn *before* calling `nb-write.py`.

**Forbidden:** Claude patches using the index from the previous turn's read without re-reading.

---

## BT-04 — Must re-read after insert before further edits

**Prompt:**
> "In `analysis.ipynb`, insert a new code cell `setup()` at index 1, then patch cell 3 to say `cleanup()`"

**Expected:** Insert at 1 → re-read → patch the (now shifted) cell → verify.

**Forbidden:** Insert at 1, then immediately patch "cell 3" using the pre-insert index map.

---

## BT-05 — Must re-read truncated cell before patching

**Setup:** A notebook where cell 0 has 120 lines of code.

**Prompt:**
> "Fix the bug in cell 0 of `big.ipynb`"

**Expected:** Claude reads with default truncation, sees the TRUNCATED warning on stderr, then re-reads `--cells 0 --truncate 0` before constructing a patch.

**Forbidden:** Claude constructs a patch based on the 80-line truncated view, discarding the hidden 40 lines.

---

## BT-06 — Must verify after every write

**Prompt:**
> "Patch cell 2 of `analysis.ipynb` to say `result = df.mean()`"

**Expected:** After `nb-write.py patch 2`, Claude runs `nb-read.py --cells 2` to confirm.

**Forbidden:** Claude stops after the write without a verification read.

---

## BT-07 — Must use -f flag, not heredoc

**Prompt:**
> "Add a new code cell to `analysis.ipynb` with: `EOF = 'end of file'\nprint(EOF)`"

**Expected:** Claude writes the source to a temp file (Write tool or Bash), then calls `nb-write.py insert ... -f /tmp/...`.

**Forbidden:** Claude uses a heredoc `<< 'EOF'` to pipe source containing the word `EOF`.

---

## BT-08 — Must not activate on casual mention

**Prompt:**
> "I was looking at `old_analysis.ipynb` last week and I think the approach was flawed"

**Expected:** Claude responds conversationally without activating the nb skill or restricting its tool set.

**Forbidden:** Claude activates the nb skill (restricting tools to only Bash/Read/Write) for a casual conversational mention.

---

## BT-09 — Read tool allowed for non-.ipynb files

**Prompt:**
> "In `analysis.ipynb`, check if the function `load_data` referenced in cell 1 is defined in `utils.py`"

**Expected:** Uses `nb-read.py` for the notebook, uses `Read` directly for `utils.py`.

**Forbidden:** Tries to use `nb-read.py` on `utils.py`, or refuses to read `utils.py` because the skill is active.

---

## Running these tests

Each test should be run in a fresh Claude Code session with the `nb` skill available.
After each test, note pass/fail and any deviation in the actual behaviour.

### Scoring

| Tests passed | Assessment |
|-------------|------------|
| 9/9 | Skill is behaving correctly |
| 7–8/9 | Minor prompt tuning needed |
| < 7/9 | Rule wording needs revision — re-run the adversarial audit |
