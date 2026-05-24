import { useState, useCallback, useRef } from 'react'
import type { AgentRunState, ChatMessage, DcfReviewState, Mode, StepState, ToolCall } from '../types'
import {
  activityStatusToToolStatus,
  isActivityEvent,
  mergeActivity,
  type ActivityEntry,
  type ActivityEvent,
} from '../lib/activity'

let _msgIdCounter = 0
const nextId = () => `msg_${++_msgIdCounter}`

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
  dcf_review: null,
  dcf_evidence_items: [],
  dcf_citation_map: {},
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

        case 'assumptions_submitted':
          return { ...prev, status: 'chat_responding', dcf_review: null }

        case 'assumptions_rejected':
          return { ...prev, status: 'rejected' }

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
            status: 'complete',
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
            // Use event content if it was never streamed token-by-token (tool-using runs)
            return { ...m, streaming: false, content: m.content || content }
          })
          // If no streaming assistant message exists yet, add one now
          const hasAssistant = msgs.some(m => m.role === 'assistant')
          const finalMsgs = hasAssistant ? msgs : [
            ...msgs,
            { id: nextId(), role: 'assistant' as const, content, streaming: false },
          ]
          // If a DCF HITL card is pending, stay in awaiting_assumptions so
          // the 150ms reset in App.tsx doesn't wipe dcf_review before the user
          // sees the card. The next startRun call resets everything cleanly.
          const nextStatus = prev.dcf_review ? 'awaiting_assumptions' : 'complete'
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
        case 'run_complete':
          return {
            ...prev,
            status: prev.dcf_review
              ? prev.status  // HITL pending — don't complete, card must stay visible
              : prev.report || prev.chat_messages.length || data.workflow ? 'complete' : prev.status,
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
    async (query: string, mode: Mode, chatThreadId?: string, sessionId?: string): Promise<string | null> => {
      esRef.current?.close()

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
        body: JSON.stringify({ query, mode, thread_id: threadIdToUse, session_id: sessionId }),
      })
      if (!res.ok) {
        const err = await res.text()
        setState(prev => ({ ...prev, status: 'error', error: err }))
        return null
      }
      const { thread_id, start_event_id } = (await res.json()) as {
        thread_id: string
        start_event_id?: number
      }
      setState(prev => ({ ...prev, thread_id }))

      // Pass start_event_id so the server doesn't replay prior turns (events
      // persisted from earlier messages in the same chat thread). Without
      // this, second+ turns see META's substeps in the NVDA run, etc.
      const afterId = typeof start_event_id === 'number' ? start_event_id : 0
      const es = new EventSource(`/runs/${thread_id}/events?after_id=${afterId}`)

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
      return thread_id
    },
    [handleEvent],
  )

  const approve = useCallback(async () => {
    const tid = state.thread_id
    if (!tid) return
    if (state.status === 'awaiting_assumptions') {
      setState(prev => ({ ...prev, status: 'chat_responding', dcf_review: null }))
      return
    }
    await fetch(`/runs/${tid}/decision`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ approved: true }),
    })
  }, [state.status, state.thread_id])

  const reject = useCallback(async () => {
    if (state.status === 'awaiting_assumptions') {
      setState(prev => ({ ...prev, status: 'idle', dcf_review: null }))
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

  return { state, startRun, approve, reject, reset }
}
