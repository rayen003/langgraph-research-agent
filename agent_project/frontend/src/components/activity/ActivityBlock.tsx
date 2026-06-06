import { type ReactNode } from 'react'
import { Check, X, Loader2, Minus, Pause } from 'lucide-react'
import type { ActivityEntry, ActivityKind, ActivityStatus } from '../../lib/activity'

/**
 * One stacked activity block — bordered rectangle with a group-colored left
 * spine, a status-bearing header, and a collapsible body. Status is conveyed by
 * icon + label + border tint (never colour alone). Appears with a staggered
 * fade-up; honours prefers-reduced-motion via the global media query.
 *
 * Presentational + controlled: the parent (BlockStack) owns open/collapse state
 * and the stagger index.
 */

type Group = 'workflow' | 'tool' | 'kg' | 'node'

interface Props {
  entry: ActivityEntry
  open: boolean
  onToggle: () => void
  /** Stagger index for the appear animation (40ms each). */
  index?: number
  /** Compact = inline-in-chat tool variant (tighter padding, smaller body). */
  compact?: boolean
  /** Nested child block (workflow substep under its parent). */
  nested?: boolean
  children?: ReactNode
}

// Group → left-spine colour. KG teal, workflow violet, tools slate, nodes zinc.
const SPINE: Record<Group, string> = {
  workflow: 'bg-violet-400/70',
  tool: 'bg-slate-400/60',
  kg: 'bg-teal-400/70',
  node: 'bg-zinc-500/50',
}

function groupOf(kind: ActivityKind, name: string): Group {
  if (kind === 'workflow' || kind === 'workflow_step') return 'workflow'
  if (name === 'query_knowledge_graph' || name.startsWith('kg_')) return 'kg'
  if (kind === 'node') return 'node'
  return 'tool'
}

function StatusIcon({ status }: { status: ActivityStatus }) {
  switch (status) {
    case 'completed':
      return <Check size={13} className="text-up" aria-hidden />
    case 'error':
      return <X size={13} className="text-down" aria-hidden />
    case 'skipped':
      return <Minus size={13} className="text-ink-dim" aria-hidden />
    case 'awaiting_input':
      return <Pause size={13} className="text-warn" aria-hidden />
    default:
      return <Loader2 size={13} className="text-accent animate-spin" aria-hidden />
  }
}

function durationLabel(entry: ActivityEntry): string {
  if (entry.started_at == null || entry.ended_at == null) return ''
  const s = Math.max(0, entry.ended_at - entry.started_at)
  if (s < 1) return `${Math.round(s * 1000)}ms`
  if (s < 60) return `${s.toFixed(1)}s`
  return `${Math.floor(s / 60)}m ${Math.round(s % 60)}s`
}

export function ActivityBlock({
  entry, open, onToggle, index = 0, compact = false, nested = false, children,
}: Props) {
  const group = groupOf(entry.kind, entry.name)
  const label = entry.display_label || entry.name
  const duration = durationLabel(entry)
  const running = entry.status === 'running' || entry.status === 'started'
  const hasBody = !!children
  const summary = (entry.summary || '').trim()

  // Border tint by status (structure, not colour-only — paired with the icon).
  const borderClass = entry.status === 'error'
    ? 'border-down/40'
    : running
      ? 'border-accent/40'
      : 'border-edge'

  return (
    <div
      className={`group/block animate-fade-up rounded-lg border ${borderClass} bg-surface overflow-hidden ${
        nested ? 'ml-3' : ''
      }`}
      style={{ animationDelay: `${Math.min(index, 8) * 40}ms` }}
    >
      <div className="flex">
        {/* Group spine */}
        <span className={`w-[2px] flex-shrink-0 ${SPINE[group]}`} aria-hidden />

        <div className="flex-1 min-w-0">
          {/* Header */}
          <button
            type="button"
            onClick={hasBody ? onToggle : undefined}
            disabled={!hasBody}
            aria-expanded={hasBody ? open : undefined}
            className={`w-full flex items-center gap-2.5 text-left ${
              compact ? 'px-3 py-2' : 'px-4 py-2.5'
            } ${hasBody ? 'hover:bg-surface-2/60 transition-colors cursor-pointer' : 'cursor-default'}`}
          >
            <span className="flex-shrink-0"><StatusIcon status={entry.status} /></span>
            <span className={`flex-shrink-0 font-medium ${compact ? 'text-[11px]' : 'text-[12px]'} ${
              group === 'workflow' ? 'text-violet-200' : 'text-ink'
            }`}>
              {label}
            </span>
            {entry.args_preview && (
              <span className="text-[11px] text-ink-dim truncate min-w-0">{entry.args_preview}</span>
            )}
            <span className="ml-auto flex items-center gap-2 flex-shrink-0 text-[10px] text-ink-dim tabular-nums">
              {typeof entry.flag_count === 'number' && entry.flag_count > 0 && (
                <span className="text-warn">⚑ {entry.flag_count}</span>
              )}
              {entry.confidence_label && (
                <span className="uppercase tracking-wide">{entry.confidence_label}</span>
              )}
              {duration && <span>{duration}</span>}
              {hasBody && (
                <svg width="9" height="9" viewBox="0 0 8 8" fill="none"
                  className={`transition-transform duration-150 text-ink-dim ${open ? 'rotate-180' : ''}`}>
                  <path d="M1 2.5L4 5.5L7 2.5" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round" />
                </svg>
              )}
            </span>
          </button>

          {/* One-line summary (always visible when collapsed + present) */}
          {!open && summary && (
            <div className={`${compact ? 'px-3 pb-2' : 'px-4 pb-2.5'} -mt-1 text-[11px] text-ink-muted truncate`}>
              {summary}
            </div>
          )}

          {/* Collapsible body — height-animated via grid-rows trick */}
          {hasBody && (
            <div className={`grid transition-all duration-200 ease-out ${
              open ? 'grid-rows-[1fr] opacity-100' : 'grid-rows-[0fr] opacity-0'
            }`}>
              <div className="overflow-hidden">
                <div className={`${compact ? 'px-3 pb-3' : 'px-4 pb-3.5'} pt-0.5 border-t border-edge/60`}>
                  {children}
                </div>
              </div>
            </div>
          )}

          {/* Error line */}
          {entry.status === 'error' && entry.error && (
            <div className={`${compact ? 'px-3 pb-2' : 'px-4 pb-2.5'} text-[11px] text-down`}>
              {entry.error}
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
