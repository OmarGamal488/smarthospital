#!/usr/bin/env bash
#
# SmartHospital — stop every service started by run.sh.
#
# Kills any local `manage.py runserver` and `manage.py runbot` processes
# that belong to this checkout (matched by the project path so we don't
# kill an unrelated Django on the same machine).
#
# Usage:
#   ./stop.sh           # graceful (SIGTERM), then SIGKILL after 2s
#   ./stop.sh --force   # straight to SIGKILL
#   ./stop.sh --dry-run # just show what would be killed

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

MODE="graceful"
case "${1:-}" in
  --force)    MODE="force" ;;
  --dry-run)  MODE="dry"   ;;
  -h|--help)  sed -n '2,12p' "$0"; exit 0 ;;
  '') ;;
  *) echo "Unknown flag: $1 (use --help)" >&2; exit 2 ;;
esac

# Find every process whose argv mentions one of our manage.py entrypoints
# AND whose cwd is the project directory. This avoids killing someone
# else's Django on the same machine.
mapfile -t PIDS < <(
  pgrep -af 'manage\.py (runserver|runbot)' 2>/dev/null \
    | while read -r pid rest; do
        cwd=$(readlink -f "/proc/$pid/cwd" 2>/dev/null || true)
        if [[ "$cwd" == "$PROJECT_DIR"* ]]; then
          echo "$pid $rest"
        fi
      done
)

if [[ ${#PIDS[@]} -eq 0 ]]; then
  echo "Nothing to stop — no SmartHospital processes are running for $PROJECT_DIR."
  exit 0
fi

echo "Found ${#PIDS[@]} SmartHospital process(es):"
for line in "${PIDS[@]}"; do
  echo "  · $line"
done

if [[ "$MODE" == "dry" ]]; then
  echo "(--dry-run — not killing anything)"
  exit 0
fi

# Extract just the PID column
JUST_PIDS=()
for line in "${PIDS[@]}"; do
  JUST_PIDS+=("${line%% *}")
done

if [[ "$MODE" == "force" ]]; then
  kill -9 "${JUST_PIDS[@]}" 2>/dev/null || true
  echo "✓ Force-killed."
  exit 0
fi

# Graceful: SIGTERM, wait briefly, then SIGKILL stragglers.
kill "${JUST_PIDS[@]}" 2>/dev/null || true
sleep 2
SURVIVORS=()
for pid in "${JUST_PIDS[@]}"; do
  if kill -0 "$pid" 2>/dev/null; then
    SURVIVORS+=("$pid")
  fi
done
if [[ ${#SURVIVORS[@]} -gt 0 ]]; then
  echo "Some processes did not exit cleanly — sending SIGKILL to: ${SURVIVORS[*]}"
  kill -9 "${SURVIVORS[@]}" 2>/dev/null || true
fi
echo "✓ Stopped."
