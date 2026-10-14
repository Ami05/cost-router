#!/usr/bin/env bash
# WARNING: execute ONLY via resync/resync.sh. Never run directly and never pass flags (e.g. -q becomes PLUGIN_ROOT and corrupts every hook in settings.json).
# install.sh — idempotent merger of hooks/hooks.json into ~/.claude/settings.json
#
# Usage: bash install.sh [PLUGIN_ROOT]
#   PLUGIN_ROOT  path to the installed plugin (default: ~/.claude/plugins/local/cost-router)
#
# What it does:
#   1. Reads hooks/hooks.json from the source repo (relative to this script)
#   2. Expands ${CLAUDE_PLUGIN_ROOT} placeholders to PLUGIN_ROOT
#   3. Diffs against ~/.claude/settings.json per event+matcher block
#   4. Adds any missing hook entries idempotently
#   5. Detects stale cost-router hooks (command contains cost-router/hooks/run.sh
#      but no longer present in hooks.json across ALL events)
#   6. Prompts Y/N for stale entries not previously declined; persists N answers
#   7. Writes settings.json only if changed
#   8. Prints a clear summary of added/skipped/removed

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RESET_DECLINED=0
PLUGIN_ROOT=""
for arg in "$@"; do
  case "$arg" in
    --reset-declined) RESET_DECLINED=1 ;;
    *) [[ -z "$PLUGIN_ROOT" ]] && PLUGIN_ROOT="$arg" ;;
  esac
done
PLUGIN_ROOT="${PLUGIN_ROOT:-$HOME/.claude/plugins/local/cost-router}"
HOOKS_JSON="$SCRIPT_DIR/hooks/hooks.json"
SETTINGS_JSON="$HOME/.claude/settings.json"
STATE_FILE="$PLUGIN_ROOT/data/install.state.json"

if [[ ! -f "$HOOKS_JSON" ]]; then
  echo "ERROR: hooks/hooks.json not found at $HOOKS_JSON"
  exit 1
fi

if [[ "$RESET_DECLINED" -eq 1 && -f "$STATE_FILE" ]]; then
  rm "$STATE_FILE"
  echo "[install.sh] Cleared declined_deletions state — all stale entries will be re-prompted"
fi

echo "[install.sh] PLUGIN_ROOT = $PLUGIN_ROOT"
echo "[install.sh] Merging hooks into $SETTINGS_JSON"

python3 - "$PLUGIN_ROOT" "$HOOKS_JSON" "$SETTINGS_JSON" "$STATE_FILE" <<'PYEOF'
import json
import os
import sys

plugin_root = sys.argv[1]
hooks_json_path = sys.argv[2]
settings_path = sys.argv[3]
state_path = sys.argv[4]

# ── Load source hooks.json, expand ${CLAUDE_PLUGIN_ROOT} ─────────────────────
with open(hooks_json_path) as f:
    raw = f.read()
raw = raw.replace("${CLAUDE_PLUGIN_ROOT}", plugin_root)
source_hooks = json.loads(raw)["hooks"]  # dict: event -> list of blocks

# ── Load or initialise settings.json ─────────────────────────────────────────
if os.path.exists(settings_path):
    with open(settings_path) as f:
        settings = json.load(f)
else:
    settings = {}

if "hooks" not in settings:
    settings["hooks"] = {}

# ── Load or initialise install state (declined_deletions) ────────────────────
if os.path.exists(state_path):
    with open(state_path) as f:
        state = json.load(f)
else:
    state = {}
declined = set(state.get("declined_deletions", []))

# ── Build a set of all hook commands present in source hooks.json ─────────────
# Used for stale-detection: any command containing cost-router/hooks/run.sh
# that is NOT in this set is potentially stale.
source_commands = set()
for event, blocks in source_hooks.items():
    for block in blocks:
        for hook in block.get("hooks", []):
            cmd = hook.get("command", "")
            if cmd:
                source_commands.add(cmd)

# ── Helper: hook identity key ─────────────────────────────────────────────────
def hook_key(hook):
    """Unique key for a single hook entry within a block."""
    if hook.get("type") == "prompt":
        # For prompt-type hooks, match on type + first 80 chars of prompt text.
        # These have no command key — skip stale check entirely.
        return ("prompt", hook.get("prompt", "")[:80])
    return ("command", hook.get("command", ""))

# ── Step 1: add missing hooks from source ─────────────────────────────────────
added_count = 0
skipped_count = 0

for event, src_blocks in source_hooks.items():
    if event not in settings["hooks"]:
        settings["hooks"][event] = []

    for src_block in src_blocks:
        src_matcher = src_block.get("matcher", "")
        src_hook_entries = src_block.get("hooks", [])

        # Find matching block in settings by event+matcher
        dest_block = None
        for b in settings["hooks"][event]:
            if b.get("matcher", "") == src_matcher:
                dest_block = b
                break

        if dest_block is None:
            # Insert the entire block (preserve matcher only if non-empty)
            new_block = {}
            if src_matcher:
                new_block["matcher"] = src_matcher
            new_block["hooks"] = []
            settings["hooks"][event].append(new_block)
            dest_block = new_block

        if "hooks" not in dest_block:
            dest_block["hooks"] = []

        existing_keys = {hook_key(h) for h in dest_block["hooks"]}

        for hook in src_hook_entries:
            hk = hook_key(hook)
            if hk in existing_keys:
                skipped_count += 1
            else:
                dest_block["hooks"].append(hook)
                existing_keys.add(hk)
                added_count += 1
                if hook.get("type") == "prompt":
                    label = f"<prompt> [{event}]"
                else:
                    label = hook.get("command", "")
                print(f"  [+] Added  [{event}] {label}")

# ── Step 2: detect stale cost-router hooks ────────────────────────────────────
# A hook is stale if its command contains "cost-router/hooks/run.sh"
# AND it is not present in source_commands.
# prompt-type entries are never considered stale (no command key).
stale_commands = []  # list of (event, block_matcher, command_str)

for event, blocks in settings["hooks"].items():
    for block in blocks:
        for hook in block.get("hooks", []):
            cmd = hook.get("command", "")
            if cmd and "cost-router/hooks/run.sh" in cmd and cmd not in source_commands:
                stale_commands.append((event, block.get("matcher", ""), cmd))

# ── Step 3: prompt user for stale entries not in declined list ────────────────
newly_declined = []
to_remove = []

if stale_commands:
    print("\n[install.sh] Stale cost-router hooks detected (present in settings but absent from hooks.json):")
    for event, matcher, cmd in stale_commands:
        label = f"[{event}{'|' + matcher if matcher else ''}] {cmd}"
        if cmd in declined:
            print(f"  (skip — previously declined removal) {label}")
            continue
        print(f"  Stale: {label}")
        try:
            with open("/dev/tty") as tty:
                sys.stdout.write("  Remove? [Y/n]: ")
                sys.stdout.flush()
                answer = tty.readline().strip().lower()
        except (OSError, EOFError, KeyboardInterrupt):
            answer = "n"
        if answer in ("", "y", "yes"):
            to_remove.append((event, matcher, cmd))
            print(f"  [-] Marked for removal")
        else:
            newly_declined.append(cmd)
            print(f"  (kept — added to declined_deletions)")

# ── Step 4: apply removals ────────────────────────────────────────────────────
removed_count = 0
for event, matcher, cmd in to_remove:
    blocks = settings["hooks"].get(event, [])
    for block in blocks:
        if block.get("matcher", "") == matcher:
            before = len(block.get("hooks", []))
            block["hooks"] = [h for h in block.get("hooks", []) if h.get("command", "") != cmd]
            removed_count += before - len(block["hooks"])

# ── Step 5: persist updated state ─────────────────────────────────────────────
if newly_declined:
    declined.update(newly_declined)
    state["declined_deletions"] = sorted(declined)
    os.makedirs(os.path.dirname(state_path), exist_ok=True)
    with open(state_path, "w") as f:
        json.dump(state, f, indent=2)
    print(f"\n[install.sh] Saved {len(declined)} declined entry/entries to {state_path}")

# ── Step 6: write settings.json only if changed ───────────────────────────────
new_text = json.dumps(settings, indent=2) + "\n"
old_text = ""
if os.path.exists(settings_path):
    with open(settings_path) as f:
        old_text = f.read()

if new_text != old_text:
    with open(settings_path, "w") as f:
        f.write(new_text)
    print(f"\n[install.sh] settings.json written ({added_count} added, {skipped_count} skipped, {removed_count} removed)")
else:
    print(f"\n[install.sh] settings.json unchanged ({skipped_count} already present, {removed_count} removed)")

print("[install.sh] Done.")
PYEOF
