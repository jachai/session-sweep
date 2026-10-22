#!/bin/sh
# Install session-sweep as a plain Claude Code skill (no plugin system).
#   ./install.sh             copy the skill to ~/.claude/skills/close-stale-sessions
#   ./install.sh --link      symlink it instead (for working on this repo)
#   ./install.sh --uninstall remove the scheduled job and the skill (the archive in ~/.claude/session-sweep is kept)
# The scheduled job installs itself the first time the skill runs; `session-sweep schedule install` does it now.
set -eu
ROOT="$(cd "$(dirname "$0")" && pwd)"
CONFIG="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
DEST="$CONFIG/skills/close-stale-sessions"
SCRIPT="$ROOT/skills/close-stale-sessions/scripts/session_sweep.py"

case "${1:-}" in
  --uninstall)
    python3 "$SCRIPT" schedule uninstall || true
    rm -rf "$DEST"
    [ -L "$HOME/.local/bin/session-sweep" ] && rm -f "$HOME/.local/bin/session-sweep"
    echo "removed the skill and the scheduled job"
    ;;
  --link|"")
    mkdir -p "$CONFIG/skills"
    rm -rf "$DEST"
    if [ "${1:-}" = "--link" ]; then
      ln -s "$ROOT/skills/close-stale-sessions" "$DEST"
    else
      cp -R "$ROOT/skills/close-stale-sessions" "$DEST"
    fi
    echo "installed the skill at $DEST"
    echo "run it in Claude Code with /close-stale-sessions; its first run installs the scheduled job"
    ;;
  *)
    echo "usage: $0 [--link | --uninstall]" >&2
    exit 2
    ;;
esac
