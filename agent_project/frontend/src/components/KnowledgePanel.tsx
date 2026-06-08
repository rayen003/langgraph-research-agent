import { useMemo, useState, useCallback, useEffect } from 'react'
import { Search, GitCompare, RefreshCw, X, ShieldAlert } from 'lucide-react'
import { useKnowledgeGraph, type KgNode } from '../hooks/useKnowledgeGraph'
import { KgCanvas } from './KgCanvas'
import { KgFilterSidebar } from './KgFilterSidebar'
import { KgQueryPanel } from './KgQueryPanel'
import { KgRunInspector } from './KgRunInspector'
import { KgRerunPicker } from './KgRerunPicker'
import { KgHubPanel } from './KgHubPanel'
import { KgDcfHistoryPanel } from './KgDcfHistoryPanel'
import { KgFinancialsPanel } from './KgFinancialsPanel'
import { KgCompareRuns } from './KgCompareRuns'
import { KgTimeline } from './KgTimeline'
import { KgAuditPanel } from './KgAuditPanel'
import { buildDcfDiffMessage } from '../lib/dcfDiff'
import { buildKgViewModel } from '../lib/kgViewModel'
import { ResizablePanel } from './ResizablePanel'
import { PanelHideButton } from './PanelHideButton'
import { usePanelHidden } from '../hooks/usePanelHidden'

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
  /** Increments when a rerun completes so the KG can refresh to show new nodes. */
  refreshTrigger?: number
}

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
  refreshTrigger,
}: Props) {
  const kg = useKnowledgeGraph(sessionId)
  const filterPanel = usePanelHidden('ui.panel.kg.filter.hidden')
  const dockPanel = usePanelHidden('ui.panel.kg.dock.hidden')

  // Refresh the KG whenever App signals a rerun has completed — picks up the
  // new dcf_run / run_assumption / run_output nodes written by the workflow.
  useEffect(() => {
    if (refreshTrigger == null || refreshTrigger === 0) return
    void kg.refresh()
  }, [refreshTrigger]) // eslint-disable-line react-hooks/exhaustive-deps
  const [rerunBusy, setRerunBusy] = useState(false)

  const [selectedNode, setSelectedNode] = useState<KgNode | null>(null)
  const [queryOpen, setQueryOpen] = useState(false)
  const [hiddenTickers, setHiddenTickers] = useState<Set<string>>(new Set())
  const [pendingRerun, setPendingRerun] = useState<PendingRerun | null>(null)
  const [compareOpen, setCompareOpen] = useState(false)
  const [auditOpen, setAuditOpen] = useState(false)
  const [historyReturnNode, setHistoryReturnNode] = useState<KgNode | null>(null)
  // Assembled comparison set — composite `ticker::run_id` keys.
  const [selectedRunKeys, setSelectedRunKeys] = useState<string[]>([])

  const toggleRunKey = useCallback((key: string) => {
    setSelectedRunKeys(prev =>
      prev.includes(key) ? prev.filter(k => k !== key) : [...prev, key])
  }, [])
  const compareKeySet = useMemo(() => new Set(selectedRunKeys), [selectedRunKeys])

  // ── Analyst view model: collapse raw graph into hubs ──────────────────────
  const viewModel = useMemo(
    () => buildKgViewModel(kg.nodes, kg.edges),
    [kg.nodes, kg.edges],
  )

  const handleNodeClick = useCallback((n: KgNode) => {
    // When the comparison artifact is open, clicking a DCF run ADDS it to the
    // assembled set instead of opening the inspector (assemble-by-click).
    if (compareOpen && n.node_type === 'dcf_run' && n.run_id) {
      toggleRunKey(`${n.ticker}::${n.run_id}`)
      return
    }
    // Company hub has no detail panel — clicking it just deselects.
    if (n.node_type === 'company') { setSelectedNode(null); return }
    // Financials hub (and any hub with categories) opens the tabbed dock panel
    // instead of spawning category sub-hubs on the canvas.
    setHistoryReturnNode(null)
    setSelectedNode(n)
  }, [compareOpen, toggleRunKey])

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

  // ── Render hub graph (filter by hidden ticker) ────────────────────────────
  // Category sub-hubs are no longer canvas nodes — they live in the Financials
  // dock panel — so the only filter is the ticker visibility toggle.
  const { visibleNodes, visibleEdges } = useMemo(() => {
    const vNodes = viewModel.hubNodes.filter(n => !hiddenTickers.has(n.ticker))
    const visIds = new Set(vNodes.map(n => n.id))
    const vEdges = viewModel.hubEdges.filter(e => visIds.has(e.src_id) && visIds.has(e.tgt_id))
    return { visibleNodes: vNodes, visibleEdges: vEdges }
  }, [viewModel, hiddenTickers])

  // ── Query highlight → roll matched raw nodes up to their hubs ─────────────
  // Graph lights the owning hub; if that hub's panel is open, the matched rows
  // glow (see matchedMemberIds passed to KgHubPanel).
  const matchedMemberIds = useMemo(
    () => new Set(kg.highlightPath),
    [kg.highlightPath],
  )
  const highlightSet = useMemo(() => {
    const hubs = new Set<string>()
    for (const rawId of kg.highlightPath) {
      const hub = viewModel.hubForRaw.get(rawId)
      if (hub) hubs.add(hub)
    }
    return hubs
  }, [kg.highlightPath, viewModel])
  const highlightEdgeSet = useMemo(() => new Set<string>(), [])

  // After a query, auto-open the PRIMARY match's hub panel (first cited raw
  // node → its owning hub) so the lit row is immediately visible. Category
  // members roll up to the Financials hub → opens the tabbed panel.
  useEffect(() => {
    if (highlightSet.size === 0) return
    const primaryRaw = kg.highlightPath[0]
    if (primaryRaw) {
      const hubId = viewModel.hubForRaw.get(primaryRaw)
      const hub = hubId ? viewModel.hubNodes.find(n => n.id === hubId) : undefined
      if (hub && hub.node_type !== 'company') setSelectedNode(hub)
    }
  }, [highlightSet, kg.highlightPath, viewModel])

  // ── Ticker filter toggle ───────────────────────────────────────────────────
  const toggleTicker = useCallback((t: string) => {
    setHiddenTickers(prev => { const n = new Set(prev); n.has(t) ? n.delete(t) : n.add(t); return n })
  }, [])
  const resetFilters = useCallback(() => {
    setHiddenTickers(new Set())
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
      // Lineage: this rerun derives from the run the user opened in the KG.
      parent_run_id: originalRunNode.run_id || undefined,
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

  // Any ticker with ≥2 DCF runs → enable cross-run comparison.
  const hasMultiRun = useMemo(() => {
    const byTicker = new Map<string, Set<string>>()
    for (const n of kg.nodes) {
      if (n.node_type !== 'dcf_run') continue
      const set = byTicker.get(n.ticker) || new Set()
      set.add(n.run_id || n.id)
      byTicker.set(n.ticker, set)
    }
    return Array.from(byTicker.values()).some(s => s.size >= 2)
  }, [kg.nodes])

  // Create a user_belief node (analyst-stated conviction).
  const handleCreateBelief = useCallback(async (ticker: string, field: string, value: unknown) => {
    await kg.createNode({ ticker, node_type: 'user_belief', field, value, source: 'user_stated' })
  }, [kg])

  if (!sessionId) {
    return (
      <div className="fixed inset-0 z-50 bg-bg flex items-center justify-center text-ink-muted text-sm">
        No active session
        <button onClick={onClose} className="ml-3 text-ink-muted hover:text-ink">close</button>
      </div>
    )
  }

  const isRunNode = selectedNode?.node_type === 'dcf_run'
  const isNewsHub = selectedNode?.node_type === 'news_hub'
  const isFinancialsHub = selectedNode?.node_type === 'financials_hub'
  const isDcfHistoryHub = selectedNode?.node_type === 'dcf_history_hub'
  const hubMembers = selectedNode && (isNewsHub || isDcfHistoryHub)
    ? (viewModel.membersByHub.get(selectedNode.id) || [])
    : []
  // Financials hub → tabbed category panel (replaces on-canvas sub-hubs).
  const financialsCategories = isFinancialsHub && selectedNode
    ? (viewModel.childHubs.get(selectedNode.id) || []).map(sub => ({
        key: String(sub.field).replace(/^fin_/, ''),
        label: String((sub.value as Record<string, unknown>)?.label ?? sub.field),
        members: viewModel.membersByHub.get(sub.id) || [],
      }))
    : []

  // ── Single dock occupant (mutually exclusive) ─────────────────────────────
  // Priority: query > compare > audit > selected node panel. Opening one closes others
  // via the handlers below, so at most one is ever truthy.
  const dockOpen = queryOpen || compareOpen || auditOpen || !!selectedNode

  const openQuery = () => { setQueryOpen(true); setCompareOpen(false); setAuditOpen(false); setSelectedNode(null) }
  const openCompare = () => { setCompareOpen(true); setQueryOpen(false); setAuditOpen(false); setSelectedNode(null) }
  const openAudit = () => { setAuditOpen(true); setQueryOpen(false); setCompareOpen(false); setSelectedNode(null) }
  const closeDock = () => { setQueryOpen(false); setCompareOpen(false); setAuditOpen(false); setSelectedNode(null); setHistoryReturnNode(null) }

  return (
    <div className="fixed inset-0 z-50 bg-bg flex flex-col text-ink">
      {/* ── Top bar ────────────────────────────────────────────────────── */}
      <header className="flex items-center gap-3 px-4 h-11 border-b border-edge flex-shrink-0 bg-surface">
        <span className="text-[11px] font-medium tracking-[0.07em] uppercase text-ink-muted">
          Knowledge Graph
        </span>
        <span className="hidden sm:inline text-edge-2">·</span>
        <span className="hidden sm:inline text-[11px] text-ink-dim tabular-nums truncate">
          {visibleNodes.length} hubs · {kg.nodes.length} facts
          {tickers.length > 0 && ` · ${tickers.join(' · ')}`}
        </span>

        {isRunActive && (
          <span className="flex items-center gap-1.5 text-[10px] uppercase tracking-wide text-ink-dim">
            <span className="w-1.5 h-1.5 rounded-full bg-accent animate-pulse" />
            running
          </span>
        )}

        <div className="ml-auto flex items-center gap-1.5">
          {dockOpen && (
            <ToolbarButton onClick={dockPanel.hide}>
              Hide panel
            </ToolbarButton>
          )}
          {hasMultiRun && (
            <ToolbarButton active={compareOpen} onClick={() => (compareOpen ? closeDock() : openCompare())}>
              <GitCompare size={14} /> Compare
            </ToolbarButton>
          )}
          <ToolbarButton active={queryOpen} onClick={() => (queryOpen ? closeDock() : openQuery())}>
            <Search size={14} /> Ask
          </ToolbarButton>
          <ToolbarButton active={auditOpen} onClick={() => (auditOpen ? closeDock() : openAudit())}>
            <ShieldAlert size={14} /> Audit
          </ToolbarButton>
          <ToolbarButton onClick={kg.refresh} disabled={kg.loading}>
            <RefreshCw size={14} className={kg.loading ? 'animate-spin' : ''} /> Refresh
          </ToolbarButton>
          <button
            onClick={onClose}
            aria-label="Close knowledge graph"
            className="ml-1 p-1.5 rounded text-ink-dim hover:text-ink hover:bg-surface-2 transition"
          >
            <X size={16} />
          </button>
        </div>
      </header>

      {/* ── Body: filters | canvas+timeline | dock ───────────────────────── */}
      <div className="flex-1 flex overflow-hidden">
        <ResizablePanel
          defaultWidth={200}
          minWidth={140}
          maxWidth={360}
          side="right"
          storageKey="ui.kgFilterSidebarWidth"
          className="border-r border-edge bg-surface"
          hidden={filterPanel.hidden}
          onReveal={filterPanel.show}
          revealLabel="Filters"
        >
          <KgFilterSidebar
            nodes={kg.nodes}
            hiddenTickers={hiddenTickers}
            onToggleTicker={toggleTicker}
            onResetFilters={resetFilters}
            onHide={filterPanel.hide}
          />
        </ResizablePanel>

        {/* Canvas column — flexes; never squeezed below usable timeline width */}
        <div className="flex-1 min-w-[280px] min-h-0 relative flex flex-col">
          <div className="flex-1 min-h-0 relative overflow-hidden">
            {/* KG write toasts are now rendered app-level in App.tsx so they
                show even when this panel is closed. */}
            {kg.error && (
              <div className="absolute top-3 left-3 z-10 px-2.5 py-1.5 rounded-md bg-down/10 text-down text-[11px] border border-down/30">
                {kg.error}
              </div>
            )}
            {kg.nodes.length === 0 ? (
              <EmptyCanvas
                title="No knowledge yet"
                body="Run a DCF or chat about a ticker to populate the graph."
              />
            ) : visibleNodes.length === 0 ? (
              <EmptyCanvas title="All nodes filtered out" body="Adjust ticker filters on the left." />
            ) : (
              <KgCanvas
                nodes={visibleNodes}
                edges={visibleEdges}
                highlightSet={highlightSet}
                highlightEdgeSet={highlightEdgeSet}
                onNodeClick={handleNodeClick}
                companySummary={companySummary}
                compareKeys={compareKeySet}
                dragRunsToCompare={compareOpen}
              />
            )}
          </div>

          {/* Timeline — bottom bar, spans canvas width only */}
          <KgTimeline
            nodes={kg.nodes}
            highlightRunIds={highlightSet}
            onSelectRun={n => { setCompareOpen(false); setQueryOpen(false); setSelectedNode(n) }}
          />
        </div>

        {/* ── Right dock — one panel at a time ──────────────────────────── */}
        {dockOpen && (
          <ResizablePanel
            defaultWidth={420}
            minWidth={300}
            maxWidth={720}
            side="left"
            storageKey="ui.kgDockWidth"
            className="border-l border-edge bg-surface"
            hidden={dockPanel.hidden}
            onReveal={dockPanel.show}
            revealLabel="Inspect"
          >
            {queryOpen && (
              <KgQueryPanel
                onClose={closeDock}
                onQuery={kg.queryNL}
                onClearHighlight={kg.clearHighlight}
                highlightCount={kg.highlightPath.length}
              />
            )}
            {compareOpen && (
              <KgCompareRuns
                nodes={kg.nodes}
                selectedRunKeys={selectedRunKeys}
                onToggleRun={toggleRunKey}
                onClear={() => setSelectedRunKeys([])}
                onChat={kg.compareChat}
                onClose={closeDock}
              />
            )}
            {selectedNode && isRunNode && (
              <KgRunInspector
                runNode={selectedNode}
                allNodes={kg.nodes}
                onClose={closeDock}
                onRerun={handleRerunRequested}
                rerunBusy={rerunBusy || isRunActive || pendingRerun !== null}
                highlightIds={matchedMemberIds}
                onBack={historyReturnNode ? () => setSelectedNode(historyReturnNode) : undefined}
                backLabel="History"
              />
            )}
            {auditOpen && (
              <KgAuditPanel
                ticker={tickers.length === 1 ? tickers[0] : undefined}
                availableTickers={tickers}
                onClose={closeDock}
              />
            )}
            {selectedNode && isNewsHub && (
              <KgHubPanel
                title={`${selectedNode.ticker} · News`}
                subtitle={`${hubMembers.length} item${hubMembers.length === 1 ? '' : 's'}`}
                members={hubMembers}
                highlightIds={matchedMemberIds}
                onClose={closeDock}
              />
            )}
            {selectedNode && isDcfHistoryHub && (
              <KgDcfHistoryPanel
                ticker={selectedNode.ticker}
                runs={hubMembers}
                allNodes={kg.nodes}
                highlightIds={matchedMemberIds}
                onClose={closeDock}
                onSelectRun={run => { setHistoryReturnNode(selectedNode); setSelectedNode(run) }}
              />
            )}
            {selectedNode && isFinancialsHub && (
              <KgFinancialsPanel
                ticker={selectedNode.ticker}
                categories={financialsCategories}
                highlightIds={matchedMemberIds}
                onClose={closeDock}
                onCreateBelief={handleCreateBelief}
                onDeleteNode={kg.deleteNode}
              />
            )}
          </ResizablePanel>
        )}
      </div>

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

// ── Top-bar button (single accent language) ──────────────────────────────────
function ToolbarButton({
  children, active, onClick, disabled,
}: {
  children: React.ReactNode; active?: boolean; onClick: () => void; disabled?: boolean
}) {
  return (
    <button
      onClick={onClick}
      disabled={disabled}
      className={`flex items-center gap-1.5 text-[12px] px-2.5 py-1.5 rounded-md border transition disabled:opacity-50 ${
        active
          ? 'bg-accent-soft text-accent border-accent/40'
          : 'text-ink-muted border-edge hover:text-ink hover:bg-surface-2'
      }`}
    >
      {children}
    </button>
  )
}

function EmptyCanvas({ title, body }: { title: string; body: string }) {
  return (
    <div role="status" className="h-full flex flex-col items-center justify-center text-center px-6">
      <div className="text-[11px] font-medium tracking-wide uppercase text-ink-dim">{title}</div>
      <div className="text-[12px] text-ink-dim mt-1.5 max-w-[240px] leading-relaxed">{body}</div>
    </div>
  )
}
