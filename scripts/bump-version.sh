#!/usr/bin/env bash
# Usage: bash scripts/bump-version.sh <new-version>
# Updates VERSION file and both plugin manifests in one step.
set -euo pipefail
VERSION="${1:?Usage: bash scripts/bump-version.sh <new-version>}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$SCRIPT_DIR")"
echo "$VERSION" > "$ROOT/VERSION"
python3 - "$ROOT" "$VERSION" <<'PYEOF'
import json, sys
root, version = sys.argv[1], sys.argv[2]
for manifest in [f"{root}/.cursor-plugin/plugin.json", f"{root}/.claude-plugin/plugin.json"]:
    with open(manifest) as f:
        data = json.load(f)
    data["version"] = version
    with open(manifest, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    print(f"Updated {manifest} -> {version}")

marketplace = f"{root}/.claude-plugin/marketplace.json"
try:
    with open(marketplace) as f:
        data = json.load(f)
    for plugin in data.get("plugins", []):
        plugin["version"] = version
    with open(marketplace, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    print(f"Updated {marketplace} -> {version}")
except FileNotFoundError:
    pass
PYEOF
echo "Version bumped to $VERSION"
echo "Don't forget to add an entry to RELEASE-NOTES.md before tagging."
