## Hard Constraints — Config

- **`resume_ttl_seconds` max is 300s (5 min) — do not raise this value.** Anthropic subagent prompt cache TTL is 5 min. Resuming after cache expiry pays full cold-write price (1.25× base input on the entire prefix) — more expensive than a fresh spawn. This is a cost protection rule. Any PR raising this value above 300 must be rejected.
