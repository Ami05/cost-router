# Prompt Scenario: HTML Re-Read & Indexing Behaviour
**Run this file by pasting it as a prompt to Claude Code (or any Claude Code session with the cost-router plugin active).**

---

## Context

This scenario validates that the cost-router re-read gate and file-graph indexing work correctly during a realistic website-building session.

The file under test is:
```
tests/prompt_scenarios/fixtures/index.html
```

You are acting as an agent building a website. Follow each step exactly. After every action that involves a Read or Edit, record what you observe in the **Observation** field below that step.

---

## Scenario Steps

### STEP 1 — First Read (baseline)

**Action:** Read the full file.
```
Read: tests/prompt_scenarios/fixtures/index.html
```

**What to observe:**
- The read is allowed with no nudge or warning.
- The `additionalContext` injected by the hook shows a symbol map, e.g.:
  `"local graph for index.html: N symbols — title: My Portfolio Website (L6), nav#main-nav (L17-21), section#hero (L24-27)..."`

**PASS criteria:**
- [ ] Read allowed (no deny, no agent_message nudge)
- [ ] Symbol map appears in additionalContext with element names and line numbers

**Observation:** _(record what you saw)_

---

### STEP 2 — Add a footer section (edit)

**Action:** Add a footer to the HTML file. Insert the following block before `</body>`:
```html
  <!-- FOOTER -->
  <footer id="site-footer">
    <p>&copy; 2026 Alex. All rights reserved.</p>
  </footer>
```

**What to observe:**
- Edit is allowed by the gate.
- After the edit, the graph is automatically marked stale by the `mark_graph_stale` hook.

**PASS criteria:**
- [ ] Edit succeeds
- [ ] No denial from gate

**Observation:** _(record what you saw)_

---

### STEP 3 — Re-read after edit (stale graph path)

**Action:** Attempt to read the full file again.
```
Read: tests/prompt_scenarios/fixtures/index.html
```

**What to observe:**
- The read is **allowed** (not denied) because the graph was marked stale by the edit.
- A stale nudge appears:
  `"File was edited — symbol map is being rebuilt. Use offset/limit with those line numbers to verify your change instead of reloading the full file."`
- The `additionalContext` now shows the updated symbol map including `footer#site-footer` with its new line number.

**PASS criteria:**
- [ ] Read is ALLOWED (permission = allow)
- [ ] Stale nudge message present: contains "File was edited"
- [ ] Updated symbol map in additionalContext shows `footer#site-footer` with a line number

**Observation:** _(record what you saw)_

---

### STEP 4 — Use targeted read instead of full re-read

**Action:** Using the line number from the symbol map in STEP 3, do a targeted section read of just the footer.
```
Read: tests/prompt_scenarios/fixtures/index.html  offset=<line from symbol map>  limit=5
```

**What to observe:**
- The targeted read is always allowed (offset/limit reads bypass the gate entirely).
- You see only the footer section — not the full file.

**PASS criteria:**
- [ ] Targeted read allowed
- [ ] Output contains `footer#site-footer` content
- [ ] Full file NOT loaded (only ~5 lines returned)

**Observation:** _(record what you saw)_

---

### STEP 5 — Add a contact section (second edit)

**Action:** Add a contact section before the footer:
```html
  <!-- CONTACT -->
  <section id="contact">
    <h2>Get in Touch</h2>
    <p>Email: alex@example.com</p>
  </section>
```

**What to observe:**
- Edit succeeds.
- Graph is marked stale again.

**PASS criteria:**
- [ ] Edit succeeds

**Observation:** _(record what you saw)_

---

### STEP 6 — Re-read after second edit (stale again)

**Action:** Read the full file again.
```
Read: tests/prompt_scenarios/fixtures/index.html
```

**What to observe:**
- Allowed again (stale resets the counter).
- Stale nudge appears again.
- Symbol map now includes `section#contact` and `footer#site-footer` with updated line numbers.

**PASS criteria:**
- [ ] Read ALLOWED
- [ ] Stale nudge present
- [ ] `section#contact` appears in symbol map

**Observation:** _(record what you saw)_

---

### STEP 7 — Re-read with NO edit (gate enforcement)

**Action:** Without making any edit, read the full file again.
```
Read: tests/prompt_scenarios/fixtures/index.html
```

**What to observe:**
- The read is **DENIED**.
- The denial message references the file graph:
  `"Already read this file — a local symbol graph exists in data/file-graphs/. Query it instead of reloading the full file."`

**PASS criteria:**
- [ ] Read is DENIED (permission = deny)
- [ ] Denial message contains "Already read this file"
- [ ] Message points to the file graph as the alternative

**Observation:** _(record what you saw)_

---

## Summary Checklist

After completing all steps, verify:

| Step | Expected | Result |
|------|----------|--------|
| 1 — First read | Allow, no nudge, symbol map injected | |
| 2 — Edit (add footer) | Edit allowed, graph marked stale | |
| 3 — Re-read after edit | Allow + stale nudge + updated symbol map | |
| 4 — Targeted offset/limit read | Always allowed, returns only requested lines | |
| 5 — Second edit (add contact) | Edit allowed, graph stale again | |
| 6 — Re-read after second edit | Allow + stale nudge + contact in symbol map | |
| 7 — Re-read with no edit | DENY + points to file graph | |

**All 7 steps must pass for this scenario to be considered GREEN.**

---

## What This Validates

- **Hook order:** `check_read_repeat` runs before `build_file_graph`, so the stale flag is seen before it is cleared.
- **Stale detection:** Every edit cycle resets the read counter exactly once — the agent gets one free re-read per edit, guided by a nudge.
- **Index-state gate:** Once the graph exists and is fresh, full re-reads are denied. The graph (with line numbers) is sufficient.
- **Targeted reads always pass:** `offset/limit` reads bypass the gate entirely — the escape hatch is always available.
- **No full re-read needed:** The agent navigates to any section via line numbers from the symbol map, never loading the full file a second time unnecessarily.

---

## How to Reset Between Runs

If you need to re-run this scenario from scratch, delete the cached graph for this file:
```bash
rm -f ~/.claude/plugins/local/cost-router/data/file-graphs/*.json
rm -f ~/.claude/plugins/local/cost-router/data/read_state.json
```

Then start a fresh Claude Code session so session state is clean.
