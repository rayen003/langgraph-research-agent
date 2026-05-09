import { useState, useEffect, useRef, useCallback } from 'react'
import { useAgentRun } from './hooks/useAgentRun'
import { useSessionManager } from './hooks/useSessionManager'
import { useJobs } from './hooks/useJobs'
import { useDocuments } from './hooks/useDocuments'
import { SessionsSidebar } from './components/SessionsSidebar'
import { MessageThread } from './components/MessageThread'
import { ExecutionSidebar } from './components/ExecutionSidebar'
import { DocumentPreview } from './components/DocumentPreview'
import { JobsPanel } from './components/JobsPanel'
import type { JobSummary, Mode } from './types'

let _msgCounter = 0
const nextMsgId = () => `m_${Date.now()}_${++_msgCounter}`

export default function App() {
  const { state, startRun, approve, reject, reset } = useAgentRun()
  const { sessions, activeSession, newSession, selectSession, deleteSession, addMessage } = useSessionManager()
  const { researchJobs, runningCount } = useJobs(true)
  const { docs, upload, remove: removeDoc } = useDocuments(activeSession?.id ?? '')
  const [mode, setMode] = useState<Mode>('auto')
  const [selectedDocId, setSelectedDocId] = useState<string | null>(null)

  // Auto-clear selection when doc disappears or session changes
  useEffect(() => {
    if (!selectedDocId) return
    if (!docs.some(d => d.doc_id === selectedDocId)) setSelectedDocId(null)
  }, [docs, selectedDocId])

  // Track which thread_ids have already been committed to a session
  const committedRef = useRef<Set<string>>(new Set())

  // When a run completes, commit its output to the active session and reset the run
  useEffect(() => {
    if (state.status !== 'complete' || !state.thread_id) return
    if (committedRef.current.has(state.thread_id)) return
    committedRef.current.add(state.thread_id)

    if (!activeSession) return

    if (state.report) {
      addMessage(activeSession.id, {
        id: nextMsgId(),
        type: 'research_report',
        content: state.report,
        threadId: state.thread_id,
        artifactPaths: state.artifact_paths,
        researchSteps: state.steps.length ? state.steps : undefined,
        activity: state.activity.length ? state.activity : undefined,
      })
    } else {
      const lastAssistant = [...state.chat_messages].reverse().find(m => m.role === 'assistant')
      if (lastAssistant?.content) {
        addMessage(activeSession.id, {
          id: nextMsgId(),
          type: 'chat_response',
          content: lastAssistant.content,
          activity: state.activity.length ? state.activity : undefined,
        })
      }
    }

    // Small delay so the final token renders before we flip back to idle
    const tid = setTimeout(reset, 150)
    return () => clearTimeout(tid)
  }, [state.status, state.thread_id]) // eslint-disable-line react-hooks/exhaustive-deps

  const handleSubmit = useCallback(
    (query: string, selectedMode: Mode) => {
      if (!activeSession) return

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
  // Document preview takes priority over execution sidebar when both could show
  const rightPanel: 'doc' | 'execution' | null =
    selectedDoc ? 'doc' : showExecutionPanel ? 'execution' : null
  const rightPanelOpen = rightPanel !== null
  const rightPanelWidth = rightPanel === 'doc' ? 520 : 360

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
        onSelectDoc={(id) => setSelectedDocId(id === selectedDocId ? null : id)}
        onRemoveDoc={removeDoc}
        disabled={false}
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
          {rightPanel === 'execution' && (
            <ExecutionSidebar
              status={state.status}
              steps={state.steps}
              completedSteps={state.completed_steps}
              error={state.error}
              activity={state.activity}
              dcfReview={state.dcf_review ?? undefined}
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
    </div>
  )
}
