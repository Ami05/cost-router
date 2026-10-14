# Controller Routing Rules

## Status-Based Routing

When a worker returns, the controller routes mechanically based on the first-line status:

### DONE
- Mark task complete
- Proceed to next task in dependency order
- No further action required

### DONE_WITH_CONCERNS
- Read the concern summary (do NOT re-read full worker output)
- If concern is cosmetic/minor → accept, proceed
- If concern affects correctness → dispatch fix task to same or higher tier
- Log: "Ruling: accepted/rejected concern — <why> — <cost if wrong>"

### BLOCKED
- Read the BLOCKED reason category
- Route per decision tree (see BLOCKED Escalation below)
- Do NOT resolve by doing work inline (no silent absorption)

### NEEDS_CONTEXT
- Extract the specific file/interface requested
- Re-dispatch with the missing context added to brief
- Max 2 re-dispatches for NEEDS_CONTEXT before escalating

## BLOCKED Escalation Decision Tree

| Category | Action |
|---|---|
| AMBIGUITY | Issue ruling with justification, or escalate to human |
| DEPENDENCY | Reorder tasks — complete dependency first |
| COMPLEXITY | Tier up — re-dispatch to higher model tier |
| MISSING_CONTEXT | Re-dispatch with requested files/interfaces |

### Escalation Limits
- Max 3 re-dispatches per task before mandatory human escalation
- Controller must NOT resolve BLOCKED by doing work inline
- Log every escalation: "Routing: ESCALATE — worker BLOCKED on [reason] — action: [choice]"

## Escalation Logging

Every escalation must be logged in the session output:

```
Routing: ESCALATE — worker BLOCKED on <reason> — action: <choice>
```

### Re-dispatch Tracking

| Re-dispatch # | Action |
|---|---|
| 1 | Add context or clarify |
| 2 | Tier up model or split task |
| 3 | Mandatory human escalation |

### Silent Absorption Prevention

The controller MUST NOT:
- Resolve BLOCKED by writing code inline
- Absorb judgment calls silently
- Skip logging when overriding a BLOCKED status

If the controller catches itself about to do work that a BLOCKED worker couldn't,
it must either: (a) re-dispatch to a higher tier, or (b) escalate to human.
