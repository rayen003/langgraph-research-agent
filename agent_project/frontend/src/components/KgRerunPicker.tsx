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
        className="bg-[#11111a] border border-[#2a2a36] rounded-md shadow-2xl w-[460px] p-5"
        onClick={e => e.stopPropagation()}
      >
        <div className="text-zinc-200 text-sm font-medium">Rerun DCF for {ticker}</div>
        <div className="text-zinc-500 text-[11px] mt-1">
          Where should this rerun execute?
        </div>

        <div className="mt-4 space-y-2">
          <button
            onClick={onPickCurrent}
            className="w-full text-left px-3 py-2.5 rounded border bg-indigo-500/10 border-indigo-500/40 text-indigo-200 hover:bg-indigo-500/20 transition"
          >
            <div className="text-[12px] font-medium">Current chat</div>
            <div className="text-[10px] text-zinc-500 mt-0.5 truncate">
              {currentSessionTitle}
            </div>
          </button>

          <button
            onClick={onPickNew}
            className="w-full text-left px-3 py-2.5 rounded border bg-emerald-500/10 border-emerald-500/40 text-emerald-200 hover:bg-emerald-500/20 transition"
          >
            <div className="text-[12px] font-medium">New chat</div>
            <div className="text-[10px] text-zinc-500 mt-0.5">
              Fresh thread — preserves current chat for comparison
            </div>
          </button>
        </div>

        <div className="mt-4 flex justify-end">
          <button
            onClick={onCancel}
            className="text-[11px] text-zinc-500 hover:text-zinc-300 px-2"
          >
            Cancel
          </button>
        </div>
      </div>
    </div>
  )
}
