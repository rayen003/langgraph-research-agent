interface Props {
  ticker: string
  currentSessionTitle: string
  onPickCurrent: () => void
  onPickNew: () => void
  onCancel: () => void
}

export function KgRerunPicker({ ticker, currentSessionTitle, onPickCurrent, onPickNew, onCancel }: Props) {
  return (
    <div
      className="fixed inset-0 z-[60] bg-black/60 flex items-center justify-center"
      onClick={onCancel}
    >
      <div
        className="bg-surface border border-edge rounded-lg shadow-2xl w-[440px] p-5"
        onClick={e => e.stopPropagation()}
      >
        <div className="text-ink text-[14px] font-medium">Rerun DCF · {ticker}</div>
        <div className="text-ink-dim text-[12px] mt-1">Where should this rerun execute?</div>

        <div className="mt-4 space-y-2">
          <button
            onClick={onPickCurrent}
            className="w-full text-left px-3 py-2.5 rounded-md border bg-accent-soft border-accent/40 text-ink hover:bg-accent/15 transition"
          >
            <div className="text-[13px] font-medium text-accent">Current chat</div>
            <div className="text-[11px] text-ink-dim mt-0.5 truncate">{currentSessionTitle}</div>
          </button>

          <button
            onClick={onPickNew}
            className="w-full text-left px-3 py-2.5 rounded-md border border-edge text-ink hover:bg-surface-2 transition"
          >
            <div className="text-[13px] font-medium">New chat</div>
            <div className="text-[11px] text-ink-dim mt-0.5">
              Fresh thread — preserves current chat for comparison
            </div>
          </button>
        </div>

        <div className="mt-4 flex justify-end">
          <button onClick={onCancel} className="text-[12px] text-ink-dim hover:text-ink px-2 transition">
            Cancel
          </button>
        </div>
      </div>
    </div>
  )
}
