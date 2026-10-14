# cost-router

[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![CI](https://github.com/Ami05/cost-router/actions/workflows/ci.yml/badge.svg)](https://github.com/Ami05/cost-router/actions/workflows/ci.yml)
[![Platforms](https://img.shields.io/badge/platforms-Cursor%20%7C%20Claude%20Code-blueviolet)](#supported-platforms)

**Route AI coding-agent work to the least expensive model likely to complete it, enforce routing decisions for tool use, and estimate usage costs.**

cost-router is a local plugin for Cursor and Claude Code. It combines routing guidance, tool-use gates, usage reporting, and evidence-based routing feedback in one codebase.

> **Cost figures are estimates, not billing records.** Actual accuracy depends on the platform, available usage data, model attribution, and pricing configuration.

## Features

- **Model-routing guidance:** classify work and choose an appropriate model/agent role.
- **Tool-use enforcement:** gate selected tool calls when a required routing verdict is missing.
- **Local usage reporting:** inspect estimated token and cost usage with `/cost-report`.
- **Routing feedback:** use `/tune-routing` to review patterns and improve routing decisions.
- **Read-efficiency safeguards:** detect repeated large-file reads and help direct agents toward targeted reads.
- **Shared implementation:** common scripts, skills, and commands support both platforms, with platform-specific hook configuration.

## Supported platforms

| Platform | Installation | Notes |
| --- | --- | --- |
| Claude Code | Self-hosted plugin marketplace or local install | Some usage and adaptive-routing signals depend on available transcript data and configuration. |
| Cursor | Local plugin installation | Cost is estimated from hook data; the adaptive auto-apply workflow is not at full parity with Claude Code. |

## Quick start

### Claude Code

Install from the repository's self-hosted marketplace:

```text
/plugin marketplace add Ami05/cost-router
/plugin install cost-router@cost-router
```

Or clone the repository and run the installer:

```bash
git clone https://github.com/Ami05/cost-router.git
cd cost-router
bash install.sh
```

For a session-only local development install, use:

```bash
claude --plugin-dir /path/to/cost-router
```

### Cursor

For local installation, copy the repository directory to:

```text
~/.cursor/plugins/local/cost-router/
```

Enable local/third-party plugin imports as required by your Cursor setup, then reload Cursor. Enterprise environments may require an administrator to enable local plugin imports.

After installation, start a new session and use the commands below to verify the setup.

## Usage

- `/cost-report` — view recent usage and estimated costs.
- `/tune-routing` — review routing patterns and suggested improvements.
- `python3 scripts/policy_manager.py --status` — inspect adaptive-routing status.
- `python3 scripts/policy_manager.py --list` — list queued routing proposals.
- `python3 scripts/policy_manager.py --undo TOKEN` — undo a recorded adaptive-routing change.

The exact available signals and behavior vary by platform. Review the limitations below before relying on automated routing changes.

## How cost estimation works

cost-router estimates token usage locally when authoritative usage data is unavailable. Its heuristic uses text length (approximately four characters per token), model-specific correction factors, and the editable pricing table in `scripts/pricing.json`.

- Treat estimates as directional comparisons, not invoices.
- Pricing data may need to be updated to reflect current provider prices.
- Prompt-caching discounts are not fully represented by the heuristic.
- Claude Code sessions may use real per-model transcript usage when it is available; otherwise, reports identify heuristic estimates.
- Model attribution may be incomplete when the platform does not include model information in hook payloads.

## Important limitations

- **Cursor:** hook payloads do not provide authoritative token counts or billing data, so reported costs are estimates. The full-session adaptive auto-apply pipeline is not wired into Cursor's stop hooks.
- **Claude Code:** model information is not consistently available in hook payloads. Transcript usage may improve attribution when a matching project transcript is available.
- **Routing gate:** a routing verdict must appear in visible assistant text to be detected reliably. A rare first-mutation false denial may occur if the host has not yet written the assistant message to its transcript; retrying can resolve it.
- **Adaptive routing:** automatic application is controlled by `policy.auto_apply_enabled` and is off by default. Review configuration and the security documentation before enabling it.

## Privacy

Usage and routing data are stored locally under the plugin's data directory. The logging design avoids storing raw prompt and response text in the usage log, but there are documented exceptions and additional data-handling details.

Read [SECURITY.md](SECURITY.md) for the exact data collected, local storage behavior, exceptions, sharing boundaries, and vulnerability-reporting instructions.

## Development and testing

The project uses Python scripts and a pytest test suite.

```bash
python3 -m pytest
```

Run tests from the repository root. Some shell-script checks may require additional tools; see [CONTRIBUTING.md](CONTRIBUTING.md) and the test documentation for development conventions.

Keep implementation details, hook event maps, configuration references, and troubleshooting procedures in dedicated documentation rather than expanding this overview.

## Documentation

- [Changelog](CHANGELOG.md) — release history.
- [Release notes](RELEASE-NOTES.md) — release-specific information.
- [Contributing](CONTRIBUTING.md) — development and contribution workflow.
- [Security and privacy](SECURITY.md) — data handling and vulnerability reporting.
- [Code of conduct](CODE_OF_CONDUCT.md) — community expectations.
- [Claude Code development notes](CLAUDE.md) — repository-specific agent and development guidance.
- [Routing gate reference](scripts/docs/routing-gates.md) — implemented gates and their behavior.

## Distribution

Claude Code supports installation through the self-hosted marketplace commands above. Cursor installation currently uses the local installation path described in this README; follow the platform's current marketplace requirements for any future public distribution.

## License

This project is licensed under the [MIT License](LICENSE).
