---
name: tune-routing
description: Analyze the cost-router usage log for routing misroutes, repeat-read patterns, and multi-step sessions with zero subagent dispatch, and propose model-router Decision Table updates.
---

# /tune-routing

Run the `tune-routing` skill: execute `python3 scripts/analyze_routing.py` from the plugin root, print the suggestions verbatim, then propose exact Decision Table row edits for `rules/model-router.md`. Only apply edits after the user explicitly confirms the wording.

**Source of truth:** Apply Decision Table edits to `rules/model-router.md` only.
