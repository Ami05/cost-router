# Task Brief Template

Use this template when dispatching workers. Target: <2000 tokens per brief.

## Brief Structure

```
## Task
<task description from ticket>

## Acceptance Criteria
- <criterion 1>
- <criterion 2>

## Files
- <file path 1> — <why needed>
- <file path 2> — <why needed>

## Interfaces
<relevant function signatures, type definitions, constants>

## Constraints
- <project-level constraint 1>
- <project-level constraint 2>

## Report Contract
Return STATUS: DONE|DONE_WITH_CONCERNS|BLOCKED|NEEDS_CONTEXT as first line.
```

## What to INCLUDE
- Task description and acceptance criteria (from ticket)
- File paths the worker needs to read or modify
- Interface signatures for dependencies (function signatures, types)
- Global constraints (style rules, test requirements)

## What to EXCLUDE
- Other tasks from the plan
- Session history or conversation context
- Full plan narrative or design rationale
- Controller reasoning or previous worker outputs
- Inline file contents (reference paths instead)
