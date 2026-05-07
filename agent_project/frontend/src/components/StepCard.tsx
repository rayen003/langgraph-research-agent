import { useState } from 'react'
import type { StepState, ToolCall } from '../types'
import { cleanToolSummary, getToolDisplay } from '../lib/toolLabels'

function ToolRow({ tc }: { tc: ToolCall }) {
  const [expanded, setExpanded] = useState(false)
  const cleanedSummary = cleanToolSummary(tc.summary, 240)
  const canExpand = tc.status !== 'running' && !!cleanedSummary
  const display = getToolDisplay(tc.tool_name)
  const argsPreview = tc.args_preview || ''

  return (
    <div className="animate-slide-in">
      <button
        className="w-full flex items-center gap-2 py-[3px] text-left group"
        onClick={() => canExpand && setExpanded(v => !v)}
        disabled={!canExpand}
      >
        {/* Status dot */}
        <span
          className={`flex-shrink-0 w-1 h-1 rounded-full ${
            tc.status === 'done'
              ? 'bg-emerald-500'
              : tc.status === 'error'
              ? 'bg-red-500'
              : 'bg-indigo-400 animate-pulse'
          }`}
        />

        {/* Tool name */}
        <span
          className={`text-[11px] font-medium leading-snug flex-shrink-0 ${
            display.group === 'workflow' ? 'text-violet-300' : 'text-zinc-400'
          }`}
        >
          {display.label}
        </span>

        {/* Args preview */}
        {argsPreview && (
          <span className="text-[11px] text-zinc-700 truncate min-w-0">
            "{argsPreview}"
          </span>
        )}

        {/* Expand toggle */}
        {canExpand && (
          <span className="ml-auto flex-shrink-0 text-zinc-700 opacity-0 group-hover:opacity-100 transition-opacity">
            <svg
              width="8"
              height="8"
              viewBox="0 0 8 8"
              fill="none"
              className={`transition-transform duration-150 ${expanded ? 'rotate-180' : ''}`}
            >
              <path d="M1 2.5L4 5.5L7 2.5" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round" />
            </svg>
          </span>
        )}
      </button>

      {expanded && cleanedSummary && (
        <p className="ml-6 mb-1 text-[11px] text-zinc-700 leading-relaxed border-l border-[#252525] pl-2.5">
          {cleanedSummary}
        </p>
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
    <div className="relative flex gap-3">
      {/* Vertical connector line */}
      {!isLast && (
        <div className="absolute left-[6px] top-[14px] bottom-0 w-px bg-[#1e1e1e]" />
      )}

      {/* Step indicator dot */}
      <div className="flex-shrink-0 mt-[2px] z-10">
        <div
          className={`
            w-3.5 h-3.5 rounded-full border flex items-center justify-center
            transition-all duration-500
            ${isRunning
              ? 'border-indigo-500 bg-indigo-500/15 animate-pulse-ring'
              : isComplete
              ? 'border-emerald-600 bg-emerald-600'
              : isFailed
              ? 'border-red-600 bg-red-600/20'
              : 'border-[#2a2a2a] bg-transparent'
            }
          `}
        >
          {isComplete && (
            <svg width="6" height="6" viewBox="0 0 6 6" fill="none">
              <path d="M1 3L2.5 4.5L5 1.5" stroke="white" strokeWidth="1.2" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
          )}
          {isFailed && (
            <svg width="6" height="6" viewBox="0 0 6 6" fill="none">
              <path d="M1.5 1.5L4.5 4.5M4.5 1.5L1.5 4.5" stroke="#ef4444" strokeWidth="1.2" strokeLinecap="round" />
            </svg>
          )}
        </div>
      </div>

      {/* Step body */}
      <div
        className={`
          flex-1 min-w-0 pb-5 transition-all duration-300
          ${isRunning ? 'animate-fade-up' : ''}
        `}
      >
        {/* Step number + description */}
        <div className="flex items-start gap-1.5">
          <span
            className={`text-[11px] flex-shrink-0 font-medium tabular-nums mt-0.5
              ${isRunning ? 'text-indigo-400' : isComplete ? 'text-zinc-700' : 'text-[#333]'}
            `}
          >
            {String(index + 1).padStart(2, '0')}
          </span>
          <p
            className={`text-xs leading-relaxed transition-colors duration-300
              ${isRunning ? 'text-zinc-200' : isComplete ? 'text-zinc-500' : 'text-[#3a3a3a]'}
            `}
          >
            {description}
          </p>
        </div>

        {/* Tool calls (shown when running or complete) */}
        {(isRunning || isComplete) && toolCalls.length > 0 && (
          <div className="mt-2 ml-5 space-y-0">
            {toolCalls.map((tc, i) => (
              <ToolRow key={i} tc={tc} />
            ))}
          </div>
        )}
      </div>
    </div>
  )
}
