---
name: feedback_gate_testing_patterns
description: Non-obvious gotchas when building and testing PreToolUse gates in this project
metadata:
  type: feedback
---

When building a new PreToolUse gate with a state file:

**Rule 1: Never bind DATA_DIR at module level.**
`STATE_FILE = DATA_DIR / "mygate_state.json"` is wrong — it copies the value at import time and test monkeypatching of `DATA_DIR` has no effect. Instead, reference `_common.DATA_DIR` dynamically at call time inside functions:
```python
import _common
# inside the function:
state_file = _common.DATA_DIR / "mygate_state.json"
```

**Why:** Python `from _common import DATA_DIR` binds a value copy. `import _common; _common.DATA_DIR` always resolves the current module-level value, which gets updated by `importlib.reload(_common)`.

**Rule 2: Test fixtures must use COST_ROUTER_DATA_DIR env var, not attribute monkeypatching.**
Setting `_common.DATA_DIR = test_dir` directly in test setUp does NOT work. `_common.DATA_DIR` is set from the env var at module import time. Correct pattern:
```python
os.environ["COST_ROUTER_DATA_DIR"] = str(self.test_data_dir)
importlib.reload(_common)
importlib.reload(my_gate_module)
```
And restore in teardown.

**Rule 3: Empty/missing turn_id must reset the counter.**
Test payloads without `turn_id` all resolve to `""`. If the reset condition is `turn_id != state.get("last_turn_id")`, it never fires for empty strings and count accumulates across unrelated tests. Use:
```python
if not turn_id or turn_id != state.get("last_turn_id", None):
    # reset
```

**How to apply:** Follow all three rules for every new gate that tracks per-turn state. Confirmed in sequential_explore_gate.py implementation (2026-09-14).
