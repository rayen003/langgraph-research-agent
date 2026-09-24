import { useState, useEffect, useRef, useCallback, useMemo } from 'react'
import { useAgentRun } from './hooks/useAgentRun'
import { useSessionManager } from './hooks/useSessionManager'
import { useJobs } from './hooks/useJobs'
import { useDocuments } from './hooks/useDocuments'
import { useWorkspaceObjects } from './hooks/useWorkspaceObjects'
import { useCollaboration } from './hooks/useCollaboration'
import { SessionsSidebar } from './components/SessionsSidebar'
import { MobileWorkspaceBar, WorkspaceRail } from './components/WorkspaceRail'
import { ChannelView } from './components/ChannelView'
import { TaskView } from './components/TaskView'
import { CreateTaskDialog } from './components/CreateTaskDialog'
import { ListPlus } from 'lucide-react'
import { MessageThread } from './components/MessageThread'
import { DocumentPreview } from './components/DocumentPreview'
import { DeckPreview } from './components/DeckPreview'
import { ObjectInspector } from './components/ObjectInspector'
import { JobsPanel } from './components/JobsPanel'
import { KnowledgePanel } from './components/KnowledgePanel'
import { KgNotificationPanel } from './components/KgNotificationPanel'
import type { KgNode } from './hooks/useKnowledgeGraph'
import { ResizablePanel } from './components/ResizablePanel'
import { usePanelHidden } from './hooks/usePanelHidden'
import { RerunToast, type RerunToastState } from './components/RerunToast'
import { SettingsButton } from './components/SettingsPanel'
import { loadUserSettings } from './lib/userSettings'
import type { CollaborationAssignment, CollaborationChannel, JobSummary, Mode, WorkspaceObject } from './types'

let _msgCounter = 0
const nextMsgId = () => `m_${Date.now()}_${++_msgCounter}`

export default function App() {
  const workspaceId = 'workspace:default'
  const { state, startRun, amendMessage, watchRun, approve, reject, reset, submitWorkflowContext } = useAgentRun()
  const { sessions, groups, activeSession, newSession, selectSession, deleteSession, renameSession, pinSession, createGroup, updateGroup, deleteGroup, moveSessionToGroup, reorderSessions, addMessage, truncateMessagesFrom, updateChatThreadId } = useSessionManager()
  const { researchJobs, runningCount } = useJobs(true)
  const { objects: workspaceObjects } = useWorkspaceObjects(undefined, true)
  const collaboration = useCollaboration(workspaceId)
  const { docs, upload, remove: removeDoc } = useDocuments(
    activeSession?.id ?? '',
    () => setKgRefreshTrigger(t => t + 1),
  )
  const [composerDocIds, setComposerDocIds] = useState<Set<string>>(() => new Set())
  const [mode, setMode] = useState<Mode>('chat')
  const [selectedDocId, setSelectedDocId] = useState<string | null>(null)
  const [selectedDeck, setSelectedDeck] = useState<{ threadId: string; filename: string; title?: string } | null>(null)
  const [selectedObject, setSelectedObject] = useState<WorkspaceObject | null>(null)
  const [selectedChannel, setSelectedChannel] = useState<CollaborationChannel | null>(null)
  const [selectedTask, setSelectedTask] = useState<CollaborationAssignment | null>(null)
  const [taskDialogOpen, setTaskDialogOpen] = useState(false)
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
    setSelectedObject(null)
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

  const handleOpenWorkspaceObject = useCallback((object: WorkspaceObject) => {
    setSelectedDocId(null)
    setSelectedDeck(null)
    setSelectedObject(object)
  }, [])

  const handleSelectChannel = useCallback((channel: CollaborationChannel) => {
    setSelectedTask(null)
    setSelectedChannel(channel)
    setSelectedObject(null)
    setSelectedDocId(null)
    setSelectedDeck(null)
    void collaboration.loadMessages(channel.channel_id)
  }, [collaboration.loadMessages])

  const handleOpenTask = useCallback((task: CollaborationAssignment) => {
    setSelectedTask(task)
    setSelectedObject(null)
    setSelectedDocId(null)
    setSelectedDeck(null)
  }, [])

  useEffect(() => {
    if (!selectedTask) return
    const current = collaboration.assignments.find(task => task.assignment_id === selectedTask.assignment_id)
    if (current && current !== selectedTask) setSelectedTask(current)
  }, [collaboration.assignments, selectedTask])

  useEffect(() => {
    if (!selectedChannel) return
    const stream = new EventSource(`/collaboration/channels/${encodeURIComponent(selectedChannel.channel_id)}/events`)
    stream.onmessage = event => {
      try {
        const payload = JSON.parse(event.data) as { type: string; message?: import('./types').CollaborationMessage }
        if (payload.type === 'collaboration_message' && payload.message) collaboration.ingestMessage(payload.message)
      } catch { /* malformed realtime event */ }
    }
    return () => stream.close()
  }, [selectedChannel, collaboration.ingestMessage])

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
        traceSpans: state.trace_spans.length ? state.trace_spans : undefined,
        executionTrace: state.execution_trace.length ? state.execution_trace : undefined,
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
          traceSpans: state.trace_spans.length ? state.trace_spans : undefined,
          executionTrace: state.execution_trace.length ? state.execution_trace : undefined,
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
  const visibleWorkspaceObjects = useMemo<WorkspaceObject[]>(() => {
    const sessionMessages = activeSession?.messages ?? []
    const messageDocObjects: WorkspaceObject[] = (activeSession?.messages ?? [])
      .flatMap(message => message.attachedDocs ?? [])
      .map(doc => {
        const created = activeSession?.createdAt ?? new Date().toISOString()
        return {
          object_id: `uploaded_document:${doc.doc_id}`,
          object_type: 'uploaded_document',
          title: doc.filename,
          status: doc.status,
          session_id: activeSession?.id ?? null,
          thread_id: null,
          source_message_id: null,
          source_object_ids: [],
          kg_node_ids: [`doc:${doc.doc_id}`],
          artifact_paths: [`/documents/${doc.doc_id}/file`],
          summary: doc.page_count ? `${doc.page_count} pg` : doc.status,
          payload: {
            doc_id: doc.doc_id,
            filename: doc.filename,
            status: doc.status,
            page_count: doc.page_count,
          },
          created_at: created,
          updated_at: created,
        }
      })
    const messageDeckObjects: WorkspaceObject[] = sessionMessages
      .filter(message => message.threadId && message.artifactPaths?.some(path => /\.pptx($|\?)/i.test(path)))
      .map(message => {
        const pptxPath = message.artifactPaths?.find(path => /\.pptx($|\?)/i.test(path)) ?? message.artifactPaths?.[0] ?? ''
        const filename = pptxPath.split('/').pop()?.split('?')[0] ?? 'Presentation deck'
        const titleFromContent = message.content.match(/deck titled\s+"([^"]+)"/i)?.[1]
        const title = titleFromContent || filename.replace(/\.pptx$/i, '').replace(/_/g, ' ')
        const created = activeSession?.createdAt ?? new Date().toISOString()
        const slideMatch = message.content.match(/(\d+)\s+slides?/i)
        return {
          object_id: `deck:${message.threadId}`,
          object_type: 'deck',
          title,
          status: 'complete',
          session_id: activeSession?.id ?? null,
          thread_id: message.threadId ?? null,
          source_message_id: message.id,
          source_object_ids: [],
          kg_node_ids: [],
          artifact_paths: message.artifactPaths ?? [],
          summary: slideMatch ? `${slideMatch[1]} slides` : 'Presentation deck',
          payload: {
            filename,
            artifact_path: pptxPath,
          },
          created_at: created,
          updated_at: created,
        }
      })

    const localDocObjects: WorkspaceObject[] = docs.map(doc => {
      const created = new Date(doc.created_at * 1000).toISOString()
      const summaryParts = [
        doc.status === 'ready'
          ? `${doc.page_count || 0} pg`
          : doc.stage ?? doc.status,
        doc.chunk_count ? `${doc.chunk_count} chunks` : null,
      ].filter(Boolean)
      return {
        object_id: `uploaded_document:${doc.doc_id}`,
        object_type: 'uploaded_document',
        title: doc.filename,
        status: doc.status,
        session_id: doc.session_id,
        thread_id: null,
        source_message_id: null,
        source_object_ids: [],
        kg_node_ids: [`doc:${doc.doc_id}`],
        artifact_paths: [`/documents/${doc.doc_id}/file`],
        summary: summaryParts.join(' · '),
        payload: {
          doc_id: doc.doc_id,
          filename: doc.filename,
          status: doc.status,
          stage: doc.stage,
          page_count: doc.page_count,
          chunk_count: doc.chunk_count,
        },
        created_at: created,
        updated_at: created,
      }
    })

    const byId = new Map<string, WorkspaceObject>()
    workspaceObjects.forEach(obj => byId.set(obj.object_id, obj))
    messageDocObjects.forEach(obj => byId.set(obj.object_id, obj))
    messageDeckObjects.forEach(obj => byId.set(obj.object_id, obj))
    localDocObjects.forEach(obj => byId.set(obj.object_id, obj))
    return Array.from(byId.values()).sort(
      (a, b) => new Date(b.updated_at).getTime() - new Date(a.updated_at).getTime(),
    )
  }, [activeSession?.createdAt, activeSession?.id, activeSession?.messages, docs, workspaceObjects])

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
      if (composerDocs.some(d => d.status !== 'ready')) return

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
        loadUserSettings(),
      )
    },
    [activeSession, state.resolved_intent, startRun, addMessage, composerDocs],
  )

  const handleNewSession = useCallback(() => {
    if (!['idle', 'complete', 'error', 'rejected'].includes(state.status)) return
    newSession()
  }, [state.status, newSession])

  const handleSelectJob = useCallback(async (job: JobSummary) => {
    if (job.status !== 'complete') {
      watchRun(job.thread_id, job.query)
      setSelectedObject(null)
      setSelectedDocId(null)
      setSelectedDeck(null)
      return
    }
    try {
      const res = await fetch(`/runs/${job.thread_id}/report`)
      if (res.ok) {
        const data = (await res.json()) as { content: string }
        const blob = new Blob([data.content], { type: 'text/markdown' })
        window.open(URL.createObjectURL(blob), '_blank')
      }
    } catch { /* ignore */ }
  }, [watchRun])

  const isRunActive = !['idle', 'complete', 'error', 'rejected'].includes(state.status)

  const selectedDoc = docs.find(d => d.doc_id === selectedDocId) ?? null
  const rightPanel: 'doc' | 'deck' | 'object' | null =
    selectedObject ? 'object' : selectedDeck ? 'deck' : selectedDoc ? 'doc' : null
  const rightPanelOpen = rightPanel !== null
  const rightPanelStorageKey =
    rightPanel === 'deck'
      ? 'ui.rightPanel.deck'
      : rightPanel === 'object'
        ? 'ui.rightPanel.object'
      : rightPanel === 'doc'
        ? 'ui.rightPanel.doc'
        : 'ui.rightPanel.doc'
  const rightPanelDefaultWidth = 520
  const rightPanelRevealLabel =
    rightPanel === 'deck' ? 'Deck' : rightPanel === 'object' ? 'Object' : 'Doc'

  const sessionsPanel = usePanelHidden('ui.panel.sessions.hidden')
  const docPanel = usePanelHidden('ui.rightPanel.doc.hidden')
  const deckPanel = usePanelHidden('ui.rightPanel.deck.hidden')
  const objectPanel = usePanelHidden('ui.rightPanel.object.hidden')
  const rightPanelVisibility =
    rightPanel === 'deck' ? deckPanel : rightPanel === 'object' ? objectPanel : docPanel

  return (
    <div className="h-screen bg-bg text-ink flex overflow-hidden">
      <button type="button" title="Create task" aria-label="Create task" onClick={() => setTaskDialogOpen(true)} className="fixed right-5 top-3 z-40 flex h-9 w-9 items-center justify-center rounded border border-border bg-bg-raised text-ink"><ListPlus size={17} /></button>
      {taskDialogOpen && <CreateTaskDialog workspaceId={workspaceId} channelId={selectedChannel?.channel_id} actors={collaboration.actors} objects={visibleWorkspaceObjects} onClose={() => setTaskDialogOpen(false)} onCreated={task => { setTaskDialogOpen(false); handleOpenTask(task); void collaboration.refresh() }} />}

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

      <WorkspaceRail
        channels={collaboration.channels}
        actors={collaboration.actors}
        objects={visibleWorkspaceObjects}
        notifications={collaboration.notifications}
        assignments={collaboration.assignments}
        activeChannelId={selectedChannel?.channel_id ?? null}
        activeTaskId={selectedTask?.assignment_id ?? null}
        onSelectChannel={handleSelectChannel}
        onOpenTask={handleOpenTask}
        onOpenObject={handleOpenWorkspaceObject}
        onCreateChannel={(name) => { void collaboration.createChannel(name).then(channel => { if (channel) handleSelectChannel(channel) }) }}
        onStartDirect={(actor) => {
          const existing = collaboration.channels.find(channel => channel.kind === 'direct' && channel.name === actor.handle)
          if (existing) { handleSelectChannel(existing); return }
          void collaboration.createChannel(actor.handle, 'direct').then(channel => { if (channel) handleSelectChannel(channel) })
        }}
        onOpenNotification={(notification) => { void collaboration.markRead(notification.notification_id) }}
        onUpdateAssignment={(assignmentId, status) => { void collaboration.updateAssignment(assignmentId, status) }}
      />
      <MobileWorkspaceBar
        channels={collaboration.channels}
        objects={visibleWorkspaceObjects}
        notifications={collaboration.notifications}
        activeChannelId={selectedChannel?.channel_id ?? null}
        onSelectChannel={handleSelectChannel}
        onOpenObject={handleOpenWorkspaceObject}
        onOpenNotification={(notification) => { void collaboration.markRead(notification.notification_id) }}
      />

      {/* ── Center: Message thread ─────────────────────────────── */}
      {selectedTask ? <TaskView
        task={selectedTask}
        actors={collaboration.actors}
        objects={visibleWorkspaceObjects}
        onBack={() => setSelectedTask(null)}
        onOpenChannel={() => {
          const channel = collaboration.channels.find(item => item.channel_id === selectedTask.channel_id)
          if (channel) handleSelectChannel(channel)
        }}
        onOpenObject={handleOpenWorkspaceObject}
        onUpdateStatus={(status) => { void collaboration.updateAssignment(selectedTask.assignment_id, status) }}
      /> : selectedChannel ? <ChannelView
        channel={selectedChannel}
        actors={collaboration.actors}
        messages={collaboration.messages[selectedChannel.channel_id] ?? []}
        assignments={collaboration.assignments}
        objects={visibleWorkspaceObjects}
        onSend={(body, mentions, objectVersionIds) => { void collaboration.sendMessage(selectedChannel.channel_id, body, mentions, objectVersionIds) }}
        onOpenTask={handleOpenTask}
        onClose={() => setSelectedChannel(null)}
      /> : <MessageThread
        session={activeSession}
        activeRun={state}
        mode={mode}
        onModeChange={setMode}
        onSubmit={handleSubmit}
        onUpload={handleUpload}
        docs={composerDocs}
        workspaceObjects={visibleWorkspaceObjects}
        selectedDocId={selectedDocId}
        onSelectDoc={(id) => {
          setSelectedDeck(null)
          setSelectedObject(null)
          setSelectedDocId(id === selectedDocId ? null : id)
        }}
        onRemoveDoc={handleRemoveComposerDoc}
        disabled={false}
        onOpenDeckPreview={handleOpenDeckPreview}
        onAmendMessage={handleAmendMessage}
        onWorkflowContextSubmit={submitWorkflowContext}
        onApprovePlan={approve}
        onRejectPlan={reject}
      />}

      {/* ── Right: object and artifact inspectors ───────────────── */}
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
          {rightPanel === 'object' && selectedObject && (
            <ObjectInspector
              object={selectedObject}
              workspaceId={workspaceId}
              onClose={() => setSelectedObject(null)}
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
