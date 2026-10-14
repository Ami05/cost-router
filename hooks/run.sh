#!/usr/bin/env bash
# Portable hook runner for cost-router.
# Cursor may invoke hooks with cwd = workspace root (not plugin root), so resolve
# the plugin directory explicitly instead of relying on relative script paths.
set -euo pipefail

resolve_plugin_root() {
  # Resolve plugin root from this script's path (not cwd or project env).
  local hook_dir
  hook_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  if [[ -d "${hook_dir}/../scripts" ]]; then
    (cd "${hook_dir}/.." && pwd)
    return 0
  fi
  if [[ -n "${CLAUDE_PLUGIN_ROOT:-}" && -d "${CLAUDE_PLUGIN_ROOT}/scripts" ]]; then
    printf '%s\n' "$CLAUDE_PLUGIN_ROOT"
    return 0
  fi
  if [[ -n "${CURSOR_PROJECT_DIR:-}" && -d "${CURSOR_PROJECT_DIR}/cost-router-v4/scripts" ]]; then
    printf '%s\n' "${CURSOR_PROJECT_DIR}/cost-router-v4"
    return 0
  fi
  if [[ -n "${CURSOR_PROJECT_DIR:-}" && -d "${CURSOR_PROJECT_DIR}/cost-router-v4/scripts" ]]; then
    printf '%s\n' "${CURSOR_PROJECT_DIR}/cost-router-v4"
    return 0
  fi
  if [[ -n "${CURSOR_PROJECT_DIR:-}" && -d "${CURSOR_PROJECT_DIR}/cost-router/scripts" ]]; then
    printf '%s\n' "${CURSOR_PROJECT_DIR}/cost-router"
    return 0
  fi
  cd "${hook_dir}/.." && pwd
}

PLUGIN_ROOT="$(resolve_plugin_root)"
cd "$PLUGIN_ROOT"

# If --plugin-dir loaded us (CLAUDE_PLUGIN_ROOT set by the plugin loader) AND
# hooks are already registered in ~/.claude/settings.json (sentinel present),
# skip to prevent double-firing the same event from both sources.
if [[ -n "${CLAUDE_PLUGIN_ROOT:-}" ]] && [[ -f "${PLUGIN_ROOT}/.hooks-in-settings" ]]; then
    case "$*" in
        *"--event prompt"*) echo '{"continue":true}' ;;
        *)                  echo '{}' ;;
    esac
    exit 0
fi

# PROD-8: missing script must fail open (empty JSON), not exit 2 and block the host.
if [[ ! -f "$PLUGIN_ROOT/scripts/$1" ]]; then
    case "$*" in
        *"--event prompt"*) echo '{"continue":true}' ;;
        *)                  echo '{}' ;;
    esac
    exit 0
fi

PYTHONPATH="$PLUGIN_ROOT${PYTHONPATH:+:$PYTHONPATH}" exec python3 "$PLUGIN_ROOT/scripts/$@"
