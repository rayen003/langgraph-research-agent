import { useState, useEffect, useRef, useCallback, useMemo } from 'react'
import { useAgentRun } from './hooks/useAgentRun'
import { useSessionManager } from './hooks/useSessionManager'
import { useJobs } from './hooks/useJobs'
import { useDocuments } from './hooks/useDocuments'
import { SessionsSidebar } from './components/SessionsSidebar'
import { MessageThread } from './components/MessageThread'
import { ExecutionSidebar } from './components/ExecutionSidebar'
import { DocumentPreview } from './components/DocumentPreview'
import { DeckPreview } from './components/DeckPreview'
import { JobsPanel } from './components/JobsPanel'
import { KnowledgePanel } from './components/KnowledgePanel'
import { KgNotificationPanel } from './components/KgNotificationPanel'
import type { KgNode } from './hooks/useKnowledgeGraph'
import { ResizablePanel } from './components/ResizablePanel'
import { usePanelHidden } from './hooks/usePanelHidden'
import { RerunToast, type RerunToastState } from './components/RerunToast'
import { SettingsButton } from './components/SettingsPanel'
import type { JobSummary, Mode } from './types'

let _msgCounter = 0
const nextMsgId = () => `m_${Date.now()}_${++_msgCounter}`

export default function App() {
  const { state, startRun, amendMessage, approve, reject, reset } = useAgentRun()
  const { sessions, groups, activeSession, newSession, selectSession, deleteSession, renameSession, pinSession, createGroup, updateGroup, deleteGroup, moveSessionToGroup, reorderSessions, addMessage, truncateMessagesFrom, updateChatThreadId } = useSessionManager()
  const { researchJobs, runningCount } = useJobs(true)
  const { docs, upload, remove: removeDoc } = useDocuments(
    activeSession?.id ?? '',
    () => setKgRefreshTrigger(t => t + 1),
  )
  const [composerDocIds, setComposerDocIds] = useState<Set<string>>(() => new Set())
  const [mode, setMode] = useState<Mode>('auto')
  const [selectedDocId, setSelectedDocId] = useState<string | null>(null)
  const [selectedDeck, setSelectedDeck] = useState<{ threadId: string; filename: string; title?: string } | null>(null)
  const [kgPanelOpen, setKgPanelOpen] = useState(false)
  const [rerunToast, setRerunToast] = useState<RerunToastState | null>(null)
  // Increments when a rerun completes so KnowledgePanel can refresh the KG
  // and pick up the new DCF run nodes written during the workflow.
  const [kgRefreshTrigger, setKgRefreshTrigger] = useState(0)

  // App-level KG node feed for the always-mounted notification widget. Lives
  // here (not inside KnowledgePanel) so toasts about KG writes — document fact
  // extraction, filings, DCF runs — surface even while the KG panel is CLOSED
  // (the common case: user uploads from the chat composer). Refetches whenever
  // kgRefreshTrigger bumps (rerun complete, or a document finishes ingest →
  // onDocReady), which is exactly when new nodes have been written.
  const [kgNotifNodes, setKgNotifNodes] = useState<KgNode[]>([])
  useEffect(() => {
    const sid = activeSession?.id
    if (!sid) return
    let cancelled = false
    fetch(`/kg/${encodeURIComponent(sid)}`)
      .then(r => (r.ok ? r.json() : null))
      .then(d => { if (!cancelled && d) setKgNotifNodes(d.nodes ?? []) })
      .catch(() => { /* offline */ })
    return () => { cancelled = true }
  }, [activeSession?.id, kgRefreshTrigger])

  // Auto-clear selection when doc disappears or session changes
  useEffect(() => {
    if (!selectedDocId) return
    if (!docs.some(d => d.doc_id === selectedDocId)) setSelectedDocId(null)
  }, [docs, selectedDocId])

  const handleOpenDeckPreview = useCallback((filename: string, title: string | undefined, threadId: string) => {
    if (!threadId) return
    setSelectedDocId(null)
    setSelectedDeck({ threadId, filename, title })
  }, [])

  /**
   * Amend a previously-sent user message. Truncates the session locally from
   * the edited index forward (the old assistant reply becomes stale) and asks
   * the backend to fork the LangGraph thread at the pre-message checkpoint
   * and re-run with the new content.
   */
  const handleAmendMessage = useCallback(
    (messageIndex: number, originalContent: string, newContent: string) => {
      if (!activeSession) return
      const threadId = activeSession.chatThreadId
      // Local truncate first so the UI feels instant.
      truncateMessagesFrom(activeSession.id, messageIndex)
      // Allow the new run's `complete` event to commit a fresh assistant turn.
      committedRef.current.delete(threadId)
      runTargetSessionIdRef.current = activeSession.id
      void amendMessage(threadId, originalContent, newContent, mode, activeSession.id)
    },
    [activeSession, amendMessage, mode, truncateMessagesFrom],
  )

  const handleCloseDeckPreview = useCallback(() => {
    setSelectedDeck(null)
  }, [])

  // Track which thread_ids have already been committed to a session
  const committedRef = useRef<Set<string>>(new Set())
  // Per-run target session override. Set when a rerun fires against a session
  // that isn't currently active (e.g. "new chat" target from KG modal). The
  // commit hook below reads this to route the assistant message to the
  // intended session rather than the (potentially-stale) activeSession.
  const runTargetSessionIdRef = useRef<string | null>(null)

  // When a run completes, commit its output to the target session and reset the run
  useEffect(() => {
    if (state.status !== 'complete' || !state.thread_id) return
    if (committedRef.current.has(state.thread_id)) return
    committedRef.current.add(state.thread_id)

    const targetSessionId = runTargetSessionIdRef.current ?? activeSession?.id
    if (!targetSessionId) return
    const targetSession = sessions.find(s => s.id === targetSessionId) ?? activeSession
    if (!targetSession) return

    // Scan workflow activity for DCF validity. The convergence_gate writes
    // model_validity into the terminal workflow activity's meta; we capture
    // it here so the persisted message can render a degraded banner.
    const workflowEntry = state.activity.find(
      a => a.kind === 'workflow' && a.meta && typeof a.meta === 'object',
    )
    const wfMeta = (workflowEntry?.meta ?? {}) as Record<string, unknown>
    const validity = (wfMeta.model_validity as 'valid' | 'invalid' | 'adjusting' | undefined)
    const invalidationReason = typeof wfMeta.invalidation_reason === 'string'
      ? wfMeta.invalidation_reason : undefined

    if (state.report) {
      addMessage(targetSession.id, {
        id: nextMsgId(),
        type: 'research_report',
        content: state.report,
        threadId: state.thread_id,
        artifactPaths: state.artifact_paths,
        researchSteps: state.steps.length ? state.steps : undefined,
        activity: state.activity.length ? state.activity : undefined,
        validity,
        invalidationReason,
      })
    } else {
      const lastAssistant = [...state.chat_messages].reverse().find(m => m.role === 'assistant')
      if (lastAssistant?.content) {
        addMessage(targetSession.id, {
          id: nextMsgId(),
          type: 'chat_response',
          content: lastAssistant.content,
          threadId: state.thread_id,
          artifactPaths: state.artifact_paths.length ? state.artifact_paths : undefined,
          activity: state.activity.length ? state.activity : undefined,
          dcfEvidenceItems: state.dcf_evidence_items?.length ? state.dcf_evidence_items : undefined,
          dcfCitationMap: state.dcf_citation_map && Object.keys(state.dcf_citation_map).length ? state.dcf_citation_map : undefined,
          validity,
          invalidationReason,
        })
      }
      // Sync session's chatThreadId to the actual LangGraph thread used so
      // subsequent turns (including auto-mode) continue the same checkpoint.
      if (state.thread_id && state.thread_id !== targetSession.chatThreadId) {
        updateChatThreadId(targetSession.id, state.thread_id)
      }
    }

    // Mark the toast complete (if any) before clearing the target ref so the
    // toast knows which thread completed.
    setRerunToast(prev => prev && prev.threadId === state.thread_id
      ? { ...prev, status: 'complete' } : prev)
    runTargetSessionIdRef.current = null

    // Nudge KnowledgePanel to refresh so the new DCF run nodes (written to
    // the target session during the rerun) appear in the KG graph immediately.
    setKgRefreshTrigger(t => t + 1)

    // Small delay so the final token renders before we flip back to idle
    const tid = setTimeout(reset, 150)
    return () => clearTimeout(tid)
  }, [state.status, state.thread_id]) // eslint-disable-line react-hooks/exhaustive-deps

  // Surface backend errors on the rerun toast too
  useEffect(() => {
    if (state.status !== 'error' || !state.thread_id) return
    setRerunToast(prev => prev && prev.threadId === state.thread_id
      ? { ...prev, status: 'error', error: state.error ?? 'error' } : prev)
  }, [state.status, state.thread_id, state.error])

  // Stage uploaded docs in the composer until the user sends a message.
  useEffect(() => {
    setComposerDocIds(new Set(docs.map(d => d.doc_id)))
  }, [activeSession?.id])

  const composerDocs = useMemo(
    () => docs.filter(d => composerDocIds.has(d.doc_id)),
    [docs, composerDocIds],
  )

  const handleUpload = useCallback(async (file: File) => {
    const info = await upload(file)
    if (info) {
      setComposerDocIds(prev => new Set([...prev, info.doc_id]))
    }
  }, [upload])

  const handleRemoveComposerDoc = useCallback(async (docId: string) => {
    setComposerDocIds(prev => {
      const next = new Set(prev)
      next.delete(docId)
      return next
    })
    await removeDoc(docId)
    if (selectedDocId === docId) setSelectedDocId(null)
  }, [removeDoc, selectedDocId])

  const handleSubmit = useCallback(
    (query: string, selectedMode: Mode) => {
      if (!activeSession) return

      if (!query.startsWith('[DCF_APPROVED]')) {
        committedRef.current.clear()
      }

      const attached = composerDocs.filter(d => d.status !== 'error')

      if (!query.startsWith('[DCF_APPROVED]')) {
        addMessage(activeSession.id, {
          id: nextMsgId(),
          type: 'user',
          content: query,
          attachedDocs: attached.length
            ? attached.map(d => ({
                doc_id: d.doc_id,
                filename: d.filename,
                status: d.status,
                page_count: d.page_count,
              }))
            : undefined,
        })
        if (attached.length) {
          setComposerDocIds(prev => {
            const next = new Set(prev)
            attached.forEach(d => next.delete(d.doc_id))
            return next
          })
        }
      }

      const resolvedIsChat =
        selectedMode === 'chat' ||
        (selectedMode === 'auto' && state.resolved_intent === 'chat')

      startRun(
        query,
        selectedMode,
        resolvedIsChat ? activeSession.chatThreadId : undefined,
        activeSession.id,
      )
    },
    [activeSession, state.resolved_intent, startRun, addMessage, composerDocs],
  )

  const handleNewSession = useCallback(() => {
    if (!['idle', 'complete', 'error', 'rejected'].includes(state.status)) return
    newSession()
  }, [state.status, newSession])

  const handleSelectJob = useCallback(async (job: JobSummary) => {
    if (job.status !== 'complete') return
    try {
      const res = await fetch(`/runs/${job.thread_id}/report`)
      if (res.ok) {
        const data = (await res.json()) as { content: string }
        const blob = new Blob([data.content], { type: 'text/markdown' })
        window.open(URL.createObjectURL(blob), '_blank')
      }
    } catch { /* ignore */ }
  }, [])

  const isRunActive = !['idle', 'complete', 'error', 'rejected'].includes(state.status)

  // Execution panel: show ONLY for workflow runs (DCF, deck, research plan-then-execute).
  // Chat-only runs (web search, ReAct) should NOT trigger the sidebar — the activity
  // trace is already embedded inline in the chat thread.
  const hasWorkflowActivity = state.activity.some(
    (a: any) => a?.kind === 'workflow' || a?.name?.startsWith('workflow:')
  )
  const showExecutionPanel = isRunActive && (
    hasWorkflowActivity ||
    state.dcf_review != null ||
    state.deck_review != null
  )

  const selectedDoc = docs.find(d => d.doc_id === selectedDocId) ?? null
  // Priority: doc preview > execution sidebar.  KG now opens as a full-screen
  // modal (rendered below at z-50) so it doesn't compete for sidebar space.
  const rightPanel: 'doc' | 'execution' | 'deck' | null =
    selectedDeck ? 'deck' : selectedDoc ? 'doc' : showExecutionPanel ? 'execution' : null
  const rightPanelOpen = rightPanel !== null
  const rightPanelStorageKey =
    rightPanel === 'deck'
      ? 'ui.rightPanel.deck'
      : rightPanel === 'doc'
        ? 'ui.rightPanel.doc'
        : 'ui.rightPanel.execution'
  const rightPanelDefaultWidth = rightPanel === 'execution' ? 360 : 520
  const rightPanelRevealLabel =
    rightPanel === 'execution' ? 'Trace' : rightPanel === 'deck' ? 'Deck' : 'Doc'

  const sessionsPanel = usePanelHidden('ui.panel.sessions.hidden')
  const executionPanel = usePanelHidden('ui.rightPanel.execution.hidden')
  const docPanel = usePanelHidden('ui.rightPanel.doc.hidden')
  const deckPanel = usePanelHidden('ui.rightPanel.deck.hidden')
  const rightPanelVisibility =
    rightPanel === 'deck' ? deckPanel : rightPanel === 'doc' ? docPanel : executionPanel

  return (
    <div className="h-screen bg-bg text-ink flex overflow-hidden">

      {/* ── Left: Sessions sidebar ─────────────────────────────── */}
      <SessionsSidebar
        sessions={sessions}
        groups={groups}
        activeId={activeSession?.id ?? ''}
        onSelect={selectSession}
        onNew={handleNewSession}
        onDelete={deleteSession}
        onRename={renameSession}
        onPin={pinSession}
        onCreateGroup={() => createGroup()}
        onUpdateGroup={updateGroup}
        onDeleteGroup={deleteGroup}
        onMoveToGroup={moveSessionToGroup}
        onReorderSessions={reorderSessions}
        hidden={sessionsPanel.hidden}
        onHide={sessionsPanel.hide}
        onReveal={sessionsPanel.show}
        disabled={isRunActive}
      />

      {/* ── Center: Message thread ─────────────────────────────── */}
      <MessageThread
        session={activeSession}
        activeRun={state}
        mode={mode}
        onModeChange={setMode}
        onSubmit={handleSubmit}
        onUpload={handleUpload}
        docs={composerDocs}
        selectedDocId={selectedDocId}
        onSelectDoc={(id) => {
          setSelectedDeck(null)
          setSelectedDocId(id === selectedDocId ? null : id)
        }}
        onRemoveDoc={handleRemoveComposerDoc}
        disabled={false}
        onOpenDeckPreview={handleOpenDeckPreview}
        onAmendMessage={handleAmendMessage}
      />

      {/* ── Right: Doc preview OR Execution panel ───────────────── */}
      {rightPanelOpen && rightPanel && (
        <ResizablePanel
          key={rightPanelStorageKey}
          defaultWidth={rightPanelDefaultWidth}
          minWidth={280}
          maxWidth={900}
          side="left"
          storageKey={rightPanelStorageKey}
          className="border-l border-border-subtle bg-bg"
          hidden={rightPanelVisibility.hidden}
          onReveal={rightPanelVisibility.show}
          revealLabel={rightPanelRevealLabel}
        >
          {rightPanel === 'doc' && selectedDoc && (
            <DocumentPreview doc={selectedDoc} onClose={() => setSelectedDocId(null)} onHide={rightPanelVisibility.hide} />
          )}
          {rightPanel === 'deck' && selectedDeck && (
            <DeckPreview
              threadId={selectedDeck.threadId}
              filename={selectedDeck.filename}
              title={selectedDeck.title}
              onClose={handleCloseDeckPreview}
              onHide={rightPanelVisibility.hide}
            />
          )}
          {rightPanel === 'execution' && (
            <ExecutionSidebar
              status={state.status}
              steps={state.steps}
              completedSteps={state.completed_steps}
              error={state.error}
              activity={state.activity}
              dcfReview={state.dcf_review ?? undefined}
              deckReview={state.deck_review ?? undefined}
              threadId={state.thread_id}
              onApprove={approve}
              onReject={reject}
              onHide={rightPanelVisibility.hide}
            />
          )}
        </ResizablePanel>
      )}

      {/* ── Jobs panel ─────────────────────────────────────────── */}
      <JobsPanel
        jobs={researchJobs}
        runningCount={runningCount}
        onSelectJob={handleSelectJob}
      />

      {/* ── KB toggle (floating, bottom-right) ─────────────────── */}
      <button
        type="button"
        onClick={() => setKgPanelOpen(o => !o)}
        aria-pressed={kgPanelOpen}
        className={`
          fixed bottom-4 right-4 z-40
          flex items-center px-3.5 py-2 rounded-xl
          text-[11px] font-medium tracking-[0.06em] uppercase
          border shadow-lg shadow-black/40
          transition-colors duration-150
          ${kgPanelOpen
            ? 'bg-bg-overlay text-ink border-accent-ring/35 hover:border-accent-ring/50'
            : 'bg-bg-overlay text-ink-dim border-border-hover hover:border-border-hover hover:text-ink-muted'
          }
        `}
        title={kgPanelOpen ? 'Close knowledge graph' : 'Open knowledge graph'}
      >
        {kgPanelOpen ? 'Close' : 'Knowledge'}
      </button>

      {/* ── Settings button (bottom-left) ──── */}
      <SettingsButton />

      {/* ── KB full-screen modal ──────────────────────────────── */}
      {kgPanelOpen && (
        <KnowledgePanel
          sessionId={activeSession?.id ?? null}
          onClose={() => setKgPanelOpen(false)}
          activeSessionTitle={activeSession?.title ?? '(unnamed)'}
          activeChatThreadId={activeSession?.chatThreadId}
          refreshTrigger={kgRefreshTrigger}
          onCreateNewSession={() => {
            // Do NOT auto-activate. KG stays attached to the session you're
            // viewing so it doesn't flash empty mid-rerun. Toast's
            // "View chat →" performs the switch after the rerun completes.
            const s = newSession({ activate: false })
            return { id: s.id, chatThreadId: s.chatThreadId }
          }}
          isRunActive={isRunActive}
          onStartRerun={async ({ ticker, sessionId, chatThreadId, target, query, diffText }) => {
            // Record where the assistant response should land — the commit
            // hook reads this ref so reruns targeting a non-active session
            // don't leak their reply into the wrong chat.
            runTargetSessionIdRef.current = sessionId || activeSession?.id || null

            // Append the diff message to the target session immediately so
            // the chat history shows what was changed even before the run
            // produces tokens. addMessage resolves session inside setState,
            // so brand-new sessions work even pre-render.
            if (sessionId) {
              addMessage(sessionId, { id: nextMsgId(), type: 'user', content: diffText })
            }

            // "New chat" must actually open a new chat. The live run stream is
            // global (activeRun), so without switching the view the rerun would
            // appear in whatever chat is on screen — making "new" and "current"
            // indistinguishable. Switch only AFTER the diff message exists so the
            // new session never renders empty mid-rerun.
            if (target === 'new' && sessionId) {
              selectSession(sessionId)
            }

            // Optimistic toast — threadId filled in once /runs returns.
            setRerunToast({
              id: `pending_${Date.now()}`,
              ticker, threadId: null, sessionId, target,
              status: 'running',
              createdAt: Date.now(),
            })

            const threadId = await startRun(query, 'chat', chatThreadId, sessionId)
            setRerunToast(prev => prev ? { ...prev, threadId } : prev)
            return threadId
          }}
        />
      )}

      {/* ── Rerun toast (bottom-right, above KB toggle) ───────── */}
      {rerunToast && (
        <RerunToast
          toast={rerunToast}
          onView={() => {
            selectSession(rerunToast.sessionId)
            setKgPanelOpen(false)
            setRerunToast(null)
          }}
          onInspect={() => setKgPanelOpen(false)}
          onDismiss={() => setRerunToast(null)}
        />
      )}

      {/* ── KG write notifications (always mounted, even with KG panel closed) ── */}
      <KgNotificationPanel nodes={kgNotifNodes} />
    </div>
  )
}
