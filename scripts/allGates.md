All Gates — Tabular Format

  Production Gate Chains (enforced at runtime)

  ┌────────────────┬──────────────┬───────────────────────────────────────────┬─────────────────────┬───────────────────────────┐
  │    Gate ID     │    Chain     │                  Trigger                  │       Action        │           File            │
  ├────────────────┼──────────────┼───────────────────────────────────────────┼─────────────────────┼───────────────────────────┤
  │ GATE-1         │ pre_write    │ Simple task markers (≤3 calls,            │ ALLOW (exempt from  │ check_routing.py          │
  │                │              │ "append"/"rename"/"tweak")                │ routing)            │                           │
  ├────────────────┼──────────────┼───────────────────────────────────────────┼─────────────────────┼───────────────────────────┤
  │ GATE-2         │ pre_write    │ Agentic work without Routing: line        │ NUDGE / DENY        │ check_routing.py          │
  ├────────────────┼──────────────┼───────────────────────────────────────────┼─────────────────────┼───────────────────────────┤
  │ GATE-2b        │ pre_write    │ >FAST_PATH_CALL_LIMIT mutating calls      │ DENY                │ check_routing.py          │
  │                │              │ without routing                           │                     │                           │
  ├────────────────┼──────────────┼───────────────────────────────────────────┼─────────────────────┼───────────────────────────┤
  │                │              │ Bulk verification command (pytest full    │ ASK user            │                           │
  │ GATE-3         │ pre_write    │ suite, npm test) inline                   │ (user-local vs      │ check_routing.py          │
  │                │              │                                           │ subagent)           │                           │
  ├────────────────┼──────────────┼───────────────────────────────────────────┼─────────────────────┼───────────────────────────┤
  │ GATE-4         │ pre_write    │ ≥DISPATCH_DENY_THRESHOLD mechanical calls │ DENY / NUDGE        │ check_routing.py          │
  │                │              │  on reasoning tier                        │                     │                           │
  ├────────────────┼──────────────┼───────────────────────────────────────────┼─────────────────────┼───────────────────────────┤
  │ GATE-5         │ pre_write    │ DIRECT + Write >150 lines / 5KB           │ NUDGE               │ check_routing.py          │
  ├────────────────┼──────────────┼───────────────────────────────────────────┼─────────────────────┼───────────────────────────┤
  │ GATE-5b        │ pre_write    │ Deploy/resync script detection            │ BULK → GATE-3       │ check_routing.py          │
  ├────────────────┼──────────────┼───────────────────────────────────────────┼─────────────────────┼───────────────────────────┤
  │ GATE-6 /       │ pre_write,   │ FO/RB role + direct                       │ DENY                │ enforce_role_gates.py     │
  │ HG-102         │ pre_read     │ Edit/Write/Shell/large-Read               │                     │                           │
  ├────────────────┼──────────────┼───────────────────────────────────────────┼─────────────────────┼───────────────────────────┤
  │ GATE-7         │ pre_write    │ Mutation target not in declared scope     │ DENY                │ check_routing.py          │
  ├────────────────┼──────────────┼───────────────────────────────────────────┼─────────────────────┼───────────────────────────┤
  │ GATE-8         │ pre_write    │ ≥INLINE_MUTATION_NUDGE_THRESHOLD          │ NUDGE / DENY        │ check_routing.py          │
  │                │              │ mutations without dispatch                │                     │                           │
  ├────────────────┼──────────────┼───────────────────────────────────────────┼─────────────────────┼───────────────────────────┤
  │ GATE-9         │ pre_write    │ Multi-step work + 0 Task dispatches in    │ DENY                │ check_routing.py          │
  │                │              │ session                                   │                     │                           │
  ├────────────────┼──────────────┼───────────────────────────────────────────┼─────────────────────┼───────────────────────────┤
  │ GATE-10        │ pre_write    │ High inline mutations + no Task dispatch  │ DENY                │ check_routing.py          │
  ├────────────────┼──────────────┼───────────────────────────────────────────┼─────────────────────┼───────────────────────────┤
  │ GATE-11        │ pre_write    │ ≥FLIP_FLOP_THRESHOLD tier switches in     │ NUDGE               │ check_routing.py          │
  │                │              │ session                                   │                     │                           │
  ├────────────────┼──────────────┼───────────────────────────────────────────┼─────────────────────┼───────────────────────────┤
  │ GATE-12        │ pre_write    │ Routing: DIRECT without reason after      │ DENY                │ check_routing.py          │
  │                │              │ em-dash                                   │                     │                           │
  ├────────────────┼──────────────┼───────────────────────────────────────────┼─────────────────────┼───────────────────────────┤
  │ GATE-13        │ pre_write    │ Routing: DIRECT with 5+ file targets      │ DENY                │ check_routing.py          │
  ├────────────────┼──────────────┼───────────────────────────────────────────┼─────────────────────┼───────────────────────────┤
  │ GATE-RD-2      │ pre_write    │ Write has >80% structural overlap with    │ NUDGE               │ check_routing.py          │
  │                │              │ recent write                              │                     │                           │
  ├────────────────┼──────────────┼───────────────────────────────────────────┼─────────────────────┼───────────────────────────┤
  │ GATE-RD-3      │ pre_write    │ Work classified as mechanical             │ DENY (dispatch to   │ check_routing.py          │
  │                │              │ (spec+pattern+multi-file)                 │ haiku)              │                           │
  ├────────────────┼──────────────┼───────────────────────────────────────────┼─────────────────────┼───────────────────────────┤
  │ GATE-RD-5      │ pre_write    │ 3+ consecutive edit-test-fail on same     │ NUDGE at 3, DENY at │ check_routing.py          │
  │                │              │ files                                     │  5                  │                           │
  ├────────────────┼──────────────┼───────────────────────────────────────────┼─────────────────────┼───────────────────────────┤
  │ GATE-RD-6      │ pre_write    │ FW routing with >limit files              │ NUDGE               │ check_routing.py          │
  ├────────────────┼──────────────┼───────────────────────────────────────────┼─────────────────────┼───────────────────────────┤
  │ Read-1/2       │ pre_read     │ Inline reads exceed per-turn threshold    │ NUDGE / DENY        │ read_gate.py              │
  │                │              │ (5/7 DIRECT, 3/4 other)                   │                     │                           │
  ├────────────────┼──────────────┼───────────────────────────────────────────┼─────────────────────┼───────────────────────────┤
  │ SEM-202        │ pre_read,    │ Semantic role/text misalignment           │ ALLOW + log         │ enforce_role_gates.py     │
  │                │ pre_write    │                                           │ (advisory)          │                           │
  ├────────────────┼──────────────┼───────────────────────────────────────────┼─────────────────────┼───────────────────────────┤
  │ DR-2           │ pre_task     │ Task dispatch with >20-line inline code   │ NUDGE / DENY        │ dispatch_context_gate.py  │
  │                │              │ fence                                     │                     │                           │
  ├────────────────┼──────────────┼───────────────────────────────────────────┼─────────────────────┼───────────────────────────┤
  │ DR-3           │ pre_task     │ Handoff profile violations (zero-handoff  │ DENY                │ dispatch_context_gate.py  │
  │                │              │ >50 chars)                                │                     │                           │
  ├────────────────┼──────────────┼───────────────────────────────────────────┼─────────────────────┼───────────────────────────┤
  │ SPO-007        │ pre_task     │ Dispatch prompt fails JSON schema or       │ DENY                │ validate_dispatch_schema. │
  │                │              │ constraint allowlist check                │                     │ py                        │
  ├────────────────┼──────────────┼───────────────────────────────────────────┼─────────────────────┼───────────────────────────┤
  │ DISPATCH-1     │ pre_task     │ Mechanical work dispatched to             │ DENY                │ check_routing.py          │
  │                │              │ reasoning-tier model                      │                     │                           │
  ├────────────────┼──────────────┼───────────────────────────────────────────┼─────────────────────┼───────────────────────────┤
  │ DISPATCH-2     │ pre_task     │ Judgment work dispatched to fast-tier     │ NUDGE               │ check_routing.py          │
  │                │              │ model                                     │                     │                           │
  ├────────────────┼──────────────┼───────────────────────────────────────────┼─────────────────────┼───────────────────────────┤
  │ DISPATCH-3     │ pre_task     │ Dispatch context exceeds token budget     │ NUDGE               │ check_routing.py          │
  ├────────────────┼──────────────┼───────────────────────────────────────────┼─────────────────────┼───────────────────────────┤
  │                │              │ Full read of >300-line file when symbol   │ ALLOW + NUDGE       │                           │
  │ File-Graph     │ pre_read     │ graph exists                              │ (suggest targeted   │ file_graph_gate.py        │
  │                │              │                                           │ read)               │                           │
  ├────────────────┼──────────────┼───────────────────────────────────────────┼─────────────────────┼───────────────────────────┤
  │ File-Hash      │ pre_write,   │ File hash corruption guard                │ Track / Deny        │ file_hash_guard.py        │
  │                │ pre_read     │                                           │                     │                           │
  ├────────────────┼──────────────┼───────────────────────────────────────────┼─────────────────────┼───────────────────────────┤
  │ Explicit-Model │ pre_task     │ Agent/Task without explicit model         │ Enforce             │ enforce_explicit_model.py │
  └────────────────┴──────────────┴───────────────────────────────────────────┴─────────────────────┴───────────────────────────┘

