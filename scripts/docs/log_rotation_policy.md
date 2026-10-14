# Log Rotation Policy

## Current Status (as of 2026-09-19)

`logrotate` is **NOT configured** for `routing_events.jsonl`. The file grows unbounded in the production plugin directory:

```
~/.claude/plugins/local/cost-router/routing_events.jsonl
```

No rotation schedule, compression, or retention policy is in place.

## If Rotation Is Enabled Later

When logrotate is configured for `routing_events.jsonl`, the `read_events` function (and any analytics script that consumes this file) **must also read the rotated file** `.1` (i.e., `routing_events.jsonl.1`) and merge both files by timestamp before processing.

Failure to do this will cause recent-window queries (e.g., last-1-hour fallback rate) to silently drop events that were rotated out mid-window.

### Merge Pattern

```python
import json
from pathlib import Path

def read_events(base_path):
    paths = [Path(base_path), Path(str(base_path) + ".1")]
    events = []
    for p in paths:
        if p.exists():
            with open(p) as f:
                for line in f:
                    line = line.strip()
                    if line:
                        events.append(json.loads(line))
    return sorted(events, key=lambda e: e.get("timestamp", ""))
```

## Action Required Before Enabling Rotation

Before enabling logrotate on `routing_events.jsonl`:

1. Update `read_events` (and any caller) to merge `.1` by timestamp.
2. Verify the update is deployed to the production plugin dir.
3. Add a test covering the cross-file merge case.
4. Update this document with the rotation schedule and retention period.
