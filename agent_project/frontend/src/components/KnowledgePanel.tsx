import { useMemo, useState, useCallback } from 'react'
import { useKnowledgeGraph, type KgNode } from '../hooks/useKnowledgeGraph'
import { KgCanvas } from './KgCanvas'
import { KgFilterSidebar } from './KgFilterSidebar'
import { KgQueryPanel } from './KgQueryPanel'
import { KgNodeCard } from './KgNodeCard'
import { KgRunInspector } from './KgRunInspector'
import { KgRerunPicker } from './KgRerunPicker'
import { buildDcfDiffMessage } from '../lib/dcfDiff'

interface Props {
  sessionId: string | null
  onClose: () => void

  // Coordination with App for rerun targeting + notifications
  activeSessionTitle?: string
  activeChatThreadId?: string
  onCreateNewSession?: () => { id: string; chatThreadId: string }
  /**
   * Kick off a DCF rerun via App-level useAgentRun. App is responsible for
   * surfacing steps/activity in ExecutionSidebar, routing the assistant
   * response to `sessionId`, and updating the rerun toast. Returns the
   * backend thread_id once the run starts.
   */
  onStartRerun?: (info: {
    ticker: string
    sessionId: string
    chatThreadId: string | undefined
    target: 'current' | 'new'
    query: string
    diffText: string
  }) => Promise<string | null>
  /** Whether App's run state is currently active (disables rerun button). */
  isRunActive?: boolean
}

const MAX_EXPANDED_RUNS_PER_TICKER = 3

interface PendingRerun {
  ticker: string
  horizonYears: number
  overrides: Record<string, number>
  originalRunNode: KgNode
  originalAssumptions: KgNode[]
}

export function KnowledgePanel({
  sessionId,
  onClose,
  activeSessionTitle = '(unnamed)',
  activeChatThreadId,
  onCreateNewSession,
  onStartRerun,
  isRunActive = false,
}: Props) {
  const kg = useKnowledgeGraph(sessionId)
  const [rerunBusy, setRerunBusy] = useState(false)

  const [selectedNode, setSelectedNode] = useState<KgNode | null>(null)
  const [queryOpen, setQueryOpen] = useState(false)
  const [hiddenTypes, setHiddenTypes] = useState<Set<string>>(new Set())
  const [hiddenTickers, setHiddenTickers] = useState<Set<string>>(new Set())
  const [hiddenSources, setHiddenSources] = useState<Set<string>>(new Set())
  const [hideOrphans, setHideOrphans] = useState(false)
  const [showAllRuns, setShowAllRuns] = useState(false)
  const [pendingRerun, setPendingRerun] = useState<PendingRerun | null>(null)

  const handleNodeClick = useCallback((n: KgNode) => setSelectedNode(n), [])

  // ── Edge count for orphan filter ──────────────────────────────────────────
  const edgeCountByNodeId = useMemo(() => {
    const m = new Map<string, number>()
    for (const e of kg.edges) {
      m.set(e.src_id, (m.get(e.src_id) || 0) + 1)
      m.set(e.tgt_id, (m.get(e.tgt_id) || 0) + 1)
    }
    return m
  }, [kg.edges])

  // ── Auto-collapse old runs ────────────────────────────────────────────────
  const expandedRunIds = useMemo(() => {
    if (showAllRuns) return null
    const byTicker = new Map<string, KgNode[]>()
    for (const n of kg.nodes) {
      if (n.node_type !== 'dcf_run') continue
      const list = byTicker.get(n.ticker) || []
      list.push(n)
      byTicker.set(n.ticker, list)
    }
    const expanded = new Set<string>()
    for (const [, runs] of byTicker) {
      runs.sort((a, b) => b.updated_at - a.updated_at)
      for (const r of runs.slice(0, MAX_EXPANDED_RUNS_PER_TICKER)) {
        if (r.run_id) expanded.add(r.run_id)
      }
    }
    return expanded
  }, [kg.nodes, showAllRuns])

  // ── Company summary for tooltip enrichment ────────────────────────────────
  const companySummary = useMemo(() => {
    const out = new Map<string, { implied?: number; spot?: number; delta?: number }>()
    const latestRunByTicker = new Map<string, KgNode>()
    for (const n of kg.nodes) {
      if (n.node_type !== 'dcf_run') continue
      const prev = latestRunByTicker.get(n.ticker)
      if (!prev || n.updated_at > prev.updated_at) latestRunByTicker.set(n.ticker, n)
    }
    for (const [ticker, run] of latestRunByTicker) {
      if (!run.run_id) continue
      let implied: number | undefined
      let spot: number | undefined
      for (const n of kg.nodes) {
        if (n.run_id !== run.run_id || n.node_type !== 'run_output') continue
        const v = typeof n.value === 'number' ? n.value : Number(n.value)
        if (!isFinite(v)) continue
        if (n.field === 'implied_share_price') implied = v
        else if (n.field === 'current_price') spot = v
      }
      const delta = (implied !== undefined && spot !== undefined && spot > 0)
        ? (implied - spot) / spot : undefined
      out.set(ticker, { implied, spot, delta })
    }
    return out
  }, [kg.nodes])

  // ── Apply filters + auto-collapse ─────────────────────────────────────────
  const { visibleNodes, visibleEdges } = useMemo(() => {
    const visIds = new Set<string>()
    const vNodes = kg.nodes.filter(n => {
      if (hiddenTypes.has(n.node_type)) return false
      if (hiddenTickers.has(n.ticker)) return false
      if (hiddenSources.has(n.source)) return false
      if (expandedRunIds !== null && n.run_id) {
        const isRunScopedLeaf =
          n.node_type === 'run_assumption' ||
          n.node_type === 'run_output' ||
          n.node_type === 'run_scenario'
        if (isRunScopedLeaf && !expandedRunIds.has(n.run_id)) return false
      }
      if (hideOrphans && (edgeCountByNodeId.get(n.id) || 0) === 0) return false
      visIds.add(n.id)
      return true
    })
    const vEdges = kg.edges.filter(e => visIds.has(e.src_id) && visIds.has(e.tgt_id))
    return { visibleNodes: vNodes, visibleEdges: vEdges }
  }, [kg.nodes, kg.edges, hiddenTypes, hiddenTickers, hiddenSources, hideOrphans, edgeCountByNodeId, expandedRunIds])

  const highlightSet = useMemo(() => new Set(kg.highlightPath), [kg.highlightPath])

  // ── Filter toggles ─────────────────────────────────────────────────────────
  const toggleType = useCallback((t: string) => {
    setHiddenTypes(prev => { const n = new Set(prev); n.has(t) ? n.delete(t) : n.add(t); return n })
  }, [])
  const toggleTicker = useCallback((t: string) => {
    setHiddenTickers(prev => { const n = new Set(prev); n.has(t) ? n.delete(t) : n.add(t); return n })
  }, [])
  const toggleSource = useCallback((s: string) => {
    setHiddenSources(prev => { const n = new Set(prev); n.has(s) ? n.delete(s) : n.add(s); return n })
  }, [])
  const resetFilters = useCallback(() => {
    setHiddenTypes(new Set()); setHiddenTickers(new Set()); setHiddenSources(new Set())
    setHideOrphans(false); setShowAllRuns(false)
  }, [])

  // ── Rerun handlers ────────────────────────────────────────────────────────
  // Step 1: Inspector clicks Rerun → KgRerunPicker opens to pick target.
  const handleRerunRequested = useCallback(async (overrides: Record<string, number>, horizonYears: number) => {
    if (!selectedNode || !selectedNode.ticker || !selectedNode.run_id) return
    const originalAssumptions = kg.nodes.filter(
      n => n.run_id === selectedNode.run_id && n.node_type === 'run_assumption',
    )
    setPendingRerun({
      ticker: selectedNode.ticker,
      horizonYears,
      overrides,
      originalRunNode: selectedNode,
      originalAssumptions,
    })
  }, [selectedNode, kg.nodes])

  // Step 2: User picks current or new chat → fire the actual rerun via App's
  // useAgentRun.startRun. Steps/activity surface in the standard
  // ExecutionSidebar (no separate stream), and App routes the assistant
  // response back to the target session.
  const fireRerun = useCallback(async (target: 'current' | 'new') => {
    if (!pendingRerun || !onStartRerun) { setPendingRerun(null); return }
    const { ticker, horizonYears, overrides, originalRunNode, originalAssumptions } = pendingRerun
    setPendingRerun(null)

    let useSessionId = sessionId || ''
    let useThreadId: string | undefined

    if (target === 'current') {
      useThreadId = activeChatThreadId
    } else {
      if (!onCreateNewSession) {
        useThreadId = undefined
      } else {
        const newSession = onCreateNewSession()
        useSessionId = newSession.id
        useThreadId = newSession.chatThreadId
      }
    }

    const diffText = buildDcfDiffMessage(
      ticker, horizonYears, originalRunNode, originalAssumptions, overrides, target,
    )
    const approvalPayload = {
      ticker,
      horizon_years: horizonYears,
      all_assumptions: overrides,
    }
    const query = `[DCF_APPROVED]:${JSON.stringify(approvalPayload)}`

    setRerunBusy(true)
    try {
      await onStartRerun({
        ticker,
        sessionId: useSessionId,
        chatThreadId: useThreadId,
        target,
        query,
        diffText,
      })
    } finally {
      setRerunBusy(false)
      // Refresh KG once the request is in flight so any user-stated locks
      // applied during the rerun show up; final dcf_run node appears on the
      // next refresh after completion (App can trigger via its own hook).
      void kg.refresh()
    }
  }, [pendingRerun, sessionId, activeChatThreadId, onCreateNewSession, onStartRerun, kg])

  // ── Header summary ─────────────────────────────────────────────────────────
  const tickers = useMemo(() => {
    const s = new Set<string>()
    for (const n of kg.nodes) if (n.ticker) s.add(n.ticker)
    return Array.from(s)
  }, [kg.nodes])

  if (!sessionId) {
    return (
      <div className="fixed inset-0 z-50 bg-[#0a0a0a] flex items-center justify-center text-zinc-600 text-sm">
        No active session
        <button onClick={onClose} className="ml-3 text-zinc-400 hover:text-zinc-200">close</button>
      </div>
    )
  }

  const isRunNode = selectedNode?.node_type === 'dcf_run'

  return (
    <div className="fixed inset-0 z-50 bg-[#0a0a0a] flex flex-col">
      {/* Top bar */}
      <div className="px-4 py-2.5 border-b border-[#1c1c24] flex items-center gap-3 flex-shrink-0">
        <div className="text-zinc-200 text-sm font-medium">Knowledge Graph</div>
        <div className="text-zinc-600 text-[11px]">
          {visibleNodes.length}/{kg.nodes.length} nodes · {visibleEdges.length}/{kg.edges.length} edges
          {tickers.length > 0 && ` · ${tickers.join(', ')}`}
        </div>

        {isRunActive && (
          <div className="text-[10px] px-2 py-0.5 rounded bg-indigo-500/15 text-indigo-300 border border-indigo-500/30 flex items-center gap-1.5">
            <span className="inline-block w-1.5 h-1.5 rounded-full bg-indigo-400 animate-pulse" />
            Run in progress
          </div>
        )}

        <div className="ml-auto flex items-center gap-2">
          <label className="text-[10px] text-zinc-500 flex items-center gap-1.5 cursor-pointer select-none">
            <input
              type="checkbox"
              checked={showAllRuns}
              onChange={() => setShowAllRuns(v => !v)}
              className="accent-indigo-500"
            />
            show all run details
          </label>
          <button
            onClick={() => setQueryOpen(o => !o)}
            className={`text-[11px] px-3 py-1.5 rounded border transition ${
              queryOpen
                ? 'bg-indigo-500/25 text-indigo-200 border-indigo-500/50'
                : 'bg-indigo-500/10 text-indigo-300 border-indigo-500/30 hover:bg-indigo-500/20'
            }`}
          >
            🔎 Ask
          </button>
          <button
            onClick={kg.refresh}
            disabled={kg.loading}
            className="text-[11px] px-3 py-1.5 rounded bg-zinc-800 text-zinc-400 border border-zinc-700 hover:bg-zinc-700 disabled:opacity-50"
          >
            {kg.loading ? '…' : '↻ Refresh'}
          </button>
          <button
            onClick={onClose}
            className="text-zinc-400 hover:text-zinc-200 text-[16px] ml-2 px-2"
            title="Close"
          >
            ✕
          </button>
        </div>
      </div>

      {/* Body */}
      <div className="flex-1 flex overflow-hidden relative">
        <KgFilterSidebar
          nodes={kg.nodes}
          hiddenTypes={hiddenTypes}
          hiddenTickers={hiddenTickers}
          hiddenSources={hiddenSources}
          hideOrphans={hideOrphans}
          edgeCountByNodeId={edgeCountByNodeId}
          onToggleType={toggleType}
          onToggleTicker={toggleTicker}
          onToggleSource={toggleSource}
          onToggleHideOrphans={() => setHideOrphans(o => !o)}
          onResetFilters={resetFilters}
        />

        <div className="flex-1 relative">
          {kg.error && (
            <div className="absolute top-2 left-2 z-10 px-2 py-1 rounded bg-red-500/10 text-red-400 text-[11px]">
              {kg.error}
            </div>
          )}
          {kg.nodes.length === 0 ? (
            <div className="h-full flex items-center justify-center text-zinc-600 text-[12px] px-4 text-center">
              No knowledge yet for this session.
              <br />
              Run a DCF or chat about a ticker to populate the graph.
            </div>
          ) : visibleNodes.length === 0 ? (
            <div className="h-full flex items-center justify-center text-zinc-600 text-[12px]">
              All nodes filtered out.
            </div>
          ) : (
            <KgCanvas
              nodes={visibleNodes}
              edges={visibleEdges}
              highlightSet={highlightSet}
              onNodeClick={handleNodeClick}
              companySummary={companySummary}
            />
          )}

          {selectedNode && isRunNode && (
            <KgRunInspector
              runNode={selectedNode}
              allNodes={kg.nodes}
              onClose={() => setSelectedNode(null)}
              onRerun={handleRerunRequested}
              rerunBusy={rerunBusy || isRunActive || pendingRerun !== null}
            />
          )}
          {selectedNode && !isRunNode && (
            <KgNodeCard
              node={selectedNode}
              onClose={() => setSelectedNode(null)}
              onPatch={kg.patchNode}
              onDelete={kg.deleteNode}
            />
          )}
        </div>

        {queryOpen && (
          <KgQueryPanel
            onClose={() => setQueryOpen(false)}
            onQuery={kg.queryNL}
            onClearHighlight={kg.clearHighlight}
            highlightCount={kg.highlightPath.length}
          />
        )}
      </div>

      {/* Rerun target picker */}
      {pendingRerun && (
        <KgRerunPicker
          ticker={pendingRerun.ticker}
          currentSessionTitle={activeSessionTitle}
          onPickCurrent={() => fireRerun('current')}
          onPickNew={() => fireRerun('new')}
          onCancel={() => setPendingRerun(null)}
        />
      )}
    </div>
  )
}
