#!/usr/bin/env bash
# agent-swarm installer
# Usage: bash install.sh --adapter <claude-code|codex|generic> [--target <path>]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ADAPTER="claude-code"
TARGET="$(pwd)"
DRY_RUN=false

usage() {
  echo "Usage: $0 --adapter <claude-code|codex|generic> [--target <path>] [--dry-run]"
  echo ""
  echo "Adapters:"
  echo "  claude-code   Installs CLAUDE.md + .claude/agents/ for Claude Code"
  echo "  codex         Installs AGENTS.md + system-prompt.md for Codex CLI"
  echo "  generic       Installs system-prompt.md (copy-paste into any tool)"
  echo ""
  echo "Options:"
  echo "  --target      Target repo root (default: current directory)"
  echo "  --dry-run     Show what would be copied without writing files"
  exit 1
}

log()  { echo "  [agent-swarm] $*"; }
warn() { echo "  [agent-swarm] WARN: $*"; }

while [[ $# -gt 0 ]]; do
  case $1 in
    --adapter) ADAPTER="$2"; shift 2 ;;
    --target)  TARGET="$2";  shift 2 ;;
    --dry-run) DRY_RUN=true; shift   ;;
    -h|--help) usage ;;
    *) echo "Unknown option: $1"; usage ;;
  esac
done

ADAPTER_DIR="$SCRIPT_DIR/adapters/$ADAPTER"

if [[ ! -d "$ADAPTER_DIR" ]]; then
  echo "Error: adapter '$ADAPTER' not found in $SCRIPT_DIR/adapters/"
  echo "Available: $(ls "$SCRIPT_DIR/adapters/")"
  exit 1
fi

echo ""
echo "  agent-swarm installer"
echo "  ─────────────────────"
echo "  adapter : $ADAPTER"
echo "  source  : $ADAPTER_DIR"
echo "  target  : $TARGET"
echo "  dry-run : $DRY_RUN"
echo ""

copy_file() {
  local src="$1"
  local dst="$2"
  local rel_dst="${dst#$TARGET/}"

  if [[ "$DRY_RUN" == true ]]; then
    log "would copy → $rel_dst"
    return
  fi

  mkdir -p "$(dirname "$dst")"

  if [[ -f "$dst" ]]; then
    warn "file exists, backing up → ${rel_dst}.bak"
    cp "$dst" "${dst}.bak"
  fi

  cp "$src" "$dst"
  log "copied → $rel_dst"
}

# Walk adapter directory and mirror into target
while IFS= read -r -d '' src_file; do
  rel="${src_file#$ADAPTER_DIR/}"
  dst_file="$TARGET/$rel"
  copy_file "$src_file" "$dst_file"
done < <(find "$ADAPTER_DIR" -type f -print0)

# Also copy workflow playbooks into .agent-swarm/workflows/
WORKFLOWS_DST="$TARGET/.agent-swarm/workflows"
while IFS= read -r -d '' src_file; do
  rel="${src_file#$SCRIPT_DIR/workflows/}"
  dst_file="$WORKFLOWS_DST/$rel"
  copy_file "$src_file" "$dst_file"
done < <(find "$SCRIPT_DIR/workflows" -type f -print0)

echo ""
if [[ "$DRY_RUN" == true ]]; then
  log "Dry run complete. No files were written."
else
  log "Install complete."
  echo ""
  case "$ADAPTER" in
    claude-code)
      echo "  Next steps:"
      echo "  1. Open the project in Claude Code"
      echo "  2. CLAUDE.md is now active as root context"
      echo "  3. Sub-agents in .claude/agents/ are available automatically"
      echo "  4. See workflows/ for orchestration playbooks"
      ;;
    codex)
      echo "  Next steps:"
      echo "  1. AGENTS.md is now active — Codex reads it automatically"
      echo "  2. See system-prompt.md if you need to paste into a custom config"
      ;;
    generic)
      echo "  Next steps:"
      echo "  1. Open system-prompt.md"
      echo "  2. Paste its contents into your tool's system prompt field"
      ;;
  esac
fi
echo ""
