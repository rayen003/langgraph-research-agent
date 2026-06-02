import { useMemo } from 'react'
import { RotateCcw } from 'lucide-react'
import type { KgNode } from '../hooks/useKnowledgeGraph'
import { HUB_LEGEND } from './KgCanvas'
import { PanelHideButton } from './PanelHideButton'

interface Props {
  nodes: KgNode[]
  hiddenTickers: Set<string>
  onToggleTicker: (t: string) => void
  onResetFilters: () => void
  onHide?: () => void
}

export function KgFilterSidebar({ nodes, hiddenTickers, onToggleTicker, onResetFilters, onHide }: Props) {
  const { tickers, tickerCounts } = useMemo(() => {
    const tkc = new Map<string, number>()
    for (const n of nodes) {
      if (!n.ticker || n.ticker.startsWith('deck::')) continue
      tkc.set(n.ticker, (tkc.get(n.ticker) || 0) + 1)
    }
    return { tickers: Array.from(tkc.keys()).sort(), tickerCounts: tkc }
  }, [nodes])

  return (
    <div className="h-full w-full flex-shrink-0 border-r border-edge bg-surface overflow-y-auto">
      <div className="px-4 h-12 border-b border-edge flex items-center justify-between gap-2">
        <div className="text-[13px] font-medium text-ink">Tickers</div>
        <div className="flex items-center gap-0.5">
          <button
            onClick={onResetFilters}
            aria-label="Reset filters"
            className="flex items-center gap-1 text-[11px] text-ink-dim hover:text-ink transition"
          >
            <RotateCcw size={11} /> reset
          </button>
          {onHide && <PanelHideButton onHide={onHide} edge="left" />}
        </div>
      </div>

      {tickers.length > 0 && (
        <div className="px-4 py-3 border-b border-edge">
          <div className="flex flex-wrap gap-1.5">
            {tickers.map(t => {
              const hidden = hiddenTickers.has(t)
              return (
                <button
                  key={t}
                  onClick={() => onToggleTicker(t)}
                  className={`px-2 py-1 rounded-md text-[11px] font-mono border transition ${
                    hidden
                      ? 'bg-transparent text-ink-dim border-edge'
                      : 'bg-accent-soft text-accent border-accent/40'
                  } hover:border-accent/60`}
                >
                  {t || '—'} <span className="opacity-60 tabular-nums">{tickerCounts.get(t)}</span>
                </button>
              )
            })}
          </div>
        </div>
      )}

      <div className="px-4 py-3 space-y-2">
        <div className="text-[11px] uppercase tracking-wide text-ink-dim font-medium mb-1">Legend</div>
        {HUB_LEGEND.map(l => (
          <div key={l.label} className="flex items-center gap-2 text-[12px]">
            <span className="inline-block w-2 h-2 rounded-full flex-shrink-0" style={{ backgroundColor: l.color }} />
            <span className="text-ink-muted">{l.label}</span>
          </div>
        ))}
        <p className="text-[11px] text-ink-dim pt-2 leading-relaxed">
          Click a hub to inspect. Drag to reposition. Double-click resets a node.
        </p>
      </div>
    </div>
  )
}
