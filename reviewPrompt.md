Design Validation Review — [System/Feature Name]

Review the attached design [doc/diagram/spec] for [system/feature name], intended to solve [problem statement]. Current stage: [concept / pre-build / in-flight / post-incident]. Fixed constraints: [deadline, tech stack, compliance requirements, anything non-negotiable].

Simulate a review panel with four distinct lenses. Do not blend their feedback — each must produce independent findings.

1. Chief Architect — system boundaries, coupling, failure domains, scalability limits, alternatives not considered.
Required checklist: upstream/downstream contract validation (schema/payload agreement, versioning, breaking-change handling), failure propagation (what happens downstream if an upstream system is slow/down/malformed — graceful degradation vs. cascading failure), full dependency map of upstream inputs and downstream consumers (including shared DBs, event buses, third-party APIs), rollback/independent-deployability across the integration boundary.

2. Product Owner — does this solve the real problem, scope creep, what's explicitly out of scope, priority tradeoffs vs. speed.

3. Chief Data Architect — data model integrity, ownership, schema evolution, compliance/PII handling.
Required checklist: data freshness and ordering assumptions (sync vs. async, at-least-once vs. exactly-once), schema versioning consistency across all systems that touch this data.

4. Data Analyst — is success measurable, is instrumentation correct, what breaks in reporting/analytics downstream.

For every lens, also surface:

Edge cases: null/empty states, concurrency/race conditions, scale at 10x/100x current volume, partial failures, retries/idempotency, backward compatibility, multi-tenant/locale variance.
Hidden false signals: metrics that look healthy but mask a broken flow, silently swallowed errors, sampling or survivorship bias in any data used to justify the design, correlation mistaken for causation in supporting claims.

Output format:

Findings per persona, each tagged Critical / Major / Minor, with: what's wrong → why it matters → suggested fix.
Consolidated risk register (likelihood × impact).
Open decisions requiring input vs. hard blocking issues.
Explicit call-outs where personas would disagree with each other.
Final go/no-go recommendation, with conditions if "go with caveats."

Also output the findings as a JSON array with fields: persona, severity, issue, impact, fix.

Fill in the three bracketed fields at the top before sending it