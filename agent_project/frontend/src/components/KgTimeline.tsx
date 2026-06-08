import { useMemo, useRef, useState } from 'react'
import { Clock, ChevronDown } from 'lucide-react'
import type { KgNode } from '../hooks/useKnowledgeGraph'
import { useResizableHeight } from '../hooks/useResizable'

interface Props {
  nodes: KgNode[]
  onSelectRun: (runNode: KgNode) => void
  /** dcf_run ids currently highlighted (query match) → glow the dot. */
  highlightRunIds?: Set<string>
}

interface RunDot {
  node: KgNode
  ticker: string
  ts: number
  label: string
  impliedPrice?: number
}

interface RunCluster {
  key: string
  ts: number
  label: string
  runs: RunDot[]
  tickers: string[]
  hot: boolean
}

// Desaturated, cohesive ticker palette — identity without the candy look.
const TICKER_COLORS = ['#3b82f6', '#0d9488', '#6366f1', '#64748b', '#0ea5e9', '#8b5cf6']

function fmtDate(ts: number): string {
  return new Date(ts * 1000).toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
}

function dayKey(ts: number): string {
  const d = new Date(ts * 1000)
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
}

/**
 * Horizontal timeline of DCF runs across all tickers, ordered by date. Each run
 * is a dot positioned by its timestamp; color = ticker. Click → open inspector.
 * Collapsible; drag the strip below the header to resize height when expanded.
 */
export function KgTimeline({ nodes, onSelectRun, highlightRunIds }: Props) {
  const [open, setOpen] = useState(true)
  const [expandedCluster, setExpandedCluster] = useState<string | null>(null)
  // Custom timeframe: `range` is the active [minTs, maxTs] zoom window (null =
  // full extent). `sel` is the in-progress drag selection in track-fraction
  // [0..1]. Drag across the strip to zoom into that window; reset to clear.
  const [range, setRange] = useState<[number, number] | null>(null)
  const [sel, setSel] = useState<{ a: number; b: number } | null>(null)
  const trackRef = useRef<HTMLDivElement>(null)
  const contentRef = useRef<HTMLDivElement>(null)
  const { height: contentHeight, handleProps } = useResizableHeight({
    defaultHeight: 132,
    minHeight: 88,
    maxHeight: 280,
    storageKey: 'ui.kgTimelineHeight',
    panelRef: contentRef,
  })

  const { dots, colorByTicker } = useMemo(() => {
    const runs: RunDot[] = []
    // implied price per (ticker, run_id) for a quick trend readout.
    const priceByRun = new Map<string, number>()
    for (const n of nodes) {
      if (n.node_type === 'run_output' && n.field === 'implied_share_price' && n.run_id) {
        const v = typeof n.value === 'number' ? n.value : Number(n.value)
        if (isFinite(v)) priceByRun.set(`${n.ticker}::${n.run_id}`, v)
      }
    }
    for (const n of nodes) {
      if (n.node_type !== 'dcf_run') continue
      runs.push({
        node: n,
        ticker: n.ticker,
        ts: n.updated_at,
        label: fmtDate(n.updated_at),
        impliedPrice: priceByRun.get(`${n.ticker}::${n.run_id}`),
      })
    }
    runs.sort((a, b) => a.ts - b.ts)

    const tickers = Array.from(new Set(runs.map(r => r.ticker))).sort()
    const cbt = new Map<string, string>()
    tickers.forEach((t, i) => cbt.set(t, TICKER_COLORS[i % TICKER_COLORS.length]))
    return { dots: runs, colorByTicker: cbt }
  }, [nodes])

  if (dots.length === 0) return null

  const fullMin = dots[0].ts
  const fullMax = dots[dots.length - 1].ts
  // Effective domain = active zoom window, clamped to the data extent.
  const effMin = range ? Math.max(fullMin, range[0]) : fullMin
  const effMax = range ? Math.min(fullMax, range[1]) : fullMax
  const span = Math.max(1, effMax - effMin)
  const single = effMin === effMax
  const pos = (ts: number) => (single ? 50 : ((ts - effMin) / span) * 92 + 4)
  const visibleDots = dots.filter(d => d.ts >= effMin - 0.5 && d.ts <= effMax + 0.5)
  const clusters: RunCluster[] = (() => {
    const byDay = new Map<string, RunDot[]>()
    for (const dot of visibleDots) {
      const key = dayKey(dot.ts)
      const arr = byDay.get(key) || []
      arr.push(dot)
      byDay.set(key, arr)
    }
    return Array.from(byDay.entries())
      .map(([key, runs]) => {
        runs.sort((a, b) => a.ts - b.ts)
        const hot = runs.some(r => highlightRunIds?.has(r.node.id))
        const tickers = Array.from(new Set(runs.map(r => r.ticker))).sort()
        return {
          key,
          ts: runs[Math.floor(runs.length / 2)].ts,
          label: fmtDate(runs[0].ts),
          runs,
          tickers,
          hot,
        }
      })
      .sort((a, b) => a.ts - b.ts)
  })()
  const dense = clusters.length > 14
  const labelEvery = dense ? Math.ceil(clusters.length / 10) : 1
  const openCluster = expandedCluster ? clusters.find(c => c.key === expandedCluster) : null
  const displayHeight = openCluster ? Math.max(contentHeight, 168) : contentHeight
  const tickerLegend = Array.from(colorByTicker.entries())
  const visibleLegend = tickerLegend.slice(0, 6)
  const hiddenLegendCount = Math.max(0, tickerLegend.length - visibleLegend.length)

  // Map a track fraction [0..1] back to a timestamp (inverse of `pos`).
  const fracToTs = (frac: number) => effMin + ((frac * 100 - 4) / 92) * span

  const beginBrush = (e: React.PointerEvent) => {
    const rect = trackRef.current?.getBoundingClientRect()
    if (!rect) return
    const frac = Math.min(1, Math.max(0, (e.clientX - rect.left) / rect.width))
    ;(e.target as Element).setPointerCapture?.(e.pointerId)
    setSel({ a: frac, b: frac })
  }
  const moveBrush = (e: React.PointerEvent) => {
    if (!sel) return
    const rect = trackRef.current?.getBoundingClientRect()
    if (!rect) return
    const frac = Math.min(1, Math.max(0, (e.clientX - rect.left) / rect.width))
    setSel({ a: sel.a, b: frac })
  }
  const endBrush = () => {
    if (!sel) return
    const lo = Math.min(sel.a, sel.b)
    const hi = Math.max(sel.a, sel.b)
    setSel(null)
    // Ignore tiny drags (treat as a click so dot selection still works).
    if (hi - lo < 0.03) return
    const t0 = fracToTs(lo)
    const t1 = fracToTs(hi)
    if (t1 - t0 >= 1) setRange([t0, t1])
  }

  return (
    <div className="relative z-20 flex-shrink-0 border-t border-edge bg-surface isolate">
      <button
        type="button"
        onClick={() => setOpen(o => !o)}
        aria-expanded={open}
        className="relative z-30 w-full flex items-center gap-2 px-4 py-2 text-left hover:bg-surface-2 transition bg-surface"
      >
        <ChevronDown size={13} className={`text-ink-dim transition-transform ${open ? '' : '-rotate-90'}`} />
        <Clock size={13} className="text-ink-dim" />
        <span className="text-[11px] uppercase tracking-wide text-ink-dim font-medium">Timeline</span>
        <span className="text-[11px] text-ink-dim tabular-nums">
          {range ? `${visibleDots.length} of ${dots.length} runs` : `${dots.length} runs`}
        </span>
        {range && (
          <span
            role="button"
            tabIndex={0}
            onClick={(e) => { e.stopPropagation(); setRange(null) }}
            onKeyDown={(e) => { if (e.key === 'Enter') { e.stopPropagation(); setRange(null) } }}
            className="flex items-center gap-1 text-[10px] text-accent hover:underline tabular-nums cursor-pointer"
            title="Clear zoom — show full timeline"
          >
            {fmtDate(effMin)}–{fmtDate(effMax)} ✕
          </span>
        )}
        <div className="ml-auto flex items-center gap-2.5 pointer-events-none min-w-0 overflow-hidden">
          {visibleLegend.map(([t, c]) => (
            <span key={t} className="flex items-center gap-1 text-[10px] text-ink-muted">
              <span className="inline-block w-1.5 h-1.5 rounded-full" style={{ backgroundColor: c }} />
              {t}
            </span>
          ))}
          {hiddenLegendCount > 0 && (
            <span className="text-[10px] text-ink-dim tabular-nums">+{hiddenLegendCount}</span>
          )}
        </div>
      </button>

      {open && (
        <>
          <div
            role="separator"
            aria-orientation="horizontal"
            aria-label="Resize timeline"
            title="Drag to resize · double-click to reset"
            {...handleProps}
            className="relative z-20 h-2 cursor-row-resize touch-none select-none group bg-surface"
          >
            <div className="absolute inset-x-0 top-1/2 h-px -translate-y-1/2 bg-border group-hover:bg-accent/70 group-active:bg-accent transition-colors" />
          </div>
          <div
            ref={contentRef}
            data-resizable-height
            style={{ height: displayHeight }}
            className="relative overflow-hidden bg-surface"
          >
            <div className="h-full overflow-x-auto overflow-y-hidden px-6 pb-5 pt-1 min-w-[280px]">
              <div ref={trackRef} className="relative h-full min-h-[3.5rem]">
                {/* Time axis — ink-dim for contrast on surface (bg-edge was invisible) */}
                <div
                  className="pointer-events-none absolute left-0 right-0 top-2 h-px bg-ink-dim/60"
                  aria-hidden
                />
                {/* Brush layer (behind dots) — drag to zoom into a timeframe.
                    Dots sit above (z-10) so their clicks still register. */}
                <div
                  className="absolute inset-0 z-0 cursor-crosshair"
                  onPointerDown={beginBrush}
                  onPointerMove={moveBrush}
                  onPointerUp={endBrush}
                  onPointerCancel={() => setSel(null)}
                  title="Drag to zoom into a timeframe"
                />
                {sel && Math.abs(sel.b - sel.a) > 0.005 && (
                  <div
                    className="pointer-events-none absolute top-0 bottom-4 z-[5] rounded-sm bg-accent/15 border border-accent/40"
                    style={{
                      left: `${Math.min(sel.a, sel.b) * 100}%`,
                      width: `${Math.abs(sel.b - sel.a) * 100}%`,
                    }}
                    aria-hidden
                  />
                )}
                {clusters.map((cluster, i) => {
                  const primary = cluster.runs[0]
                  const multi = cluster.runs.length > 1
                  const color = multi ? 'var(--color-accent)' : (colorByTicker.get(primary.ticker) || '#64748b')
                  const showLabel = cluster.hot || !dense || i === 0 || i === clusters.length - 1 || i % labelEvery === 0
                  const expanded = expandedCluster === cluster.key
                  const title = multi
                    ? `${cluster.label} · ${cluster.runs.length} runs · ${cluster.tickers.join(', ')}`
                    : `${primary.ticker} · ${primary.label}${primary.impliedPrice ? ` · $${primary.impliedPrice.toFixed(2)}` : ''}`
                  return (
                    <div
                      key={cluster.key}
                      className="absolute z-10 -translate-x-1/2 flex flex-col items-center group"
                      style={{ left: `${pos(cluster.ts)}%`, top: 0 }}
                      title={title}
                    >
                      <button
                        type="button"
                        onClick={() => {
                          if (multi) setExpandedCluster(expanded ? null : cluster.key)
                          else onSelectRun(primary.node)
                        }}
                        className="flex flex-col items-center"
                      >
                        <span
                          className={`relative rounded-full border-2 transition group-hover:scale-110 ${
                            multi ? 'w-5 h-5' : 'w-2.5 h-2.5'
                          } ${
                            cluster.hot || expanded ? 'ring-2 ring-accent/60' : ''
                          }`}
                          style={{ backgroundColor: color, borderColor: 'var(--color-bg)' }}
                        >
                          {multi && (
                            <span className="absolute inset-0 flex items-center justify-center text-[9px] font-mono text-white tabular-nums">
                              {cluster.runs.length}
                            </span>
                          )}
                        </span>
                        <span className={`mt-1.5 text-[9px] text-ink-dim whitespace-nowrap group-hover:text-ink-muted transition-opacity ${
                          showLabel ? 'opacity-100' : 'opacity-0 group-hover:opacity-100'
                        }`}>
                          {cluster.label}
                        </span>
                        {!multi && primary.impliedPrice != null && (
                          <span className={`text-[9px] font-mono tabular-nums text-ink-dim group-hover:text-ink-muted transition-opacity ${
                            showLabel ? 'opacity-100' : 'opacity-0 group-hover:opacity-100'
                          }`}>
                            ${primary.impliedPrice.toFixed(0)}
                          </span>
                        )}
                      </button>
                    </div>
                  )
                })}
                {openCluster && (
                  <div className="absolute left-0 right-0 top-11 bottom-1 z-30 rounded-md border border-edge bg-surface-2/95 shadow-xl backdrop-blur-sm">
                    <div className="flex items-center gap-2 border-b border-edge px-3 py-2">
                      <span className="text-[10px] font-medium uppercase tracking-wide text-ink-muted">
                        {openCluster.label}
                      </span>
                      <span className="text-[10px] text-ink-dim tabular-nums">
                        {openCluster.runs.length} runs
                      </span>
                      <span className="min-w-0 truncate text-[10px] text-ink-dim">
                        {openCluster.tickers.join(', ')}
                      </span>
                      <button
                        type="button"
                        onClick={() => setExpandedCluster(null)}
                        className="ml-auto rounded px-1.5 py-0.5 text-[10px] text-ink-dim hover:bg-surface hover:text-ink-muted"
                      >
                        Close
                      </button>
                    </div>
                    <div className="h-[calc(100%-2.25rem)] overflow-y-auto p-1.5">
                      <div className="grid grid-cols-1 gap-1 sm:grid-cols-2 lg:grid-cols-3">
                        {openCluster.runs.map(run => {
                          const runColor = colorByTicker.get(run.ticker) || '#64748b'
                          const hot = highlightRunIds?.has(run.node.id)
                          return (
                            <button
                              key={run.node.id}
                              type="button"
                              onClick={() => onSelectRun(run.node)}
                              className={`flex min-w-0 items-center gap-2 rounded border px-2 py-1.5 text-left transition hover:bg-surface ${
                                hot ? 'border-accent/60 ring-1 ring-accent/40' : 'border-transparent'
                              }`}
                            >
                              <span className="h-1.5 w-1.5 shrink-0 rounded-full" style={{ backgroundColor: runColor }} />
                              <span className="text-[11px] text-ink-muted">{run.ticker}</span>
                              <span className="min-w-0 truncate text-[10px] text-ink-dim">{run.node.field}</span>
                              {run.impliedPrice != null && (
                                <span className="ml-auto shrink-0 font-mono text-[10px] text-ink-dim">${run.impliedPrice.toFixed(0)}</span>
                              )}
                            </button>
                          )
                        })}
                      </div>
                    </div>
                  </div>
                )}
              </div>
            </div>
          </div>
        </>
      )}
    </div>
  )
}
