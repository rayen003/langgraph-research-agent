import { useEffect, useRef, useState } from 'react'
import { MarkdownRenderer } from './MarkdownRenderer'
import { QueryInput } from './QueryInput'
import { ActivityTrace, ResearchStepsTrace } from './ActivityTrace'
import type { ActivityEntry } from '../lib/activity'
import type { AgentRunState, DocumentInfo, DcfReviewState, Mode, Session, SessionMessage, StepState, ToolCall } from '../types'

const IMAGE_RE = /\.(png|jpg|jpeg|webp|gif|svg)$/i
const ARTIFACT_MARKER_RE = /\[ARTIFACTS?\]|\[CHART\]/i

function splitOnMarker(text: string): [string, string] {
  const match = ARTIFACT_MARKER_RE.exec(text)
  if (!match) return [text, '']
  return [text.slice(0, match.index).trimEnd(), text.slice(match.index + match[0].length).trimStart()]
}

// ── Individual message renderers ─────────────────────────────────────────────

function UserBubble({ content }: { content: string }) {
  return (
    <div className="flex justify-end animate-fade-up">
      <div className="max-w-[72%] px-4 py-2.5 rounded-2xl rounded-tr-sm bg-[#1a1a24] border border-[#252535]">
        <p className="text-sm text-zinc-200 leading-relaxed whitespace-pre-wrap">{content}</p>
      </div>
    </div>
  )
}

function AgentLabel() {
  return (
    <div className="flex items-center gap-1.5 mb-1.5">
      <div className="w-4 h-4 rounded-md bg-indigo-500/20 border border-indigo-500/30 flex items-center justify-center flex-shrink-0">
        <svg width="8" height="8" viewBox="0 0 12 12" fill="none">
          <path d="M2 9L5 3L8 7L10 4" stroke="#818cf8" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      </div>
      <span className="text-[11px] text-zinc-600 font-medium">Agent</span>
    </div>
  )
}

function ChatBubble({
  content,
  streaming,
  toolCalls,
  activities,
  persisted,
  dcfReview,
  onDcfApprove,
  onDcfReject,
  threadId,
  hideLabel,
}: {
  content: string
  streaming?: boolean
  /** Legacy ToolCall list (committed messages, pre-activity contract). */
  toolCalls?: ToolCall[]
  /** Unified activity log scoped to chat — preferred when provided. */
  activities?: ActivityEntry[]
  /** True when rendering a committed message (read-only). */
  persisted?: boolean
  dcfReview?: DcfReviewState
  onDcfApprove?: (overrides?: Record<string, number>) => void
  onDcfReject?: () => void
  threadId?: string
  hideLabel?: boolean
}) {
  const useUnified = !!(activities && activities.length)
  const calls = toolCalls ?? []
  const hasContent = !!content
  const hasRunning = useUnified
    ? activities!.some(a => a.status === 'started' || a.status === 'running')
    : calls.some(t => t.status === 'running')
  const hasAnyActivity = useUnified ? activities!.length > 0 : calls.length > 0

  // Activity defaults: open while we're still working (no content yet),
  // collapsed once the assistant message is present so the response stays
  // front-and-center but the audit trail remains one click away.
  const defaultOpen = !hasContent && !persisted

  return (
    <div className="flex justify-start animate-fade-up">
      <div className="max-w-[85%] min-w-0 w-full">
        {!hideLabel && <AgentLabel />}
        <div className="pl-1 space-y-2">
          {hasAnyActivity && (
            <ActivityTrace
              toolCalls={useUnified ? undefined : calls}
              activities={useUnified ? activities : undefined}
              scope={useUnified ? 'chat' : undefined}
              defaultOpen={defaultOpen || !!dcfReview}
              dcfReview={dcfReview}
              onDcfApprove={onDcfApprove}
              onDcfReject={onDcfReject}
              threadId={threadId}
            />
          )}

          {hasContent ? (
            <MarkdownRenderer content={content} streaming={streaming} />
          ) : !hasRunning && !persisted ? (
            <ThinkingDots />
          ) : null}
        </div>
      </div>
    </div>
  )
}

function ResearchReportCard({
  content,
  threadId,
  artifactPaths,
  streaming,
  steps,
  activity,
}: {
  content: string
  threadId?: string
  artifactPaths?: string[]
  streaming?: boolean
  /** Persisted research-step snapshot. When provided, an Activity bar is
   *  rendered above the report so the audit trail survives after commit. */
  steps?: StepState[]
  /** Unified activity snapshot (preferred when present). */
  activity?: ActivityEntry[]
}) {
  const hasArtifacts = !streaming && (artifactPaths?.length ?? 0) > 0 && !!threadId
  const markerPresent = ARTIFACT_MARKER_RE.test(content)
  const [before, after] = hasArtifacts && markerPresent ? splitOnMarker(content) : [content, '']

  // Keep the research plan timeline as the primary audit surface. Activity is
  // useful detail, but it should not replace the step descriptions/messages.
  const safeActivity = Array.isArray(activity) ? activity : []
  const safeSteps = Array.isArray(steps) ? steps : []
  const researchActivity = safeActivity.filter(
    a => a.scope === 'research' || a.scope === 'workflow',
  )

  return (
    <div className="flex justify-start animate-fade-up w-full">
      <div className="w-full min-w-0 space-y-2">
        <AgentLabel />
        {safeSteps.length > 0 ? (
          <ResearchStepsTrace steps={safeSteps} defaultOpen={!!streaming} />
        ) : researchActivity.length > 0 ? (
          <ActivityTrace
            activities={researchActivity}
            label="Research activity"
            defaultOpen={false}
          />
        ) : null}
        <div
          className={`
            rounded-xl border border-[#1e1e1e] bg-[#080808] px-6 py-5
            ${streaming ? '' : ''}
          `}
        >
          <MarkdownRenderer content={before} streaming={streaming && !markerPresent && !after} />

          {hasArtifacts && markerPresent && (
            <ArtifactImages artifactPaths={artifactPaths!} threadId={threadId!} />
          )}

          {after && <MarkdownRenderer content={after} streaming={streaming} />}

          {hasArtifacts && !markerPresent && (
            <ArtifactImages artifactPaths={artifactPaths!} threadId={threadId!} />
          )}
        </div>
      </div>
    </div>
  )
}

function ArtifactImages({ artifactPaths, threadId }: { artifactPaths: string[]; threadId: string }) {
  const images = artifactPaths.filter(p => IMAGE_RE.test(p))
  if (!images.length) return null
  return (
    <div className="my-5 space-y-4">
      {images.map(p => {
        const filename = p.split('/').pop() ?? p
        const label = filename.replace(/\.[^.]+$/, '').replace(/[_-]/g, ' ')
        return (
          <figure key={p} className="space-y-2">
            <img
              src={`/artifacts/${threadId}/${filename}`}
              alt={label}
              className="rounded-xl border border-[#2a2a2a] max-w-full"
            />
            <figcaption className="text-xs text-zinc-600 text-center">{label}</figcaption>
          </figure>
        )
      })}
    </div>
  )
}

/** Status card shown during research planning/executing (before synthesis). */
function ResearchStatusCard({ run }: { run: AgentRunState }) {
  const { status, steps, completed_steps } = run
  const total = steps.length
  const running = steps.find(s => s.status === 'running')

  let label = ''
  if (status === 'classifying') label = 'Classifying intent…'
  else if (status === 'planning') label = 'Building research plan…'
  else if (status === 'awaiting_approval') label = 'Plan ready — review in the sidebar'
  else if (status === 'workflow_running') label = 'Running deterministic workflow…'
  else if (status === 'awaiting_assumptions') label = 'Assumptions ready — review in the sidebar'
  else if (status === 'executing') {
    label = running
      ? `Step ${completed_steps + 1}/${total} — ${running.description.length > 55 ? running.description.slice(0, 55) + '…' : running.description}`
      : `Executing step ${completed_steps + 1}/${total}…`
  }

  return (
    <div className="flex justify-start animate-fade-up">
      <div className="max-w-[85%]">
        <AgentLabel />
        <div className="pl-1 flex items-center gap-2.5 py-2">
          <div
            className={`
              w-1.5 h-1.5 rounded-full flex-shrink-0
              ${status === 'awaiting_approval' ? 'bg-amber-500' : 'bg-indigo-500 animate-pulse'}
            `}
          />
          <span className="text-sm text-zinc-500">{label}</span>
        </div>
      </div>
    </div>
  )
}

function ThinkingDots() {
  return (
    <div className="flex items-center gap-1 h-6 pl-1">
      {[0, 1, 2].map(i => (
        <div
          key={i}
          className="w-1.5 h-1.5 rounded-full bg-zinc-600 animate-pulse"
          style={{ animationDelay: `${i * 150}ms` }}
        />
      ))}
    </div>
  )
}

// ── Committed message renderer ────────────────────────────────────────────────

function CommittedMessage({ msg }: { msg: SessionMessage }) {
  if (msg.type === 'user') {
    if (msg.content.startsWith('[DCF_APPROVED]')) return null
    return <UserBubble content={msg.content} />
  }
  if (msg.type === 'chat_response') {
    return (
      <ChatBubble
        content={msg.content}
        toolCalls={msg.toolTrace}
        activities={msg.activity}
        persisted
      />
    )
  }
  if (msg.type === 'research_report') {
    return (
      <ResearchReportCard
        content={msg.content}
        threadId={msg.threadId}
        artifactPaths={msg.artifactPaths}
        steps={msg.researchSteps}
        activity={msg.activity}
      />
    )
  }
  return null
}

// ── Document card (attachment) ────────────────────────────────────────────────

const FILE_COLORS: Record<string, { bg: string; border: string; text: string; label: string }> = {
  pdf:  { bg: 'bg-red-500/15',    border: 'border-red-500/25',    text: 'text-red-400',    label: 'PDF'  },
  docx: { bg: 'bg-blue-500/15',   border: 'border-blue-500/25',   text: 'text-blue-400',   label: 'Word' },
  doc:  { bg: 'bg-blue-500/15',   border: 'border-blue-500/25',   text: 'text-blue-400',   label: 'Word' },
  xlsx: { bg: 'bg-emerald-500/15',border: 'border-emerald-500/25',text: 'text-emerald-400',label: 'Excel'},
  xls:  { bg: 'bg-emerald-500/15',border: 'border-emerald-500/25',text: 'text-emerald-400',label: 'Excel'},
  csv:  { bg: 'bg-teal-500/15',   border: 'border-teal-500/25',   text: 'text-teal-400',   label: 'CSV'  },
  txt:  { bg: 'bg-zinc-500/15',   border: 'border-zinc-500/25',   text: 'text-zinc-400',   label: 'Text' },
  md:   { bg: 'bg-zinc-500/15',   border: 'border-zinc-500/25',   text: 'text-zinc-400',   label: 'MD'   },
}

function DocumentCard({
  doc,
  selected,
  onSelect,
  onRemove,
}: {
  doc: DocumentInfo
  selected?: boolean
  onSelect?: (id: string) => void
  onRemove?: (id: string) => void
}) {
  const ext = doc.filename.split('.').pop()?.toLowerCase() ?? ''
  const color = FILE_COLORS[ext] ?? { bg: 'bg-zinc-500/15', border: 'border-zinc-500/25', text: 'text-zinc-400', label: ext.toUpperCase() }
  const isProcessing = doc.status === 'processing'
  const isError = doc.status === 'error'
  const name = doc.filename.length > 22 ? doc.filename.slice(0, 20) + '…' : doc.filename
  const clickable = !isProcessing && !isError && !!onSelect

  return (
    <div
      className={`
        relative flex items-center gap-2.5 px-3 py-2.5 rounded-xl border
        transition-colors duration-150 cursor-default select-none
        ${isError
          ? 'border-red-900/40 bg-red-950/20'
          : selected
            ? 'border-indigo-600/50 bg-indigo-950/20'
            : 'border-[#252535] bg-[#111118] hover:border-[#33334a]'
        }
      `}
      style={{ minWidth: 140, maxWidth: 200 }}
    >
      {/* File type icon */}
      <button
        type="button"
        onClick={() => clickable && onSelect?.(doc.doc_id)}
        disabled={!clickable}
        className={`flex items-center gap-2.5 min-w-0 flex-1 ${clickable ? 'cursor-pointer' : 'cursor-default'}`}
      >
        <div className={`w-9 h-9 rounded-lg ${color.bg} border ${color.border} flex items-center justify-center flex-shrink-0`}>
          {isProcessing ? (
            <span className="w-3 h-3 rounded-full border-2 border-current border-t-transparent animate-spin" style={{ color: color.text.replace('text-', '') }} />
          ) : (
            <svg width="16" height="16" viewBox="0 0 14 14" fill="none" className={color.text}>
              <path d="M2.5 1.5h6L11 4.5v8H2.5v-11z" stroke="currentColor" strokeWidth="1.2" strokeLinejoin="round" />
              <path d="M8 1.5V5h2.5" stroke="currentColor" strokeWidth="1.2" strokeLinejoin="round" />
            </svg>
          )}
        </div>

        {/* Name + type */}
        <div className="min-w-0 text-left">
          <p className={`text-xs font-medium truncate leading-tight ${isError ? 'text-red-400' : selected ? 'text-zinc-100' : 'text-zinc-200'}`}>
            {name}
          </p>
          <p className="text-[11px] text-zinc-500 leading-tight mt-0.5">
            {isProcessing ? 'Processing…' : isError ? 'Error' : color.label}
          </p>
        </div>
      </button>

      {/* Remove button */}
      {onRemove && (
        <button
          type="button"
          onClick={(e) => { e.stopPropagation(); onRemove(doc.doc_id) }}
          className="absolute top-1.5 right-1.5 w-4 h-4 rounded-full bg-[#1a1a22] border border-[#2a2a38] flex items-center justify-center text-zinc-500 hover:text-zinc-200 hover:bg-[#252535] transition-colors"
          title="Remove"
        >
          <svg width="7" height="7" viewBox="0 0 7 7" fill="none">
            <path d="M1 1l5 5M6 1L1 6" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" />
          </svg>
        </button>
      )}
    </div>
  )
}

// ── Main component ────────────────────────────────────────────────────────────

interface Props {
  session: Session | undefined
  activeRun: AgentRunState
  mode: Mode
  onModeChange: (mode: Mode) => void
  onSubmit: (query: string, mode: Mode) => void
  onUpload?: (file: File) => void
  docs?: DocumentInfo[]
  selectedDocId?: string | null
  onSelectDoc?: (docId: string) => void
  onRemoveDoc?: (docId: string) => void
  disabled?: boolean
}

export function MessageThread({ session, activeRun, mode, onModeChange, onSubmit, onUpload, docs = [], selectedDocId, onSelectDoc, onRemoveDoc, disabled }: Props) {
  const bottomRef = useRef<HTMLDivElement>(null)
  const messages = session?.messages ?? []

  const runActive = !['idle', 'complete', 'error', 'rejected'].includes(activeRun.status)
  const isResearchRun = activeRun.resolved_intent === 'research' || (
    runActive && !['chat_responding', 'classifying'].includes(activeRun.status)
      && activeRun.resolved_intent !== 'chat'
  )
  const isChatRun = activeRun.resolved_intent === 'chat' || activeRun.status === 'chat_responding'
  const isSynthesizing = activeRun.status === 'synthesizing'

  // Determine if we're in a "pre-research" status (planning/executing — before synthesis)
  const showResearchStatus = runActive && isResearchRun && !isSynthesizing

  // Live chat messages (streaming)
  const liveChatMessages = isChatRun ? activeRun.chat_messages : []
  const lastCommittedUser = [...messages].reverse().find(m => m.type === 'user')
  const showPendingUser =
    runActive &&
    !!activeRun.query &&
    lastCommittedUser?.content !== activeRun.query

  // Auto-scroll on new content
  useEffect(() => {
    if (runActive || isSynthesizing) {
      bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
    }
  }, [activeRun.report, liveChatMessages.length, runActive, isSynthesizing, showPendingUser])

  const isInputBusy =
    disabled ||
    activeRun.status === 'classifying' ||
    activeRun.status === 'planning' ||
    activeRun.status === 'executing' ||
    activeRun.status === 'synthesizing' ||
    activeRun.status === 'chat_responding'

  const isEmpty = messages.length === 0 && !runActive

  return (
    <div className="flex-1 flex flex-col overflow-hidden min-w-0">

      {/* Message list */}
      <div className="flex-1 overflow-y-auto">
        {isEmpty ? (
          <EmptyState mode={mode} onSubmit={onSubmit} onModeChange={onModeChange} onUpload={onUpload} />
        ) : (
          <div className="max-w-3xl mx-auto px-6 py-8 space-y-6">
            {/* Committed session messages */}
            {messages.map(msg => (
              <CommittedMessage key={msg.id} msg={msg} />
            ))}

            {/* Live: user message before session commit is visible.
                Hide [DCF_APPROVED] approval triggers — the confirmed card and
                activity trace make the action visible without a text bubble. */}
            {showPendingUser && !activeRun.query.startsWith('[DCF_APPROVED]') && (
              <UserBubble content={activeRun.query} />
            )}

            {/* Live: chat streaming — chat-scoped activities feed the bubble. */}
            {isChatRun && liveChatMessages.map((m, idx) => {
              if (m.role !== 'assistant') return null
              const assistantMsgs = liveChatMessages.filter(x => x.role === 'assistant')
              const isLast = idx === liveChatMessages.length - 1 || m.id === assistantMsgs[assistantMsgs.length - 1]?.id
              const chatActivities = isLast ? activeRun.activity.filter(a => a.scope === 'chat') : []
              const hasDcf = isLast && !!activeRun.dcf_review
              const prevMsg = liveChatMessages[idx - 1]
              const hideLabel = !!prevMsg && prevMsg.role === 'assistant'
              return (
                <ChatBubble
                  key={m.id}
                  content={m.content}
                  streaming={m.streaming}
                  activities={chatActivities.length > 0 ? chatActivities : undefined}
                  dcfReview={hasDcf ? activeRun.dcf_review! : undefined}
                  onDcfApprove={hasDcf ? () => {
                    // DcfHitlSection calls /dcf-decision endpoint directly when threadId set.
                    // This callback is a no-op fallback for missing threadId.
                  } : undefined}
                  onDcfReject={hasDcf ? () => {
                    // DcfHitlSection calls /dcf-decision endpoint directly when threadId set.
                    // No-op fallback.
                  } : undefined}
                  threadId={activeRun.thread_id || undefined}
                  hideLabel={hideLabel}
                />
              )
            })}

            {/* Live: research status (planning / executing) */}
            {showResearchStatus && <ResearchStatusCard run={activeRun} />}

            {/* Live: synthesis streaming — report builds in-thread */}
            {isSynthesizing && (
              <ResearchReportCard
                content={activeRun.report}
                streaming={true}
                steps={activeRun.steps}
                activity={activeRun.activity}
              />
            )}

            <div ref={bottomRef} />
          </div>
        )}
      </div>

      {/* Input bar */}
      {!isEmpty && (
        <div className="border-t border-[#141414] px-4 pt-3 pb-4 bg-[#0a0a0a] flex-shrink-0">
          <div className="max-w-3xl mx-auto space-y-2.5">
            {/* Attachment cards — visible above input when docs uploaded */}
            {docs.length > 0 && (
              <div className="flex flex-wrap gap-2">
                {docs.map(doc => (
                  <DocumentCard
                    key={doc.doc_id}
                    doc={doc}
                    selected={doc.doc_id === selectedDocId}
                    onSelect={onSelectDoc}
                    onRemove={onRemoveDoc}
                  />
                ))}
              </div>
            )}
            <QueryInput
              onSubmit={onSubmit}
              onUpload={onUpload}
              disabled={isInputBusy}
              mode={mode}
              onModeChange={onModeChange}
            />
          </div>
        </div>
      )}
    </div>
  )
}

// ── Empty / hero state ────────────────────────────────────────────────────────

function EmptyState({
  mode,
  onSubmit,
  onModeChange,
  onUpload,
}: {
  mode: Mode
  onSubmit: (query: string, mode: Mode) => void
  onModeChange: (mode: Mode) => void
  onUpload?: (file: File) => void
}) {
  const examples =
    mode === 'chat'
      ? ['Explain DCF valuation', 'What is EBITDA?', 'How do LBOs work?']
      : mode === 'research'
        ? ['Apple stock last 5 years', 'AI landscape 2025', 'Compare React vs Vue']
        : ['Apple vs Google financials', 'Explain quantitative easing', 'AI model releases 2025']

  return (
    <div className="flex-1 flex flex-col items-center justify-center min-h-full px-6 py-16 space-y-8">
      <div className="w-full max-w-xl space-y-2 text-center">
        <h2 className="text-xl font-medium text-zinc-100 tracking-tight">
          {mode === 'chat'
            ? 'What would you like to discuss?'
            : mode === 'research'
              ? 'What do you want to research?'
              : 'What can I help you with?'}
        </h2>
        <p className="text-sm text-zinc-600">
          {mode === 'chat'
            ? 'Quick answers, explanations, and follow-ups.'
            : mode === 'research'
              ? 'Deep research with a structured plan and full report.'
              : "I'll decide whether to research or answer directly."}
        </p>
      </div>

      <div className="w-full max-w-xl">
        <QueryInput
          onSubmit={onSubmit}
          onUpload={onUpload}
          disabled={false}
          autoFocus
          mode={mode}
          onModeChange={onModeChange}
        />
      </div>

      <div className="flex flex-wrap gap-2 justify-center">
        {examples.map(ex => (
          <button
            key={ex}
            onClick={() => onSubmit(ex, mode)}
            className="px-3 py-1.5 rounded-full border border-[#222] text-xs text-zinc-600 hover:text-zinc-300 hover:border-[#333] transition-colors duration-150"
          >
            {ex}
          </button>
        ))}
      </div>
    </div>
  )
}
