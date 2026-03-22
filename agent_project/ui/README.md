# Agent UI

React + Vite frontend for the LangGraph research agent. It talks to `agent_project/server.py` (FastAPI) via the Vite dev proxy.

## Prerequisites

- Node.js 20+
- Backend running on port **8000**

## Run

From `agent_project/`:

```bash
uv run python server.py
```

From `agent_project/ui/`:

```bash
npm install
npm run dev
```

Open the URL Vite prints (default **http://localhost:3000**). API calls to `/runs`, `/artifacts`, and `/health` are proxied to `http://localhost:8000`.

**Note:** If you still have a **Chainlit** tab or IDE preview open on `http://localhost:8000`, it will try to load Socket.IO and `/project/translations`. The FastAPI server answers those with harmless stubs so logs stay clean — but you should use **port 3000** for this UI, not 8000.

## Build

```bash
npm run build
```

Serve `dist/` behind any static host; configure that host to reverse-proxy the same API paths to the FastAPI backend.
