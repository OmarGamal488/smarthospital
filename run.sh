#!/usr/bin/env bash
#
# SmartHospital — one-shot launcher.
#
# Starts (in order):
#   1. uv sync                       — make sure deps are installed
#   2. manage.py migrate             — apply any pending migrations
#   3. manage.py runserver           — Django + Channels (HTTP + WebSocket)
#   4. manage.py runbot              — Telegram patient bot (only if TELEGRAM_BOT_TOKEN is set)
#
# Both long-running processes are spawned in the background. Ctrl-C (or
# closing the terminal) kills the whole group thanks to the trap below.
#
# Usage:
#   ./run.sh                # default: web on :8000, bot if token present
#   ./run.sh --no-bot       # skip the bot even if a token is set
#   ./run.sh --port 9000    # serve web on a different port
#   ./run.sh --reset-seed   # wipe + re-seed demo data before starting
#   ./run.sh --reminders    # also run a one-off `send_reminders --dry-run` pass

set -euo pipefail

# ── Resolve the project directory regardless of cwd ─────────────────────────
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"

# ── Parse flags ─────────────────────────────────────────────────────────────
PORT=8000
RUN_BOT=1
RESEED=0
REMIND=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-bot)      RUN_BOT=0 ;;
    --port)        PORT="${2:?--port needs a value}"; shift ;;
    --port=*)      PORT="${1#--port=}" ;;
    --reset-seed)  RESEED=1 ;;
    --reminders)   REMIND=1 ;;
    -h|--help)
      sed -n '2,21p' "$0"; exit 0 ;;
    *) echo "Unknown flag: $1 (use --help)" >&2; exit 2 ;;
  esac
  shift
done

# ── Colour helpers ──────────────────────────────────────────────────────────
GREEN='\033[1;32m'; YELLOW='\033[1;33m'; RED='\033[1;31m'; CYAN='\033[1;36m'; NC='\033[0m'
say() { printf "${CYAN}▸${NC} %s\n" "$*"; }
ok()  { printf "${GREEN}✓${NC} %s\n" "$*"; }
warn(){ printf "${YELLOW}⚠${NC} %s\n" "$*"; }
die() { printf "${RED}✗${NC} %s\n" "$*"; exit 1; }

# ── Pre-flight checks ───────────────────────────────────────────────────────
command -v uv >/dev/null 2>&1 || die "uv not found — install from https://docs.astral.sh/uv/"

if [[ ! -f .env ]]; then
  warn ".env missing — chatbot LLM and Telegram bot will be disabled until you add tokens"
fi

# ── Sync deps + migrate ─────────────────────────────────────────────────────
say "uv sync"
uv sync --quiet

say "Applying migrations"
uv run manage.py migrate --noinput

if [[ "$RESEED" == "1" ]]; then
  say "Re-seeding demo data (wipes existing!)"
  uv run manage.py seed_demo --reset --patients 80 --doctors 24 --appointments 380
fi

if [[ "$REMIND" == "1" ]]; then
  say "Dispatching reminder pass (dry-run)"
  uv run manage.py send_reminders --dry-run || true
fi

# ── Trap so Ctrl-C kills all children, even when launched via `nohup` etc. ─
PIDS=()
cleanup() {
  echo
  warn "Shutting down…"
  for pid in "${PIDS[@]:-}"; do
    if kill -0 "$pid" 2>/dev/null; then
      kill "$pid" 2>/dev/null || true
    fi
  done
  # Give them a moment; force-kill any survivors
  sleep 1
  for pid in "${PIDS[@]:-}"; do
    if kill -0 "$pid" 2>/dev/null; then
      kill -9 "$pid" 2>/dev/null || true
    fi
  done
  ok "Stopped."
}
trap cleanup INT TERM EXIT

# ── Launch web server ───────────────────────────────────────────────────────
say "Starting Django dev server on http://127.0.0.1:${PORT}/"
uv run manage.py runserver "${PORT}" 2>&1 | sed -u 's/^/[web] /' &
PIDS+=($!)

# ── Launch Telegram bot if token is set ────────────────────────────────────
HAS_TOKEN=0
if [[ -f .env ]] && grep -qE '^TELEGRAM_BOT_TOKEN=.+' .env; then
  HAS_TOKEN=1
fi
if [[ -n "${TELEGRAM_BOT_TOKEN:-}" ]]; then
  HAS_TOKEN=1
fi

if [[ "$RUN_BOT" == "1" && "$HAS_TOKEN" == "1" ]]; then
  say "Starting Telegram bot"
  uv run manage.py runbot 2>&1 | sed -u 's/^/[bot] /' &
  PIDS+=($!)
elif [[ "$RUN_BOT" == "1" ]]; then
  warn "TELEGRAM_BOT_TOKEN is not set in .env — skipping bot (use --no-bot to silence this)"
fi

ok "All services up. Press Ctrl-C to stop."
printf "\n  Web        → ${GREEN}http://127.0.0.1:%s/${NC}\n" "$PORT"
printf "  Login      → ${GREEN}http://127.0.0.1:%s/login/${NC}\n" "$PORT"
printf "  Admin      → ${GREEN}http://127.0.0.1:%s/admin/${NC}\n" "$PORT"
[[ "$HAS_TOKEN" == "1" && "$RUN_BOT" == "1" ]] && \
  printf "  Telegram   → ${GREEN}long-polling — open your bot in Telegram${NC}\n"
echo

# ── Wait on the first child to exit, then cleanup takes over ───────────────
wait -n
