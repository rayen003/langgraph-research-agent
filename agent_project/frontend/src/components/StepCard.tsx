import { useState } from 'react'
import type { StepState, ToolCall } from '../types'
import { cleanToolSummary, getToolDisplay } from '../lib/toolLabels'

/** Try to parse and format a JSON args preview into a cleaner label. */
export function fmtArgsPreview(raw: string): string {
  if (!raw) return ''
  try {
    const obj = JSON.parse(raw)
    // Extract the most informative single field
    const ticker = obj.ticker ? String(obj.ticker).toUpperCase() : ''
    const mode = obj.assumption_review_mode ? 'review' : 'execute'
    const overrides = obj.assumption_overrides ? `${Object.keys(obj.assumption_overrides).length} overrides` : ''
    const horizon = obj.horizon_years ? `${obj.horizon_years}y` : ''
    const parts = [ticker, mode, overrides, horizon].filter(Boolean)
    return parts.length > 0 ? parts.join(' · ') : ''
  } catch {
    // Not JSON — truncate and clean quotes
    const cleaned = raw.replace(/["\{\}]/g, '').trim()
    return cleaned.length > 60 ? cleaned.slice(0, 60) + '…' : cleaned
  }
}

function ToolRow({ tc, isLast }: { tc: ToolCall; isLast?: boolean }) {
  const [expanded, setExpanded] = useState(false)
  const cleanedSummary = cleanToolSummary(tc.summary, 240)
  const canExpand = tc.status !== 'running' && !!cleanedSummary
  const display = getToolDisplay(tc.tool_name)
  const argsLabel = fmtArgsPreview(tc.args_preview || '')
  const isDone = tc.status === 'done'
  const isError = tc.status === 'error'
  const isRunning = tc.status === 'running'

  return (
    <div className="text-[11px]">
      {/* Vertical connector line for tool call timeline */}
      {!isLast && (
        <div className={`ml-[9px] w-px h-2 ${isDone ? 'bg-emerald-600/20' : 'bg-border'}`} />
      )}
      <div className="flex items-center gap-2 py-0.5">
        {/* Timeline node — circle matching step indicator style */}
        <div className={`flex-shrink-0 w-[19px] h-[19px] rounded-full border flex items-center justify-center ${
          isDone ? 'border-emerald-500/30 bg-emerald-500/10' :
          isError ? 'border-red-500/30 bg-red-500/10' :
          'border-indigo-400/20 bg-indigo-400/5'
        }`}>
          {isDone && (
            <svg width="10" height="10" viewBox="0 0 12 12" fill="none">
              <path d="M2.5 6L5 8.5L9.5 3.5" stroke="currentColor" className="text-emerald-400" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"/>
            </svg>
          )}
          {isRunning && (
            <div className="w-[6px] h-[6px] rounded-full bg-indigo-400 animate-pulse" />
          )}
          {isError && (
            <svg width="8" height="8" viewBox="0 0 8 8" fill="none">
              <path d="M2 2L6 6M6 2L2 6" stroke="currentColor" className="text-red-400" strokeWidth="1.2" strokeLinecap="round"/>
            </svg>
          )}
        </div>

        {/* Tool label + args */}
        <div className="flex-1 min-w-0 flex items-center gap-1.5">
          <span className={`font-medium ${isDone ? 'text-ink-muted' : isRunning ? 'text-indigo-300' : 'text-ink-muted'}`}>
            {display.label}
          </span>
          {argsLabel && (
            <span className="text-ink-dim truncate">{argsLabel}</span>
          )}
        </div>

        {/* Expand toggle */}
        {canExpand && (
          <button
            onClick={() => setExpanded(v => !v)}
            className="flex-shrink-0 text-ink-dim hover:text-ink-muted"
          >
            <svg width="6" height="6" viewBox="0 0 8 8" fill="none" className={`transition-transform duration-150 ${expanded ? 'rotate-180' : ''}`}>
              <path d="M1 2.5L4 5.5L7 2.5" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round" />
            </svg>
          </button>
        )}
      </div>

      {expanded && cleanedSummary && (
        <div className="ml-7 mb-1 text-ink-dim leading-relaxed border-l border-border pl-2.5">
          {cleanedSummary}
        </div>
      )}
    </div>
  )
}

interface Props {
  step: StepState
  index: number
  isLast: boolean
}

export function StepCard({ step, index, isLast }: Props) {
  const status = step.status || 'pending'
  const isRunning = status === 'running'
  const isComplete = status === 'completed'
  const isFailed = status === 'failed'
  const toolCalls = Array.isArray(step.tool_calls) ? step.tool_calls : []
  const description = step.description || 'Research step'

  return (
    <div className="relative flex gap-3 group">
      {/* Vertical connector line */}
      {!isLast && (
        <div className={`absolute left-[9px] top-5 bottom-0 w-px ${
          isComplete ? 'bg-emerald-600/30' : 'bg-border'
        }`} />
      )}

      {/* Timeline node */}
      <div className="flex-shrink-0 mt-[3px] z-10">
        <div className={`w-[19px] h-[19px] rounded-full border flex items-center justify-center transition-all duration-300 ${
          isRunning ? 'border-indigo-400/50 bg-indigo-400/10' :
          isComplete ? 'border-emerald-500/40 bg-emerald-500/10' :
          isFailed ? 'border-red-500/40 bg-red-500/10' :
          'border-border bg-transparent'
        }`}>
          {isComplete && (
            <svg width="10" height="10" viewBox="0 0 12 12" fill="none">
              <path d="M2.5 6L5 8.5L9.5 3.5" stroke="currentColor" className="text-emerald-400" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"/>
            </svg>
          )}
          {isRunning && (
            <div className="w-[6px] h-[6px] rounded-full bg-indigo-400 animate-pulse" />
          )}
          {isFailed && (
            <svg width="8" height="8" viewBox="0 0 8 8" fill="none">
              <path d="M2 2L6 6M6 2L2 6" stroke="currentColor" className="text-red-400" strokeWidth="1.2" strokeLinecap="round"/>
            </svg>
          )}
        </div>
      </div>

      {/* Content */}
      <div className="flex-1 min-w-0 pb-3 transition-opacity duration-300">
        {/* Step description */}
        <div className="flex items-center gap-2">
          <span className={`text-[11px] font-medium leading-snug ${
            isRunning ? 'text-indigo-300' :
            isComplete ? 'text-ink-muted' :
            isFailed ? 'text-red-300' :
            'text-ink-dim'
          }`}>
            {description}
          </span>
          {/* Count badge — shown during running and after completion */}
          {toolCalls.length > 0 && (
            <span className="text-[10px] text-ink-dim">
              {toolCalls.filter(t => t.status === 'done').length}/{toolCalls.length} done
              {toolCalls.filter(t => t.status === 'running').length > 0 && ` · ${toolCalls.filter(t => t.status === 'running').length} running`}
            </span>
          )}
        </div>

        {/* Tool calls */}
        {(isRunning || isComplete) && toolCalls.length > 0 && (
          <div className="mt-1 space-y-0">
            {toolCalls.map((tc, i) => (
              <ToolRow key={i} tc={tc} isLast={i === toolCalls.length - 1} />
            ))}
          </div>
        )}
      </div>
    </div>
  )
}
