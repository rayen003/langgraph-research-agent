export interface RerunToastState {
  id: string
  ticker: string
  threadId: string | null
  sessionId: string
  target: 'current' | 'new'
  status: 'running' | 'complete' | 'error'
  error?: string
  createdAt: number
}

interface Props {
  toast: RerunToastState
  /** Fired when complete: switches to target session + closes KG. */
  onView: () => void
  onDismiss: () => void
  /**
   * Fired while running when the user clicks the toast body. Closes the KG
   * modal so the ExecutionSidebar (steps + activity) is visible.
   */
  onInspect?: () => void
}

export function RerunToast({ toast, onView, onDismiss, onInspect }: Props) {
  const status = toast.status
  const isComplete = status === 'complete'
  const isError = status === 'error'

  const bg =
    isComplete ? 'bg-emerald-500/15 border-emerald-500/40 text-emerald-200' :
    isError ? 'bg-red-500/15 border-red-500/40 text-red-200' :
    'bg-indigo-500/15 border-indigo-500/40 text-indigo-200'

  const dot =
    isComplete ? 'bg-emerald-400' :
    isError ? 'bg-red-400' :
    'bg-indigo-400 animate-pulse'

  const headline =
    isComplete ? `✓ DCF rerun complete · ${toast.ticker}` :
    isError ? `⚠ DCF rerun failed · ${toast.ticker}` :
    `DCF rerunning · ${toast.ticker}`

  const subtext =
    isError ? (toast.error || 'unknown error') :
    toast.target === 'new' ? 'in new chat' : 'in current chat'

  return (
    <div
      className={`fixed bottom-16 right-4 z-[55] w-[300px] rounded-md border shadow-xl ${bg}`}
    >
      <button
        type="button"
        onClick={() => { if (status === 'running' && onInspect) onInspect() }}
        disabled={status !== 'running'}
        className={`w-full text-left px-3 py-2.5 flex items-start gap-2 ${
          status === 'running' && onInspect ? 'hover:bg-white/5 cursor-pointer' : 'cursor-default'
        }`}
        title={status === 'running' ? 'Inspect steps + activity' : ''}
      >
        <span className={`inline-block w-2 h-2 rounded-full mt-1.5 flex-shrink-0 ${dot}`} />
        <div className="flex-1 min-w-0">
          <div className="text-[12px] font-medium truncate">{headline}</div>
          <div className="text-[10px] opacity-80 truncate">
            {subtext}{status === 'running' && onInspect ? ' · click to inspect →' : ''}
          </div>
        </div>
        <span
          role="button"
          tabIndex={0}
          onClick={(e) => { e.stopPropagation(); onDismiss() }}
          onKeyDown={(e) => { if (e.key === 'Enter') { e.stopPropagation(); onDismiss() } }}
          className="text-ink-dim hover:text-ink-muted text-[12px] px-1"
          title="dismiss"
        >
          ✕
        </span>
      </button>

      {isComplete && (
        <div className="px-3 pb-2.5">
          <button
            onClick={onView}
            className="w-full text-[10px] px-2 py-1 rounded bg-emerald-500/25 hover:bg-emerald-500/40 text-emerald-100 border border-emerald-500/50"
          >
            View chat →
          </button>
        </div>
      )}
    </div>
  )
}
