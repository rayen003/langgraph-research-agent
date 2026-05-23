#!/usr/bin/env bash
#  start.sh  —  launch backend + frontend
#  Backend:   http://localhost:8080
#  Frontend:  http://localhost:5174
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FRONTEND_DIR="$ROOT/agent_project/frontend"
UV="$HOME/.local/bin/uv"

CYAN='\033[0;36m'
MAGENTA='\033[0;35m'
BOLD='\033[1m'
DIM='\033[2m'
YELLOW='\033[0;33m'
RESET='\033[0m'

# ── Kill any existing processes on our ports ──────────────────────────
echo -e "${YELLOW}Cleaning up existing processes...${RESET}"
for port in 8080 5174 5175 5176 5177 5178; do
  pids=$(lsof -ti :$port 2>/dev/null)
  if [ -n "$pids" ]; then
    echo -e "  ${DIM}Killing PID(s) $pids on port $port${RESET}"
    kill -9 $pids 2>/dev/null
  fi
done
sleep 1

cleanup() {
  echo ""
  echo -e "${DIM}Shutting down…${RESET}"
  kill 0 2>/dev/null
  wait 2>/dev/null
  exit 0
}
trap cleanup SIGINT SIGTERM

if [ ! -f "$UV" ]; then
  echo "uv not found at $UV"
  echo "Install: curl -Ls https://astral.sh/uv/install.sh | sh"
  exit 1
fi

if [ ! -d "$FRONTEND_DIR/node_modules" ]; then
  echo -e "${DIM}Installing frontend deps…${RESET}"
  cd "$FRONTEND_DIR" && npm install
fi

echo -e "${DIM}Syncing Python deps…${RESET}"
cd "$ROOT" && "$UV" sync --quiet

echo -e "${CYAN}${BOLD}▶ Backend${RESET}  ${DIM}http://localhost:8080${RESET}"
PYTHONUNBUFFERED=1 "$UV" run uvicorn server:app \
  --app-dir "$ROOT/agent_project" \
  --host 0.0.0.0 \
  --port 8080 &

echo -e "${MAGENTA}${BOLD}▶ Frontend${RESET} ${DIM}http://localhost:5174${RESET}"
cd "$FRONTEND_DIR"
npm run dev &

echo ""
echo -e "  ${BOLD}Research Agent running${RESET}"
echo -e "  ${CYAN}Backend${RESET}   → http://localhost:8080"
echo -e "  ${MAGENTA}Frontend${RESET}  → http://localhost:5174"
echo -e "  ${DIM}Ctrl+C to stop both${RESET}"
echo ""

wait
