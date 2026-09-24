import { useState, useCallback, useRef } from 'react'
import type { AgentRunState, ChatMessage, DcfReviewState, MemoReviewState, Mode, StepState, ToolCall, WorkflowContextReviewState } from '../types'
import type { UserSettings } from '../lib/userSettings'
import {
  activityStatusToToolStatus,
  isActivityEvent,
  mergeActivity,
  type ActivityEntry,
  type ActivityEvent,
} from '../lib/activity'
import { isTraceSpanEvent, mergeTraceSpan, type TraceSpanEvent } from '../lib/tracing'
import type { TraceSpan } from '../lib/tracing'

let _msgIdCounter = 0
const nextId = () => `msg_${++_msgIdCounter}`

function completedClientSpan(
  traceId: string,
  rootSpanId: string | undefined,
  name: string,
  startedAt: number,
  endedAt: number,
  metadata?: Record<string, unknown>,
): TraceSpanEvent {
  return {
    type: 'trace_span',
    trace_id: traceId,
    span_id: `client_${name}_${Math.random().toString(36).slice(2, 10)}`,
    parent_span_id: rootSpanId,
    category: 'transport',
    name,
    status: 'completed',
    started_at: startedAt,
    ended_at: endedAt,
    duration_ms: (endedAt - startedAt) * 1000,
    metadata,
  }
}

function completeOpenTraceSpans(spans: TraceSpan[], endedAt: number): TraceSpan[] {
  return spans.map(span => {
    if (span.status === 'completed' || span.status === 'error' || typeof span.started_at !== 'number') {
      return span
    }
    return {
      ...span,
      status: 'completed',
      ended_at: endedAt,
      duration_ms: Math.max(0, (endedAt - span.started_at) * 1000),
    }
  })
}

const INITIAL_STATE: AgentRunState = {
  status: 'idle',
  thread_id: null,
  query: '',
  mode: 'auto',
  resolved_intent: null,
  steps: [],
  report: '',
  artifact_paths: [],
  error: null,
  completed_steps: 0,
  chat_messages: [],
  activity: [],
  trace_spans: [],
  execution_trace: [],
  dcf_review: null,
  dcf_evidence_items: [],
  dcf_citation_map: {},
  deck_review: null,
  memo_review: null,
  workflow_context_review: null,
}

/**
 * Project an ActivityEntry into the legacy `ToolCall` shape consumed by
 * `StepCard`, `ResearchStepsTrace`, and the activity-row renderer.
 */
function entryToToolCall(entry: ActivityEntry): ToolCall {
  return {
    tool_name: entry.name || 'unknown',
    status: activityStatusToToolStatus(entry.status),
    summary: String(entry.summary || entry.error || ''),
    args_preview: String(entry.args_preview || ''),
  }
}

/**
 * Rebuild each step's `tool_calls` array from the unified activity log.
 * Activities with `scope === "research"` and a matching `step_id` flow into
 * the step they belong to; workflow-scoped activities nest under whichever
 * research step is currently `running` (mirrors the legacy reducer).
 */
function projectStepToolCalls(
  steps: StepState[],
  activity: ActivityEntry[],
): StepState[] {
  if (!steps.length) return steps
  const runningStep = steps.find(s => s.status === 'running')?.id

  const byStep = new Map<string, ToolCall[]>()
  for (const a of activity) {
    let stepId: string | undefined
    if (a.scope === 'research' && a.step_id && a.step_id !== 'chat') {
      stepId = a.step_id
    } else if (a.scope === 'workflow') {
      stepId = a.step_id || runningStep
    }
    if (!stepId) continue
    const list = byStep.get(stepId) ?? []
    list.push(entryToToolCall(a))
    byStep.set(stepId, list)
  }

  let changed = false
  const next = steps.map(step => {
    const projected = byStep.get(step.id) ?? []
    const existing = Array.isArray(step.tool_calls) ? step.tool_calls : []
    if (projected.length === 0 && existing.length === 0) return step
    // Cheap equality: same length + same visible row fields.
    const same =
      projected.length === existing.length &&
      projected.every((t, i) =>
        t.tool_name === existing[i].tool_name &&
        t.status === existing[i].status &&
        t.summary === existing[i].summary &&
        t.args_preview === existing[i].args_preview,
      )
    if (same) return step
    changed = true
    return { ...step, tool_calls: projected }
  })
  return changed ? next : steps
}

export function useAgentRun() {
  const [state, setState] = useState<AgentRunState>(INITIAL_STATE)
  const esRef = useRef<EventSource | null>(null)

  const handleEvent = useCallback((e: MessageEvent) => {
    let data: Record<string, unknown>
    try {
      data = JSON.parse(e.data as string)
    } catch {
      return
    }

    const type = data.type as string
    if (type === 'ping' || type === 'done') return

    if (isTraceSpanEvent(data)) {
      setState(prev => ({
        ...prev,
        trace_spans: mergeTraceSpan(prev.trace_spans, data),
      }))
      return
    }

    // Unified activity envelope — merged into a single store regardless of
    // scope (research / chat / workflow). We additionally project the
    // unified log into the legacy `step.tool_calls` shape so existing
    // research-step renderers (StepCard, ResearchStepsTrace, ActivityTrace)
    // keep working without each touching `state.activity` directly. Chat
    // renderers consume `state.activity` filtered by scope.
    if (isActivityEvent(data)) {
      setState(prev => {
        const nextActivity = mergeActivity(
          prev.activity,
          data as unknown as ActivityEvent,
        )
        return {
          ...prev,
          activity: nextActivity,
          steps: projectStepToolCalls(prev.steps, nextActivity),
        }
      })
      return
    }

    if (type === 'execution_step') {
      setState(prev => ({
        ...prev,
        execution_trace: [...prev.execution_trace, data as unknown as AgentRunState['execution_trace'][number]],
      }))
      return
    }

    setState(prev => {
      switch (type) {

        // ── Intent routing ─────────────────────────────────────────────────
        case 'intent_classified': {
          const intent = data.intent as 'research' | 'chat'
          return {
            ...prev,
            resolved_intent: intent,
            status: intent === 'research' ? 'planning' : 'chat_responding',
          }
        }

        // ── Research path ──────────────────────────────────────────────────
        case 'plan_ready': {
          const rawSteps = (data.plan as { steps?: unknown[] })?.steps ?? []
          const steps: StepState[] = (rawSteps as Record<string, unknown>[]).map(s => ({
            id: s.id as string,
            description: s.description as string,
            depends_on: (s.depends_on as string[]) ?? [],
            status: 'pending',
            tool_calls: [],
            reasoning: '',
          }))
          return { ...prev, status: 'awaiting_approval', steps }
        }

        case 'execution_started':
          return { ...prev, status: 'executing' }

        case 'workflow_context_review':
          return {
            ...prev,
            status: 'awaiting_workflow_context',
            workflow_context_review: {
              workflow_id: (data.workflow_id as string) ?? '',
              title: (data.title as string) ?? 'Workflow setup',
              context: (data.context as Record<string, any>) ?? {},
              controls: (data.controls as WorkflowContextReviewState['controls']) ?? [],
            },
          }

        // NOTE: `workflow_started` and `workflow_step` legacy reducer
        // branches were removed when DCF migrated to the unified `activity`
        // contract. The workflow span now flows through `kind="workflow"`
        // and `kind="workflow_step"` activity entries instead.

        // NOTE: `workflow_started` and `workflow_step` legacy reducer
        // branches were removed when DCF migrated to the unified `activity`
        // contract. The workflow span now flows through `kind="workflow"`
        // and `kind="workflow_step"` activity entries instead.

        case 'dcf_assumptions_review':
          return {
            ...prev,
            status: 'awaiting_assumptions',
            dcf_review: {
              ticker: (data.ticker as string) ?? '',
              horizon_years: (data.horizon_years as number) ?? 5,
              assumptions: (data.assumptions as Record<string, number>) ?? {},
              provenance: (data.assumption_provenance as Record<string, { source?: string; confidence?: number }>) ?? {},
              memo_proposals: (data.memo_proposals as Record<string, { rationale: string; confidence: number }>) ?? {},
              evidence_items: (data.evidence_items as import('../types').EvidenceItem[]) ?? [],
            },
          }

        case 'assumptions_submitted': {
          // Discard pre-HITL narration streamed before approval (the DCF
          // workflow emits "### Bear/Base/Bull scenario…" chat_tokens into the
          // streaming bubble while proposing assumptions). The post-approval
          // report streams fresh into the same message, so wipe the junk now —
          // otherwise it stays prepended above the report (m.content = junk +
          // report is longer than the chat_complete payload, so the length
          // check in chat_complete keeps it).
          const msgs = prev.chat_messages.map(m =>
            m.streaming && m.role === 'assistant' ? { ...m, content: '' } : m
          )
          return { ...prev, status: 'chat_responding', dcf_review: null, chat_messages: msgs }
        }

        case 'assumptions_rejected':
          return { ...prev, status: 'rejected' }

        case 'deck_outline_review':
          return {
            ...prev,
            status: 'awaiting_outline_review',
            deck_review: {
              deck_title: (data.deck_title as string) ?? '',
              hitl_mode: (data.hitl_mode as string) ?? 'partial',
              slide_count: (data.slide_count as number) ?? 0,
              outline: (data.outline as import('../types').DeckReviewState['outline']) ?? { slides: [] },
              blocks_preview: (data.blocks_preview as import('../types').DeckBlockPreview[]) ?? [],
            },
          }

        case 'deck_outline_submitted':
          return { ...prev, status: 'workflow_running', deck_review: null }

        case 'deck_outline_rejected':
          return { ...prev, status: 'chat_responding', deck_review: null }

        case 'memo_draft_review':
          return {
            ...prev,
            status: 'awaiting_memo_review',
            memo_review: {
              draft: (data.draft as MemoReviewState['draft']) ?? {
                title: 'Investment memo', executive_summary: '', sections: {}, recommendation: '',
                source_version_ids: [], source_refs: [], confidence: 0, limitations: [],
              },
              sources: (data.sources as MemoReviewState['sources']) ?? [],
              context_version_id: (data.context_version_id as string) ?? null,
            },
          }

        case 'memo_draft_submitted':
          return { ...prev, status: 'workflow_running', memo_review: null }

        case 'memo_draft_rejected':
          return { ...prev, status: 'chat_responding', memo_review: null }

        case 'step_start': {
          const steps = prev.steps.map(s =>
            s.id === data.step_id ? { ...s, status: 'running' as const } : s,
          )
          return { ...prev, steps }
        }

        case 'step_reasoning': {
          const steps = prev.steps.map(s =>
            s.id === data.step_id ? { ...s, reasoning: (data.text as string) ?? '' } : s,
          )
          return { ...prev, steps }
        }

        // NOTE: `tool_call_start` / `tool_call_end` / `tool_error` legacy
        // reducer branches were removed when both research and chat paths
        // migrated to the unified `activity` contract. Tool telemetry now
        // flows exclusively through `kind="tool"` activity entries; the
        // `projectStepToolCalls` helper above backfills `step.tool_calls`
        // so existing research-step renderers (StepCard,
        // ResearchStepsTrace, ActivityTrace) keep working unchanged.

        case 'step_complete': {
          const steps = prev.steps.map(s =>
            s.id === data.step_id ? { ...s, status: 'completed' as const } : s,
          )
          const completed_steps = steps.filter(s => s.status === 'completed').length
          return { ...prev, steps, completed_steps }
        }

        case 'synthesis_start':
          return { ...prev, status: 'synthesizing' }

        case 'synthesis_token':
          return { ...prev, report: prev.report + ((data.token as string) ?? '') }

        case 'synthesis_complete':
          return {
            ...prev,
            artifact_paths: (data.artifact_paths as string[]) ?? [],
          }

        // ── Chat path ──────────────────────────────────────────────────────
        case 'chat_start': {
          // Add a blank streaming assistant message
          const assistantMsg: ChatMessage = {
            id: nextId(),
            role: 'assistant',
            content: '',
            streaming: true,
          }
          return {
            ...prev,
            status: 'chat_responding',
            chat_messages: [...prev.chat_messages, assistantMsg],
          }
        }

        case 'chat_token': {
          const token = (data.token as string) ?? ''
          const msgs = [...prev.chat_messages]
          const lastIdx = msgs.length - 1
          if (lastIdx >= 0 && msgs[lastIdx].role === 'assistant' && msgs[lastIdx].streaming) {
            msgs[lastIdx] = { ...msgs[lastIdx], content: msgs[lastIdx].content + token }
          }
          return { ...prev, chat_messages: msgs }
        }

        case 'chat_complete': {
          const content = (data.content as string) ?? ''
          const artifactPaths = (data.artifact_paths as string[]) ?? []
          const evidenceItems = (data.evidence_items as import('../types').EvidenceItem[]) ?? []
          const citationMap = (data.citation_map as Record<string, string>) ?? {}
          const msgs = prev.chat_messages.map(m => {
            if (!m.streaming) return m
            // Prefer the event content when it's the substantive final answer.
            // A tool-using DCF run streams a short pre-tool preamble (a few
            // chat_tokens) THEN delivers the full report via chat_complete; the
            // old `m.content || content` kept the stray preamble and DROPPED the
            // report. Use whichever is longer — the report always wins over a
            // partial preamble, while a fully-streamed chat answer (event
            // content empty/equal) keeps its streamed text.
            const finalText = content && content.length > (m.content?.length ?? 0)
              ? content
              : (m.content || content)
            return { ...m, streaming: false, content: finalText }
          })
          // If no streaming assistant message exists yet, add one now
          const hasAssistant = msgs.some(m => m.role === 'assistant')
          const finalMsgs = hasAssistant ? msgs : [
            ...msgs,
            { id: nextId(), role: 'assistant' as const, content, streaming: false },
          ]
          // If a DCF or deck HITL card is pending, stay in the awaiting state so
          // the 150ms reset in App.tsx doesn't wipe review state before the user
          // sees the card. The next startRun call resets everything cleanly.
          const nextStatus = prev.dcf_review
            ? 'awaiting_assumptions'
            : prev.deck_review
              ? 'awaiting_outline_review'
              : prev.memo_review
                ? 'awaiting_memo_review'
              : prev.workflow_context_review
                ? 'awaiting_workflow_context'
              : 'chat_responding'
          return {
            ...prev,
            status: nextStatus,
            chat_messages: finalMsgs,
            artifact_paths: artifactPaths.length ? artifactPaths : prev.artifact_paths,
            dcf_evidence_items: evidenceItems.length ? evidenceItems : prev.dcf_evidence_items,
            dcf_citation_map: Object.keys(citationMap).length ? citationMap : prev.dcf_citation_map,
          }
        }
        // ── Shared terminal states ─────────────────────────────────────────
        case 'run_complete': {
          const runArtifacts = (data.artifact_paths as string[]) ?? []
          return {
            ...prev,
            artifact_paths: runArtifacts.length ? runArtifacts : prev.artifact_paths,
          }
        }

        case 'rejected':
          return { ...prev, status: 'rejected' }

        case 'error':
          return { ...prev, status: 'error', error: (data.message as string) ?? 'Unknown error' }

        default:
          return prev
      }
    })
  }, [])

  const startRun = useCallback(
    async (
      query: string,
      mode: Mode,
      chatThreadId?: string,
      sessionId?: string,
      userSettings?: UserSettings,
    ): Promise<string | null> => {
      esRef.current?.close()
      const requestStartedAt = Date.now() / 1000

      const userMsg: ChatMessage = { id: nextId(), role: 'user', content: query }

      setState(prev => {
        // For chat mode, preserve conversation history across turns
        const keepHistory =
          mode === 'chat' ||
          (mode === 'auto' && prev.resolved_intent === 'chat')

        return {
          ...INITIAL_STATE,
          status: 'classifying',
          query,
          mode,
          // Keep chat history for multi-turn continuity
          chat_messages: keepHistory
            ? [...prev.chat_messages, userMsg]
            : [userMsg],
        }
      })

      // Reuse thread when chatThreadId is provided — caller (App.tsx) already
      // gates this to chat-resolved turns only, so auto-→-research still gets
      // a fresh thread (chatThreadId is undefined in that case).
      const threadIdToUse = chatThreadId ?? undefined

      const res = await fetch('/runs', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          query,
          mode,
          thread_id: threadIdToUse,
          session_id: sessionId,
          user_settings: userSettings,
        }),
      })
      if (!res.ok) {
        const err = await res.text()
        setState(prev => ({ ...prev, status: 'error', error: err }))
        return null
      }
      const responseReceivedAt = Date.now() / 1000
      const { thread_id, start_event_id, trace_id, root_span_id } = (await res.json()) as {
        thread_id: string
        start_event_id?: number
        trace_id?: string
        root_span_id?: string
      }
      setState(prev => ({
        ...prev,
        thread_id,
        trace_spans: trace_id
          ? mergeTraceSpan(prev.trace_spans, completedClientSpan(
              trace_id,
              root_span_id,
              'frontend_run_request',
              requestStartedAt,
              responseReceivedAt,
              { phase: 'POST /runs' },
            ))
          : prev.trace_spans,
      }))

      // Pass start_event_id so the server doesn't replay prior turns (events
      // persisted from earlier messages in the same chat thread). Without
      // this, second+ turns see META's substeps in the NVDA run, etc.
      const afterId = typeof start_event_id === 'number' ? start_event_id : 0
      const es = new EventSource(`/runs/${thread_id}/events?after_id=${afterId}`)
      let firstEventSeen = false

      es.onmessage = (e: MessageEvent) => {
        let data: Record<string, unknown>
        try { data = JSON.parse(e.data as string) } catch { return }
        if (!firstEventSeen && trace_id) {
          firstEventSeen = true
          const firstEventAt = Date.now() / 1000
          setState(prev => ({
            ...prev,
            trace_spans: mergeTraceSpan(prev.trace_spans, completedClientSpan(
              trace_id,
              root_span_id,
              'submit_to_first_event',
              requestStartedAt,
              firstEventAt,
              { first_event_type: data.type },
            )),
          }))
        }
        if (data.type === 'done') {
          const endedAt = Date.now() / 1000
          setState(prev => ({
            ...prev,
            status: prev.dcf_review || prev.deck_review || prev.memo_review
              || prev.workflow_context_review
              ? prev.status
              : 'complete',
            trace_spans: completeOpenTraceSpans(prev.trace_spans, endedAt),
          }))
          es.close()
          esRef.current = null
          return
        }
        handleEvent(e)
      }

      es.onerror = () => {
        setState(prev => {
          const terminal = ['complete', 'rejected', 'error']
          if (terminal.includes(prev.status)) return prev
          return { ...prev, status: 'error', error: 'Connection lost' }
        })
        es.close()
        esRef.current = null
      }

      esRef.current = es
      return thread_id
    },
    [handleEvent],
  )

  const amendMessage = useCallback(
    async (
      threadId: string,
      originalContent: string,
      newContent: string,
      mode: Mode,
      sessionId?: string,
    ): Promise<boolean> => {
      esRef.current?.close()

      const userMsg: ChatMessage = { id: nextId(), role: 'user', content: newContent }

      setState(() => ({
        ...INITIAL_STATE,
        status: 'classifying',
        query: newContent,
        mode,
        thread_id: threadId,
        chat_messages: [userMsg],
      }))

      const res = await fetch(`/runs/${threadId}/amend`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          original_content: originalContent,
          new_content: newContent,
          mode,
          session_id: sessionId,
        }),
      })
      if (!res.ok) {
        const err = await res.text()
        setState(prev => ({ ...prev, status: 'error', error: err }))
        return false
      }
      const { start_event_id } = (await res.json()) as { thread_id: string; start_event_id?: number }
      const afterId = typeof start_event_id === 'number' ? start_event_id : 0
      const es = new EventSource(`/runs/${threadId}/events?after_id=${afterId}`)

      es.onmessage = (e: MessageEvent) => {
        let data: Record<string, unknown>
        try { data = JSON.parse(e.data as string) } catch { return }
        if (data.type === 'done') {
          es.close()
          esRef.current = null
          return
        }
        handleEvent(e)
      }
      es.onerror = () => {
        setState(prev => {
          const terminal = ['complete', 'rejected', 'error']
          if (terminal.includes(prev.status)) return prev
          return { ...prev, status: 'error', error: 'Connection lost' }
        })
        es.close()
        esRef.current = null
      }
      esRef.current = es
      return true
    },
    [handleEvent],
  )

  const watchRun = useCallback((threadId: string, query = '') => {
    esRef.current?.close()
    setState({ ...INITIAL_STATE, status: 'classifying', thread_id: threadId, query, mode: 'auto' })
    const es = new EventSource(`/runs/${encodeURIComponent(threadId)}/events?after_id=0`)
    es.onmessage = (event: MessageEvent) => {
      let data: Record<string, unknown>
      try { data = JSON.parse(event.data as string) } catch { return }
      if (data.type === 'done') { es.close(); esRef.current = null; return }
      handleEvent(event)
    }
    es.onerror = () => {
      es.close(); esRef.current = null
      setState(prev => ['complete', 'rejected', 'error'].includes(prev.status) ? prev : { ...prev, status: 'error', error: 'Connection lost' })
    }
    esRef.current = es
  }, [handleEvent])

  const approve = useCallback(async () => {
    const tid = state.thread_id
    if (!tid) return
    if (state.status === 'awaiting_assumptions') {
      setState(prev => ({ ...prev, status: 'chat_responding', dcf_review: null }))
      return
    }
    if (state.status === 'awaiting_outline_review') {
      setState(prev => ({ ...prev, status: 'chat_responding', deck_review: null }))
      return
    }
    if (state.status === 'awaiting_memo_review') {
      setState(prev => ({ ...prev, status: 'workflow_running', memo_review: null }))
      return
    }
    await fetch(`/runs/${tid}/decision`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ approved: true }),
    })
  }, [state.status, state.thread_id])

  const submitWorkflowContext = useCallback(async (context: Record<string, any>, approved = true) => {
    const tid = state.thread_id
    if (!tid) return
    const response = await fetch(`/runs/${tid}/workflow-context-decision`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        approved,
        action: approved ? 'approve' : 'cancel',
        context,
      }),
    })
    if (!response.ok) throw new Error('Could not submit decision. Please retry.')
    setState(prev => ({
      ...prev,
      status: approved || context.workflow_id === 'tracked_task' ? 'chat_responding' : 'idle',
      workflow_context_review: null,
    }))
  }, [state.thread_id])

  const reject = useCallback(async () => {
    if (state.status === 'awaiting_assumptions') {
      setState(prev => ({ ...prev, status: 'idle', dcf_review: null }))
      return
    }
    if (state.status === 'awaiting_outline_review') {
      setState(prev => ({ ...prev, status: 'idle', deck_review: null }))
      return
    }
    if (state.status === 'awaiting_memo_review') {
      setState(prev => ({ ...prev, status: 'idle', memo_review: null }))
      return
    }
    const tid = state.thread_id
    if (!tid) return
    await fetch(`/runs/${tid}/decision`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ approved: false }),
    })
  }, [state.status, state.thread_id])

  const reset = useCallback(() => {
    esRef.current?.close()
    esRef.current = null
    setState(INITIAL_STATE)
  }, [])

  return { state, startRun, amendMessage, watchRun, approve, reject, reset, submitWorkflowContext }
}
