# Security & Privacy

## Reporting a vulnerability

Open a GitHub issue using the [bug report template](.github/ISSUE_TEMPLATE/bug_report.md), or email **sarthak05gupta@gmail.com** for anything sensitive enough that you'd rather not file publicly.

## What is logged

Every usage event stored in `data/usage.jsonl` contains only:

- byte length
- an (est.) token count
- a coarse model tier (`fast` / `reasoning` / `unknown`)
- the model name, if the hook payload includes one
- the tool name
- a one-way truncated SHA-256 hash of any file/command target (so repeats can be detected without storing the path itself)
- a timestamp

## What is never logged

Full prompt or response text is never written to disk, anywhere, by design. This matters because the plugin is meant to be installed by other people — logging raw content would leak their code or prompts into a local file even though it never leaves their machine.

## Named exceptions to "never logs raw content"

Two files intentionally deviate from the hash-only rule above. Both are documented here explicitly rather than left implicit, since a security reviewer should be able to find every exception in one place:

1. **`data/file_hash_registry.json` / `data/file_hash_history.json`** — `file_hash_guard.py`'s stale-file guard keys its entries by the **plaintext file path**, not a hash of it (the *values* are content hashes; the *keys* are not). This is needed for the guard to answer "have I seen this exact path before." It never stores file contents, only their hash, and it's gitignored the same as everything else — but if your threat model cares about plaintext paths specifically, this is the file to know about.
2. **`data/routing_state.json`'s `last_assistant_snippet`** — the routing gate reads the host's conversation transcript to find the last genuine user prompt/assistant reply for role-detection, but never persists that transcript content except for up to 500 characters of the assistant's own most recent reply, used only to detect a `Routing:` verdict or a dispatch-override phrase. It is capped, gitignored the same as `usage.jsonl`, and overwritten every turn rather than accumulated.



## Known limitations

- **A `Routing:` verdict placed only inside encrypted/redacted thinking is invisible to the gate.** The gate extracts assistant text from the transcript's `text`/`thinking` content blocks; a redacted-thinking block carries no readable string field, so a verdict that only ever appeared there can't be detected. The `model-router` skill already requires the verdict in visible assistant text for this reason.
- **A rare false-deny is possible on a turn's very first mutation**, if the host hasn't flushed the assistant's message to the transcript file before `PreToolUse` fires. This self-heals on retry.

## Local-only, resets on copy

Because the log lives inside the portable plugin folder rather than a fixed system location, it resets whenever you copy the folder fresh somewhere else. That's the intended trade-off for full portability — there is no cross-machine or cross-session persistence of usage data unless you explicitly wire up org insight sharing above.
