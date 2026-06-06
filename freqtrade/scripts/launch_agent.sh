set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PATH="$HOME/.npm-global/bin:$HOME/.local/bin:$PATH"

MODE="tabs"   # default: gnome-terminal tabs
for arg in "$@"; do
  case "$arg" in
    --tmux)   MODE="tmux" ;;
    --attach) MODE="attach" ;;
  esac
done

# ── prerequisite check ────────────────────────────────────────────────────────
if ! command -v claude &>/dev/null; then
  echo "ERROR: claude not found. Run: bash scripts/setup.sh"; exit 1
fi
echo "claude : $(which claude)  [$(claude --version 2>/dev/null | head -1)]"


# ── per-agent shell snippet ───────────────────────────────────────────────────
# Sets PATH, moves to the right dir, launches claude, then drops to bash if it exits.
agent_cmd() {
  local dir="$1"
  echo "export PATH=$HOME/.npm-global/bin:\$PATH; cd '$dir'; claude; exec bash"
}

STRATEGY_CMD="$(agent_cmd "$REPO_ROOT")"

# ════════════════════════════════════════════════════════════════════════════
# MODE: gnome-terminal tabs (default)
# ════════════════════════════════════════════════════════════════════════════
if [[ "$MODE" == "tabs" ]]; then
  if ! command -v gnome-terminal &>/dev/null; then
    echo "gnome-terminal not found — falling back to tmux"
    MODE="tmux"
  else
    echo ""
    echo "Opening 4 separate terminal windows (one per agent)..."
    echo ""
    echo "  Window 1 — strategy  (Strategy Agent)"
    echo ""
    gnome-terminal --title="strategy — Strategy Agent" -- bash -c "$STRATEGY_CMD" &
    sleep 0.3
    echo "Done. Switch agents by clicking each window in the taskbar."
    exit 0
  fi
fi

# ════════════════════════════════════════════════════════════════════════════
# MODE: tmux (fallback or --tmux flag)
# ════════════════════════════════════════════════════════════════════════════
SESSION="agents"

if [[ "$MODE" == "attach" ]]; then
  tmux attach-session -t "$SESSION" 2>/dev/null \
    || { echo "No session '$SESSION'. Run without --attach to start one."; exit 1; }
  exit 0
fi

if ! command -v tmux &>/dev/null; then
  echo "ERROR: tmux not found. Run: bash scripts/setup.sh"; exit 1
fi

tmux kill-session -t "$SESSION" 2>/dev/null || true

# Enable mouse so you can click to switch windows — no key combos needed
tmux \
  new-session  -d -s "$SESSION" -n "strategy" -c "$REPO_ROOT" "bash -c '$STRATEGY_CMD'" \;\
  select-window    -t "$SESSION:strategy"

echo ""
echo "╔══════════════════════════════════════════════════════╗"
echo "║   agents session — click tabs at bottom to switch   ║"
echo "╠═════════════════════════════════════════════════════╣"
echo "║  strategy   ← click any tab                ║"
echo "╠═══╗"
echo "║  Ctrl+B, d  →  detach (agents keep running)         ║"
echo "╚═══╝"
echo ""

tmux attach-session -t "$SESSION"
