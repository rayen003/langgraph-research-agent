import { type ReactNode } from 'react'
import { Check, X, Loader2, Minus, Pause, ChevronDown } from 'lucide-react'
import type { ActivityEntry, ActivityKind, ActivityStatus } from '../../lib/activity'

/**
 * One activity block — a soft, borderless card (depth from surface fill, not
 * 1px lines). Icon-led header + muted summary line; click to reveal the full
 * detail in a nested soft panel. Airy padding, generous rhythm. Status by icon
 * (never colour alone). Appears with a staggered fade-up; honours
 * prefers-reduced-motion via the global media query.
 *
 * Presentational + controlled: BlockStack owns open state, nesting, stagger.
 */

type Group = 'workflow' | 'tool' | 'kg' | 'node'

interface Props {
  entry: ActivityEntry
  open: boolean
  onToggle: () => void
  index?: number
  compact?: boolean
  children?: ReactNode
}

// Group → icon tint. Subtle, not a hard spine.
const ICON_TINT: Record<Group, string> = {
  workflow: 'text-violet-300',
  tool: 'text-sky-300',
  kg: 'text-teal-300',
  node: 'text-ink-muted',
}

function groupOf(kind: ActivityKind, name: string): Group {
  if (kind === 'workflow' || kind === 'workflow_step') return 'workflow'
  if (name === 'query_knowledge_graph' || name.startsWith('kg_')) return 'kg'
  if (kind === 'node') return 'node'
  return 'tool'
}

/** Humanize a raw activity name: strip the `workflow:dcf:` prefix, snake→words,
 *  title-case. `workflow:dcf:normalize_input` → `Normalize input`. */
function humanizeLabel(entry: ActivityEntry): string {
  if (entry.display_label) return entry.display_label
  let s = entry.name
  s = s.replace(/^workflow:[a-z]+:/i, '').replace(/^workflow:/i, '')
  s = s.replace(/[_:]+/g, ' ').trim()
  return s.charAt(0).toUpperCase() + s.slice(1)
}

function StatusIcon({ status, tint }: { status: ActivityStatus; tint: string }) {
  switch (status) {
    case 'completed':
      return <Check size={14} className="text-up" aria-hidden />
    case 'error':
      return <X size={14} className="text-down" aria-hidden />
    case 'skipped':
      return <Minus size={14} className="text-ink-dim" aria-hidden />
    case 'awaiting_input':
      return <Pause size={14} className="text-warn" aria-hidden />
    default:
      return <Loader2 size={14} className={`${tint} animate-spin`} aria-hidden />
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
  entry, open, onToggle, index = 0, compact = false, children,
}: Props) {
  const group = groupOf(entry.kind, entry.name)
  const tint = ICON_TINT[group]
  const label = humanizeLabel(entry)
  const duration = durationLabel(entry)
  const running = entry.status === 'running' || entry.status === 'started'
  const hasBody = !!children
  const summary = (entry.summary || '').trim()

  return (
    <div
      className="animate-fade-up"
      style={{ animationDelay: `${Math.min(index, 10) * 35}ms` }}
    >
      {/* Compact pill — single line, soft fill, no hard border */}
      <button
        type="button"
        onClick={hasBody ? onToggle : undefined}
        disabled={!hasBody}
        aria-expanded={hasBody ? open : undefined}
        className={`group w-full flex items-center gap-2.5 rounded-lg px-3 py-2 text-left transition-colors ${
          running ? 'bg-surface-2' : 'bg-surface-2/40 hover:bg-surface-2'
        } ${hasBody ? 'cursor-pointer' : 'cursor-default'}`}
      >
        <StatusIcon status={entry.status} tint={tint} />
        <span className="text-[12px] text-ink truncate min-w-0 flex-1">{label}</span>
        <span className="flex items-center gap-2 flex-shrink-0 text-[10px] text-ink-dim tabular-nums">
          {typeof entry.flag_count === 'number' && entry.flag_count > 0 && (
            <span className="text-warn">⚑ {entry.flag_count}</span>
          )}
          {entry.confidence_label && <span className="uppercase tracking-wide">{entry.confidence_label}</span>}
          {duration && <span>{duration}</span>}
          {hasBody && (
            <ChevronDown size={12} className={`transition-transform duration-150 ${open ? 'rotate-180' : ''}`} />
          )}
        </span>
      </button>

      {/* One-line summary under the pill when collapsed (keeps the list scannable
          + exhaustive without bloating the pill). */}
      {!open && summary && (
        <div className="mt-0.5 ml-[26px] text-[11px] text-ink-dim leading-snug truncate">{summary}</div>
      )}

      {/* Expanded detail — soft panel, indented, full content (no clip) */}
      {hasBody && open && (
        <div className="mt-1.5 ml-[26px] mr-0.5 rounded-md bg-bg-overlay px-3 py-2.5 animate-fade-up text-[12px] leading-relaxed">
          {children}
        </div>
      )}

      {/* Error line */}
      {entry.status === 'error' && entry.error && (
        <div className="mt-1 ml-[26px] text-[11px] text-down leading-snug">{entry.error}</div>
      )}
    </div>
  )
}
