import { useState, useEffect, useRef, useCallback } from 'react'
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
import { RerunToast, type RerunToastState } from './components/RerunToast'
import type { JobSummary, Mode } from './types'

let _msgCounter = 0
const nextMsgId = () => `m_${Date.now()}_${++_msgCounter}`

export default function App() {
  const { state, startRun, amendMessage, approve, reject, reset } = useAgentRun()
  const { sessions, activeSession, newSession, selectSession, deleteSession, addMessage, truncateMessagesFrom, updateChatThreadId } = useSessionManager()
  const { researchJobs, runningCount } = useJobs(true)
  const { docs, upload, remove: removeDoc } = useDocuments(activeSession?.id ?? '')
  const [mode, setMode] = useState<Mode>('auto')
  const [selectedDocId, setSelectedDocId] = useState<string | null>(null)
  const [selectedDeck, setSelectedDeck] = useState<{ threadId: string; filename: string; title?: string } | null>(null)
  const [kgPanelOpen, setKgPanelOpen] = useState(false)
  const [rerunToast, setRerunToast] = useState<RerunToastState | null>(null)
  // Increments when a rerun completes so KnowledgePanel can refresh the KG
  // and pick up the new DCF run nodes written during the workflow.
  const [kgRefreshTrigger, setKgRefreshTrigger] = useState(0)

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

  const handleSubmit = useCallback(
    (query: string, selectedMode: Mode) => {
      if (!activeSession) return

      // New turn → allow a fresh commit even on a reused thread_id. Without
      // this, the second+ message in a chat thread (same thread_id, memory
      // continuity) hits the committedRef guard from the first run and never
      // gets its assistant response added to the session.
      if (!query.startsWith('[DCF_APPROVED]')) {
        committedRef.current.clear()
      }

      // Add user message to the active session (skip internal approval triggers)
      if (!query.startsWith('[DCF_APPROVED]')) {
        addMessage(activeSession.id, { id: nextMsgId(), type: 'user', content: query })
      }

      // Chat queries reuse the session's dedicated chatThreadId for multi-turn context
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
    [activeSession, state.resolved_intent, startRun, addMessage],
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

  // Execution panel: show for ANY active non-idle run (research or chat)
  const showExecutionPanel = isRunActive

  const selectedDoc = docs.find(d => d.doc_id === selectedDocId) ?? null
  // Priority: doc preview > execution sidebar.  KG now opens as a full-screen
  // modal (rendered below at z-50) so it doesn't compete for sidebar space.
  const rightPanel: 'doc' | 'execution' | 'deck' | null =
    selectedDeck ? 'deck' : selectedDoc ? 'doc' : showExecutionPanel ? 'execution' : null
  const rightPanelOpen = rightPanel !== null
  const rightPanelWidth =
    rightPanel === 'doc' || rightPanel === 'deck' ? 520 : 360

  return (
    <div className="h-screen bg-[#0a0a0a] text-zinc-100 flex overflow-hidden">

      {/* ── Left: Sessions sidebar ─────────────────────────────── */}
      <SessionsSidebar
        sessions={sessions}
        activeId={activeSession?.id ?? ''}
        onSelect={selectSession}
        onNew={handleNewSession}
        onDelete={deleteSession}
        disabled={isRunActive}
      />

      {/* ── Center: Message thread ─────────────────────────────── */}
      <MessageThread
        session={activeSession}
        activeRun={state}
        mode={mode}
        onModeChange={setMode}
        onSubmit={handleSubmit}
        onUpload={upload}
        docs={docs}
        selectedDocId={selectedDocId}
        onSelectDoc={(id) => {
          setSelectedDeck(null)
          setSelectedDocId(id === selectedDocId ? null : id)
        }}
        onRemoveDoc={removeDoc}
        disabled={false}
        onOpenDeckPreview={handleOpenDeckPreview}
        onAmendMessage={handleAmendMessage}
      />

      {/* ── Right: Doc preview OR Execution panel (slides in) ───── */}
      <div
        style={{ width: rightPanelOpen ? `${rightPanelWidth}px` : '0' }}
        className="flex-shrink-0 overflow-hidden transition-[width] duration-300 ease-in-out"
      >
        <div style={{ width: `${rightPanelWidth}px` }} className="h-full">
          {rightPanel === 'doc' && selectedDoc && (
            <DocumentPreview doc={selectedDoc} onClose={() => setSelectedDocId(null)} />
          )}
          {rightPanel === 'deck' && selectedDeck && (
            <DeckPreview
              threadId={selectedDeck.threadId}
              filename={selectedDeck.filename}
              title={selectedDeck.title}
              onClose={handleCloseDeckPreview}
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
            />
          )}
        </div>
      </div>

      {/* ── Jobs panel ─────────────────────────────────────────── */}
      <JobsPanel
        jobs={researchJobs}
        runningCount={runningCount}
        onSelectJob={handleSelectJob}
      />

      {/* ── KB toggle (floating, bottom-right) ─────────────────── */}
      <button
        onClick={() => setKgPanelOpen(o => !o)}
        className={`fixed bottom-4 right-4 z-40 px-3 py-2 rounded-full text-[12px] border shadow-lg transition ${
          kgPanelOpen
            ? 'bg-teal-500/20 text-teal-300 border-teal-500/40 hover:bg-teal-500/30'
            : 'bg-zinc-900 text-zinc-400 border-zinc-700 hover:bg-zinc-800 hover:text-zinc-200'
        }`}
        title="Toggle Knowledge Graph"
      >
        🧠 {kgPanelOpen ? 'Close KB' : 'Knowledge Base'}
      </button>

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

      {/* ── Rerun toast (bottom-right, above the 🧠 button) ───── */}
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
    </div>
  )
}
