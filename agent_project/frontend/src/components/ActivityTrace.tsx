import { useState, useMemo } from 'react'
import type { StepState, ToolCall } from '../types'
import type { ActivityEntry, ActivityScope } from '../lib/activity'
import { cleanToolSummary, getToolDisplay } from '../lib/toolLabels'

/**
 * Convert a unified ActivityEntry[] (the new contract) into the legacy
 * ToolCall[] shape that the row renderer already understands. Keeps the
 * UI rendering path single-source-of-truth while we migrate.
 */
export function activitiesToToolCalls(
  entries: ActivityEntry[],
  options: { scope?: ActivityScope; stepId?: string } = {},
): ToolCall[] {
  const { scope, stepId } = options
  const safeEntries = Array.isArray(entries) ? entries : []
  const filtered = safeEntries.filter(e => {
    if (!e || typeof e !== 'object') return false
    if (scope && e.scope !== scope) return false
    if (stepId && e.step_id !== stepId) return false
    return true
  })
  return filtered.map(e => ({
    tool_name: e.name || 'unknown',
    status:
      e.status === 'completed' || e.status === 'skipped'
        ? 'done'
        : e.status === 'error'
          ? 'error'
          : 'running',
    summary: String(e.summary || e.error || ''),
    args_preview: String(e.args_preview || ''),
  }))
}

/**
 * Collapsible audit trail for a chat run.
 *
 * Shows one row per tool call with status dot, human-readable label, optional
 * args preview, and an expandable summary. Used both for live runs (with
 * streaming statuses) and for committed messages where the trace is read-only.
 *
 * Accepts EITHER `toolCalls` (legacy ToolCall[] shape, still emitted by the
 * old `tool_call_*` events) OR `activities` (unified ActivityEntry[] from
 * the new `activity` event envelope). When both are supplied, `activities`
 * wins. Once all backend emitters use the activity contract, the
 * `toolCalls` prop can be removed.
 */
export function ActivityTrace({
  toolCalls,
  activities,
  scope,
  stepId,
  defaultOpen,
  label = 'Activity',
  emptyHint,
}: {
  toolCalls?: ToolCall[]
  activities?: ActivityEntry[]
  scope?: ActivityScope
  stepId?: string
  defaultOpen?: boolean
  label?: string
  emptyHint?: string
}) {
  const [open, setOpen] = useState<boolean>(defaultOpen ?? false)

  const rows = useMemo<ToolCall[]>(() => {
    if (activities && activities.length) {
      return activitiesToToolCalls(activities, { scope, stepId })
    }
    return Array.isArray(toolCalls) ? toolCalls : []
  }, [activities, toolCalls, scope, stepId])

  if (!rows.length) {
    if (!emptyHint) return null
    return (
      <p className="text-[11px] text-zinc-700 italic px-3 py-1.5">{emptyHint}</p>
    )
  }

  const total = rows.length
  const running = rows.filter(t => t.status === 'running').length
  const errors = rows.filter(t => t.status === 'error').length
  const done = total - running - errors

  let summaryText: string
  if (running > 0) {
    summaryText = `${done}/${total} done · ${running} running${errors ? ` · ${errors} error${errors === 1 ? '' : 's'}` : ''}`
  } else if (errors > 0) {
    summaryText = `${done}/${total} done · ${errors} error${errors === 1 ? '' : 's'}`
  } else {
    summaryText = `${total} step${total === 1 ? '' : 's'}`
  }

  return (
    <div className="rounded-lg border border-[#1c1c1c] bg-[#0c0c0c] overflow-hidden">
      <button
        onClick={() => setOpen(o => !o)}
        className="w-full flex items-center gap-2 px-3 py-1.5 text-left text-[11px] hover:bg-[#101010] transition-colors"
      >
        <span
          className={`w-1.5 h-1.5 rounded-full flex-shrink-0 ${
            running > 0
              ? 'bg-indigo-400 animate-pulse'
              : errors > 0
                ? 'bg-red-500'
                : 'bg-emerald-500'
          }`}
        />
        <span className="text-zinc-400 font-medium tracking-wide">{label}</span>
        <span className="text-zinc-700">·</span>
        <span className="text-zinc-600">{summaryText}</span>
        <span className="ml-auto text-zinc-700">
          <svg
            width="9"
            height="9"
            viewBox="0 0 8 8"
            fill="none"
            className={`transition-transform duration-150 ${open ? 'rotate-180' : ''}`}
          >
            <path d="M1 2.5L4 5.5L7 2.5" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round" />
          </svg>
        </span>
      </button>

      {open && (
        <div className="border-t border-[#161616] px-3 py-2 space-y-1.5">
          {rows.map((tc, i) => (
            <ActivityRow key={`${tc.tool_name}-${i}`} tc={tc} />
          ))}
        </div>
      )}
    </div>
  )
}

function ActivityRow({ tc }: { tc: ToolCall }) {
  const [open, setOpen] = useState(false)
  const display = getToolDisplay(tc.tool_name)
  const cleaned = cleanToolSummary(tc.summary)
  const expandable = tc.status !== 'running' && cleaned.length > 0

  return (
    <div className="text-[11px]">
      <button
        onClick={() => expandable && setOpen(o => !o)}
        disabled={!expandable}
        className="w-full flex items-center gap-2 text-left text-zinc-500 hover:text-zinc-300 disabled:hover:text-zinc-500 transition-colors"
      >
        <span
          className={`w-1 h-1 rounded-full flex-shrink-0 ${
            tc.status === 'done'
              ? 'bg-emerald-500'
              : tc.status === 'error'
                ? 'bg-red-500'
                : 'bg-indigo-400 animate-pulse'
          }`}
        />
        <span
          className={`font-medium ${
            display.group === 'workflow' ? 'text-violet-300' : 'text-zinc-300'
          }`}
        >
          {display.label}
        </span>
        {tc.args_preview && (
          <span className="text-zinc-600 truncate min-w-0">"{tc.args_preview}"</span>
        )}
        {expandable && (
          <span className="ml-auto text-zinc-700 flex-shrink-0">
            <svg
              width="7"
              height="7"
              viewBox="0 0 8 8"
              fill="none"
              className={`transition-transform duration-150 ${open ? 'rotate-180' : ''}`}
            >
              <path d="M1 2.5L4 5.5L7 2.5" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round" />
            </svg>
          </span>
        )}
      </button>

      {open && cleaned && (
        <p className="ml-3 mt-1 pl-2 border-l border-[#222] text-zinc-600 leading-relaxed">
          {cleaned}
        </p>
      )}
    </div>
  )
}

/**
 * Persisted research-step timeline. Shows description + nested tool calls per
 * step. Used inside the committed research_report card.
 */
export function ResearchStepsTrace({
  steps,
  defaultOpen,
}: {
  steps: StepState[]
  defaultOpen?: boolean
}) {
  const [open, setOpen] = useState<boolean>(defaultOpen ?? false)
  const safeSteps = Array.isArray(steps) ? steps : []
  if (!safeSteps.length) return null

  const completed = safeSteps.filter(s => s.status === 'completed').length
  const failed = safeSteps.filter(s => s.status === 'failed').length
  const total = safeSteps.length

  return (
    <div className="rounded-lg border border-[#1c1c1c] bg-[#0c0c0c] overflow-hidden">
      <button
        onClick={() => setOpen(o => !o)}
        className="w-full flex items-center gap-2 px-3 py-1.5 text-left text-[11px] hover:bg-[#101010] transition-colors"
      >
        <span
          className={`w-1.5 h-1.5 rounded-full flex-shrink-0 ${
            failed > 0 ? 'bg-red-500' : 'bg-emerald-500'
          }`}
        />
        <span className="text-zinc-400 font-medium tracking-wide">Research plan</span>
        <span className="text-zinc-700">·</span>
        <span className="text-zinc-600">
          {completed}/{total} step{total === 1 ? '' : 's'}
          {failed ? ` · ${failed} failed` : ''}
        </span>
        <span className="ml-auto text-zinc-700">
          <svg
            width="9"
            height="9"
            viewBox="0 0 8 8"
            fill="none"
            className={`transition-transform duration-150 ${open ? 'rotate-180' : ''}`}
          >
            <path d="M1 2.5L4 5.5L7 2.5" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round" />
          </svg>
        </span>
      </button>

      {open && (
        <div className="border-t border-[#161616] px-3 py-2.5 space-y-2">
          {safeSteps.map((step, idx) => (
            <PersistedStepRow key={step.id || idx} step={step} index={idx} />
          ))}
        </div>
      )}
    </div>
  )
}

function PersistedStepRow({ step, index }: { step: StepState; index: number }) {
  const isComplete = step.status === 'completed'
  const isFailed = step.status === 'failed'
  const toolCalls = Array.isArray(step.tool_calls) ? step.tool_calls : []
  return (
    <div className="text-[11px]">
      <div className="flex items-start gap-2">
        <span
          className={`mt-1 w-1.5 h-1.5 rounded-full flex-shrink-0 ${
            isFailed ? 'bg-red-500' : isComplete ? 'bg-emerald-500' : 'bg-zinc-700'
          }`}
        />
        <span className="font-medium text-zinc-500 tabular-nums w-5 flex-shrink-0">
          {String(index + 1).padStart(2, '0')}
        </span>
        <span className="text-zinc-300 leading-relaxed">{step.description || 'Research step'}</span>
      </div>
      {toolCalls.length > 0 && (
        <div className="ml-7 mt-1 space-y-1">
          {toolCalls.map((tc, i) => (
            <ActivityRow key={`${step.id}-${i}`} tc={tc} />
          ))}
        </div>
      )}
    </div>
  )
}
