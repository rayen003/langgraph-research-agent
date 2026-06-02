import { useState } from 'react'
import type { JobSummary } from '../types'
import { useResizable } from '../hooks/useResizable'

const STATUS_COLOR: Record<string, string> = {
  classifying: 'bg-zinc-500',
  planning: 'bg-indigo-500 animate-pulse',
  awaiting_approval: 'bg-amber-500 animate-pulse',
  executing: 'bg-indigo-500 animate-pulse',
  synthesizing: 'bg-violet-500 animate-pulse',
  complete: 'bg-emerald-500',
  error: 'bg-red-500',
  rejected: 'bg-zinc-600',
  chat_responding: 'bg-cyan-500 animate-pulse',
}

const STATUS_LABEL: Record<string, string> = {
  classifying: 'Classifying',
  planning: 'Planning',
  awaiting_approval: 'Awaiting approval',
  executing: 'Executing',
  synthesizing: 'Writing report',
  complete: 'Complete',
  error: 'Error',
  rejected: 'Rejected',
  chat_responding: 'Responding',
}

function timeAgo(iso: string): string {
  const diff = Date.now() - new Date(iso).getTime()
  const s = Math.floor(diff / 1000)
  if (s < 60) return `${s}s ago`
  const m = Math.floor(s / 60)
  if (m < 60) return `${m}m ago`
  return `${Math.floor(m / 60)}h ago`
}

interface Props {
  jobs: JobSummary[]
  runningCount: number
  onSelectJob: (job: JobSummary) => void
}

export function JobsPanel({ jobs, runningCount, onSelectJob }: Props) {
  const [open, setOpen] = useState(false)
  const { width, handleProps } = useResizable({
    defaultWidth: 320,
    minWidth: 260,
    maxWidth: 520,
    side: 'left',
    storageKey: 'ui.jobsPanelWidth',
  })

  if (jobs.length === 0) return null

  return (
    <div className="fixed bottom-6 right-6 z-50">
      {/* Floating button */}
      <button
        onClick={() => setOpen(v => !v)}
        className="
          flex items-center gap-2 px-3 py-2 rounded-xl
          bg-bg-overlay border border-border-hover
          hover:border-border-hover transition-colors duration-150
          shadow-lg shadow-black/40
        "
      >
        <svg width="13" height="13" viewBox="0 0 13 13" fill="none">
          <rect x="1" y="1" width="5" height="5" rx="1.2" stroke="var(--color-accent-muted)" strokeWidth="1.2" />
          <rect x="7" y="1" width="5" height="5" rx="1.2" stroke="var(--color-accent-muted)" strokeWidth="1.2" />
          <rect x="1" y="7" width="5" height="5" rx="1.2" stroke="var(--color-accent-muted)" strokeWidth="1.2" />
          <rect x="7" y="7" width="5" height="5" rx="1.2" stroke="var(--color-accent-muted)" strokeWidth="1.2" />
        </svg>
        <span className="text-xs text-ink-muted font-medium">Research</span>
        {runningCount > 0 ? (
          <span className="flex h-4 w-4 items-center justify-center rounded-full bg-indigo-600 text-[10px] text-white font-medium">
            {runningCount}
          </span>
        ) : (
          <span className="flex h-4 w-4 items-center justify-center rounded-full bg-surface-3 border border-border-hover text-[10px] text-ink-dim font-medium">
            {jobs.length}
          </span>
        )}
      </button>

      {/* Panel */}
      {open && (
        <div
          data-resizable
          style={{ width }}
          className="
            absolute bottom-12 right-0 relative
            bg-bg border border-border rounded-xl shadow-2xl shadow-black/60
            animate-fade-up
          "
        >
          <div
            role="separator"
            aria-orientation="vertical"
            aria-label="Resize panel"
            title="Drag to resize · double-click to reset"
            {...handleProps}
            className="absolute top-0 bottom-0 left-0 w-3 -translate-x-1/2 z-50 cursor-col-resize touch-none select-none group"
          >
            <div className="absolute inset-y-0 left-1/2 w-px -translate-x-1/2 bg-surface-3/60 group-hover:bg-indigo-400/70 group-active:bg-indigo-400 transition-colors" />
          </div>
          <div className="overflow-hidden rounded-xl">
          <div className="px-4 py-3 border-b border-border flex items-center justify-between">
            <span className="text-[11px] font-medium text-ink-dim tracking-widest uppercase">Research Jobs</span>
            <button onClick={() => setOpen(false)} className="text-zinc-700 hover:text-ink-muted">
              <svg width="12" height="12" viewBox="0 0 12 12" fill="none">
                <path d="M2 2L10 10M10 2L2 10" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" />
              </svg>
            </button>
          </div>

          <div className="max-h-72 overflow-y-auto divide-y divide-border-subtle">
            {jobs.map(job => (
              <button
                key={job.thread_id}
                onClick={() => { onSelectJob(job); setOpen(false) }}
                className="w-full px-4 py-3 text-left hover:bg-bg-overlay transition-colors duration-100"
              >
                <div className="flex items-start gap-2.5">
                  <div className={`mt-1.5 w-1.5 h-1.5 rounded-full flex-shrink-0 ${STATUS_COLOR[job.status] ?? 'bg-zinc-600'}`} />
                  <div className="flex-1 min-w-0 space-y-0.5">
                    <p className="text-xs text-ink-muted leading-snug truncate">{job.query}</p>
                    <div className="flex items-center gap-2">
                      <span className="text-[10px] text-zinc-700">
                        {STATUS_LABEL[job.status] ?? job.status}
                      </span>
                      <span className="text-[10px] text-zinc-800">·</span>
                      <span className="text-[10px] text-zinc-800">{timeAgo(job.created_at)}</span>
                    </div>
                  </div>
                  {job.status === 'complete' && (
                    <svg width="10" height="10" viewBox="0 0 10 10" fill="none" className="flex-shrink-0 mt-1 text-zinc-700">
                      <path d="M2 5H8M5 2L8 5L5 8" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round" strokeLinejoin="round" />
                    </svg>
                  )}
                  {job.status === 'awaiting_approval' && (
                    <span className="flex-shrink-0 mt-0.5 text-[10px] text-amber-500 font-medium">Review</span>
                  )}
                </div>
              </button>
            ))}
          </div>
          </div>
        </div>
      )}
    </div>
  )
}
