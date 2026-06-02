#!/usr/bin/env bash
#  start.sh — start the research agent (backend API + web UI)
#
#  What it does:
#    1. Picks free ports (default backend 8080, frontend 5174)
#    2. Stops leftover agent processes from a previous run (unless --no-kill)
#    3. Starts FastAPI backend + React frontend
#
#  Common commands:
#    ./start.sh                    Start everything (recommended)
#    ./start.sh --dev              Frontend with hot reload (for UI work)
#    ./start.sh --port 8001        Backend on port 8001 (frontend still 5174)
#    ./start.sh --no-kill          Never kill old processes; use next free port
#    ./start.sh --backend          API only (no browser UI)
#    ./start.sh --help             Full option list
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AGENT_DIR="$ROOT/agent_project"
FRONTEND_DIR="$AGENT_DIR/frontend"
RUNS_DIR="$AGENT_DIR/runs"
DB_PATH="$RUNS_DIR/agent.db"
UV="${UV:-$HOME/.local/bin/uv}"
BACKEND_PORT=8080
FRONTEND_PORT=5174
BACKEND_PORT_MIN=8080
BACKEND_PORT_MAX=8099
FRONTEND_PORT_MIN=5174
FRONTEND_PORT_MAX=5199

CYAN='\033[0;36m'
MAGENTA='\033[0;35m'
BOLD='\033[1m'
DIM='\033[2m'
YELLOW='\033[0;33m'
GREEN='\033[0;32m'
RED='\033[0;31m'
RESET='\033[0m'

MODE="dev"
DO_SYNC=0
DO_KILL=1
START_BACKEND=1
START_FRONTEND=1
RESTORE_DB=""

usage() {
  cat <<'EOF'
start.sh — launch backend + frontend

Defaults:  backend http://localhost:8080   frontend http://localhost:5174

Options:
  (no flags)          Start both with dev server (hot reload)
  --lite              Static preview (low CPU, no hot reload)
  --port PORT         Backend port (e.g. --port 8001)
  --backend-port PORT Same as --port
  --frontend-port PORT  UI port (default 5174)
  --no-kill           Do not stop old agent processes; only pick a free port
  --backend           API only
  --frontend          UI only (needs API already running)
  --sync              Run `uv sync` before start
  --restore-db [FILE] Restore agent.db from backup (KG + jobs); then exit
  --help              This message

Examples:
  ./start.sh
  ./start.sh --dev
  ./start.sh --port 8001
  ./start.sh --no-kill
EOF
  exit 0
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dev) MODE="dev" ;;
    --lite) MODE="lite" ;;
    --sync) DO_SYNC=1 ;;
    --no-kill) DO_KILL=0 ;;
    --clean) DO_KILL=1 ;;  # legacy alias
    --port|--backend-port)
      if [[ -z "${2:-}" ]]; then
        echo -e "${RED}--port requires a number (e.g. --port 8001)${RESET}" >&2
        exit 1
      fi
      BACKEND_PORT_MIN="$2"
      BACKEND_PORT_MAX="$2"
      shift
      ;;
    --frontend-port)
      if [[ -z "${2:-}" ]]; then
        echo -e "${RED}--frontend-port requires a number${RESET}" >&2
        exit 1
      fi
      FRONTEND_PORT_MIN="$2"
      FRONTEND_PORT_MAX="$2"
      shift
      ;;
    --backend) START_FRONTEND=0 ;;
    --frontend) START_BACKEND=0 ;;
    --restore-db)
      if [[ "${2:-}" == --* || -z "${2:-}" ]]; then
        RESTORE_DB="latest"
      else
        RESTORE_DB="$2"
        shift
      fi
      START_BACKEND=0
      START_FRONTEND=0
      ;;
    --help|-h) usage ;;
    *)
      echo -e "${RED}Unknown option: $1${RESET}"
      usage
      ;;
  esac
  shift
done

if [[ ! -f "$UV" ]]; then
  echo "uv not found at $UV"
  echo "Install: curl -Ls https://astral.sh/uv/install.sh | sh"
  exit 1
fi

is_sqlite_db() {
  local f="$1"
  [[ -f "$f" ]] || return 1
  [[ "$(basename "$f")" == *-wal || "$(basename "$f")" == *-shm || "$(basename "$f")" == *-journal ]] && return 1
  file "$f" 2>/dev/null | grep -q 'SQLite 3.x database'
}

list_db_backups() {
  local f
  for f in "$RUNS_DIR"/agent.db.bak.*; do
    [[ -e "$f" ]] || continue
    is_sqlite_db "$f" || continue
    printf '%s\n' "$f"
  done | sort -r
}

restore_db() {
  local src="$1"
  if [[ "$src" == "latest" ]]; then
    src="$(list_db_backups | head -1 || true)"
    if [[ -z "$src" ]]; then
      echo -e "${RED}No valid SQLite backups found in $RUNS_DIR (agent.db.bak.*)${RESET}"
      echo -e "${DIM}Ignore -wal / -shm sidecar files — only full .db backups count.${RESET}"
      exit 1
    fi
  elif [[ "$src" != /* ]]; then
    src="$ROOT/$src"
  fi
  if [[ ! -f "$src" ]]; then
    echo -e "${RED}Backup not found: $src${RESET}"
    exit 1
  fi
  if ! is_sqlite_db "$src"; then
    echo -e "${RED}Not a SQLite database: $src${RESET}"
    echo -e "${DIM}Pick a file like agent.db.bak.YYYYMMDD_HHMMSS (not -wal / -shm).${RESET}"
    exit 1
  fi
  mkdir -p "$RUNS_DIR"
  if [[ -f "$DB_PATH" ]] && is_sqlite_db "$DB_PATH"; then
    local stamp
    stamp="$(date +%Y%m%d_%H%M%S)"
    cp "$DB_PATH" "$RUNS_DIR/agent.db.bak.$stamp"
    echo -e "${DIM}Saved current DB → agent.db.bak.$stamp${RESET}"
  fi
  rm -f "$DB_PATH-wal" "$DB_PATH-shm" "$DB_PATH-journal"
  cp "$src" "$DB_PATH"
  rm -f "$DB_PATH-wal" "$DB_PATH-shm" "$DB_PATH-journal"
  echo -e "${GREEN}Restored KG + jobs database from:${RESET}"
  echo "  $src"
  if command -v sqlite3 >/dev/null 2>&1; then
    local nodes edges
    nodes="$(sqlite3 "$DB_PATH" 'SELECT COUNT(*) FROM kg_nodes;' 2>/dev/null || echo '?')"
    edges="$(sqlite3 "$DB_PATH" 'SELECT COUNT(*) FROM kg_edges;' 2>/dev/null || echo '?')"
    echo -e "${DIM}  kg_nodes=$nodes  kg_edges=$edges${RESET}"
    if [[ "$nodes" == "?" || "$edges" == "?" ]]; then
      echo -e "${RED}Restore failed — restored file is not readable as agent.db${RESET}"
      exit 1
    fi
  fi
  echo -e "${YELLOW}Restart the app: ./start.sh${RESET}"
}

if [[ -n "$RESTORE_DB" ]]; then
  restore_db "$RESTORE_DB"
  exit 0
fi

port_pids() {
  lsof -ti ":$1" 2>/dev/null || true
}

is_our_process() {
  local pid="$1"
  local cmd
  cmd="$(ps -p "$pid" -o command= 2>/dev/null || true)"
  [[ -z "$cmd" ]] && return 1
  [[ "$cmd" == *"$ROOT"* ]] || [[ "$cmd" == *"uvicorn server:app"* ]] || \
    [[ "$cmd" == *"vite"* ]] || [[ "$cmd" == *"npm run dev"* ]] || \
    [[ "$cmd" == *"npm run preview"* ]]
}

kill_port_listeners() {
  local port="$1"
  local force="${2:-0}"
  local pid
  for pid in $(port_pids "$port"); do
    if [[ "$DO_KILL" -eq 1 ]] && is_our_process "$pid"; then
      echo -e "${YELLOW}Stopping stale agent on :$port (PID $pid)${RESET}" >&2
      kill "$pid" 2>/dev/null || true
    elif [[ "$force" -eq 1 ]]; then
      kill "$pid" 2>/dev/null || true
    fi
  done
  sleep 0.4
  if [[ -n "$(port_pids "$port")" && "$DO_KILL" -eq 1 ]]; then
    for pid in $(port_pids "$port"); do
      if is_our_process "$pid"; then
        kill -9 "$pid" 2>/dev/null || true
      fi
    done
    sleep 0.2
  fi
}

# Prints ONLY the port number on stdout (for BACKEND_PORT="$(resolve_port ...)").
# Human messages go to stderr.
resolve_port() {
  local name="$1"
  local min="$2"
  local max="$3"
  local port="$min"
  local pids

  while [[ "$port" -le "$max" ]]; do
    kill_port_listeners "$port"
    pids="$(port_pids "$port")"
    if [[ -z "$pids" ]]; then
      if [[ "$port" -ne "$min" ]]; then
        echo -e "${DIM}$name: port $min busy — using $port${RESET}" >&2
      fi
      printf '%s\n' "$port"
      return 0
    fi
    port=$((port + 1))
  done

  echo -e "${RED}No free $name port in range $min–$max${RESET}" >&2
  return 1
}

wait_for_backend() {
  local i
  for i in {1..40}; do
    if curl -sf "http://127.0.0.1:$BACKEND_PORT/health" >/dev/null 2>&1; then
      return 0
    fi
    sleep 0.25
  done
  echo -e "${RED}Backend did not become ready on :$BACKEND_PORT${RESET}"
  return 1
}

# Keep ML / embedding libs from grabbing every core (big win on laptop CPUs).
export OMP_NUM_THREADS="${OMP_NUM_THREADS:-1}"
export MKL_NUM_THREADS="${MKL_NUM_THREADS:-1}"
export OPENBLAS_NUM_THREADS="${OPENBLAS_NUM_THREADS:-1}"
export TOKENIZERS_PARALLELISM="${TOKENIZERS_PARALLELISM:-false}"
export PYTHONDONTWRITEBYTECODE="${PYTHONDONTWRITEBYTECODE:-1}"
export ANONYMIZED_TELEMETRY="${ANONYMIZED_TELEMETRY:-False}"
export PYTHONUNBUFFERED=1

# Prefer API embeddings over loading a local ONNX model when a key is present.
if [[ -f "$AGENT_DIR/.env" ]] && grep -q '^OPENAI_API_KEY=.\+' "$AGENT_DIR/.env" 2>/dev/null; then
  export USE_OPENAI_EMBEDDINGS="${USE_OPENAI_EMBEDDINGS:-1}"
fi

cleanup() {
  echo ""
  echo -e "${DIM}Shutting down…${RESET}"
  kill 0 2>/dev/null || true
  wait 2>/dev/null || true
  exit 0
}
trap cleanup SIGINT SIGTERM

if [[ "$START_BACKEND" -eq 1 ]]; then
  BACKEND_PORT="$(resolve_port "Backend" "$BACKEND_PORT_MIN" "$BACKEND_PORT_MAX")"
  export BACKEND_PORT
fi
if [[ "$START_FRONTEND" -eq 1 ]]; then
  FRONTEND_PORT="$(resolve_port "Frontend" "$FRONTEND_PORT_MIN" "$FRONTEND_PORT_MAX")"
  export FRONTEND_PORT
fi

if [[ "$DO_SYNC" -eq 1 ]]; then
  echo -e "${DIM}Syncing Python deps…${RESET}"
  cd "$ROOT" && "$UV" sync --quiet
elif [[ ! -d "$ROOT/.venv" ]]; then
  echo -e "${DIM}First run — syncing Python deps…${RESET}"
  cd "$ROOT" && "$UV" sync --quiet
fi

if [[ "$START_FRONTEND" -eq 1 && ! -d "$FRONTEND_DIR/node_modules" ]]; then
  echo -e "${DIM}Installing frontend deps…${RESET}"
  cd "$FRONTEND_DIR" && npm install
fi

if [[ "$START_BACKEND" -eq 1 ]]; then
  echo -e "${CYAN}${BOLD}▶ Backend${RESET}  ${DIM}http://localhost:$BACKEND_PORT${RESET}"
  # Invoke the project's .venv python DIRECTLY (not `uv run` / PATH uvicorn).
  # An active anaconda env can shadow uv's resolution and run the wrong
  # interpreter — that pulls in anaconda's chromadb 0.5.23, which writes
  # self-incompatible collection configs and crashes ingest with
  # KeyError('_type'). The pinned .venv ships chromadb 1.5.8.
  VENV_PY="$ROOT/.venv/bin/python"
  if [[ ! -x "$VENV_PY" ]]; then
    echo -e "${DIM}.venv missing — syncing deps…${RESET}"
    cd "$ROOT" && "$UV" sync --quiet
  fi
  "$VENV_PY" -m uvicorn server:app \
    --app-dir "$AGENT_DIR" \
    --host 127.0.0.1 \
    --port "$BACKEND_PORT" \
    --workers 1 \
    --limit-concurrency 20 &
  BACKEND_PID=$!

  if ! wait_for_backend; then
    kill "$BACKEND_PID" 2>/dev/null || true
    exit 1
  fi
  echo -e "${DIM}  Backend ready${RESET}"
fi

if [[ "$START_FRONTEND" -eq 1 ]]; then
  cd "$FRONTEND_DIR"
  if [[ "$MODE" == "lite" ]]; then
    if [[ ! -d dist ]]; then
      echo -e "${DIM}Building frontend once (subsequent starts skip this)…${RESET}"
      npm run build
    fi
    echo -e "${MAGENTA}${BOLD}▶ Frontend${RESET} ${DIM}http://localhost:$FRONTEND_PORT (preview — low CPU)${RESET}"
    npm run preview -- --host 127.0.0.1 --port "$FRONTEND_PORT" &
  else
    echo -e "${MAGENTA}${BOLD}▶ Frontend${RESET} ${DIM}http://localhost:$FRONTEND_PORT (dev + HMR)${RESET}"
    npm run dev -- --host 127.0.0.1 --port "$FRONTEND_PORT" &
  fi
fi

echo ""
echo -e "  ${BOLD}Research Agent running${RESET} ${DIM}($MODE mode)${RESET}"
[[ "$START_BACKEND" -eq 1 ]] && echo -e "  ${CYAN}Backend${RESET}   → http://localhost:$BACKEND_PORT"
[[ "$START_FRONTEND" -eq 1 ]] && echo -e "  ${MAGENTA}Frontend${RESET}  → http://localhost:$FRONTEND_PORT"
echo -e "  ${DIM}Tip: ./start.sh --help · Ctrl+C stops both servers${RESET}"
echo -e "  ${DIM}Ctrl+C to stop${RESET}"
echo ""

wait
