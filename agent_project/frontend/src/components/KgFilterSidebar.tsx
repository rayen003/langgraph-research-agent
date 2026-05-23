import { useMemo } from 'react'
import type { KgNode } from '../hooks/useKnowledgeGraph'
import { colorForNode } from './KgCanvas'

interface Props {
  nodes: KgNode[]
  hiddenTypes: Set<string>
  hiddenTickers: Set<string>
  hiddenSources: Set<string>
  hideOrphans: boolean
  edgeCountByNodeId: Map<string, number>
  onToggleType: (t: string) => void
  onToggleTicker: (t: string) => void
  onToggleSource: (s: string) => void
  onToggleHideOrphans: () => void
  onResetFilters: () => void
}

const TYPE_LABELS: Record<string, string> = {
  company: 'Company',
  dcf_run: 'DCF Run',
  thesis: 'Thesis',
  company_synthesis: 'Synthesis',
  run_assumption: 'Assumption',
  run_output: 'Output',
  run_scenario: 'Scenario',
  market_metric_fund: 'Fundamentals',
  market_metric_price: 'Price',
  driver: 'Driver',
  risk: 'Risk',
  theme: 'Theme',
  user_belief: 'User belief',
}

export function KgFilterSidebar({
  nodes,
  hiddenTypes,
  hiddenTickers,
  hiddenSources,
  hideOrphans,
  edgeCountByNodeId,
  onToggleType,
  onToggleTicker,
  onToggleSource,
  onToggleHideOrphans,
  onResetFilters,
}: Props) {
  const { types, tickers, sources, typeCounts, tickerCounts, sourceCounts } = useMemo(() => {
    const tc = new Map<string, number>()
    const tkc = new Map<string, number>()
    const sc = new Map<string, number>()
    for (const n of nodes) {
      tc.set(n.node_type, (tc.get(n.node_type) || 0) + 1)
      tkc.set(n.ticker, (tkc.get(n.ticker) || 0) + 1)
      sc.set(n.source, (sc.get(n.source) || 0) + 1)
    }
    const orderedTypes = Array.from(tc.keys()).sort((a, b) => (tc.get(b)! - tc.get(a)!))
    const orderedTickers = Array.from(tkc.keys()).sort()
    const orderedSources = Array.from(sc.keys()).sort()
    return {
      types: orderedTypes,
      tickers: orderedTickers,
      sources: orderedSources,
      typeCounts: tc,
      tickerCounts: tkc,
      sourceCounts: sc,
    }
  }, [nodes])

  const orphanCount = useMemo(
    () => nodes.filter(n => (edgeCountByNodeId.get(n.id) || 0) === 0).length,
    [nodes, edgeCountByNodeId],
  )

  return (
    <div className="h-full w-[220px] flex-shrink-0 border-r border-[#1c1c24] bg-[#0c0c12] overflow-y-auto">
      <div className="px-3 py-3 border-b border-[#1c1c24] flex items-center justify-between">
        <div className="text-zinc-300 text-[12px] font-medium">Filters</div>
        <button
          onClick={onResetFilters}
          className="text-[10px] text-zinc-500 hover:text-zinc-300"
        >
          reset
        </button>
      </div>

      {/* Node types */}
      <div className="px-3 py-3 border-b border-[#1c1c24] space-y-1">
        <div className="text-[10px] uppercase text-zinc-600 tracking-wider mb-2">Types</div>
        {types.map(t => {
          const hidden = hiddenTypes.has(t)
          const color = colorForNode({ node_type: t, source: '' } as KgNode)
          return (
            <button
              key={t}
              onClick={() => onToggleType(t)}
              className={`flex items-center gap-2 w-full text-left px-1 py-1 rounded text-[11px] transition ${
                hidden ? 'opacity-40' : 'opacity-100'
              } hover:bg-[#1c1c24]`}
            >
              <span
                className="inline-block w-2.5 h-2.5 rounded-full flex-shrink-0"
                style={{ backgroundColor: color }}
              />
              <span className="text-zinc-300 flex-1 truncate">{TYPE_LABELS[t] || t}</span>
              <span className="text-zinc-600 text-[10px]">{typeCounts.get(t)}</span>
            </button>
          )
        })}
      </div>

      {/* Tickers */}
      {tickers.length > 0 && (
        <div className="px-3 py-3 border-b border-[#1c1c24] space-y-1">
          <div className="text-[10px] uppercase text-zinc-600 tracking-wider mb-2">Tickers</div>
          <div className="flex flex-wrap gap-1">
            {tickers.map(t => {
              const hidden = hiddenTickers.has(t)
              return (
                <button
                  key={t}
                  onClick={() => onToggleTicker(t)}
                  className={`px-2 py-0.5 rounded text-[10px] font-mono border transition ${
                    hidden
                      ? 'bg-zinc-900 text-zinc-600 border-zinc-800'
                      : 'bg-blue-500/15 text-blue-300 border-blue-500/40'
                  } hover:opacity-80`}
                >
                  {t || '—'} <span className="opacity-60">{tickerCounts.get(t)}</span>
                </button>
              )
            })}
          </div>
        </div>
      )}

      {/* Sources */}
      <div className="px-3 py-3 border-b border-[#1c1c24] space-y-1">
        <div className="text-[10px] uppercase text-zinc-600 tracking-wider mb-2">Source</div>
        {sources.map(s => {
          const hidden = hiddenSources.has(s)
          return (
            <button
              key={s}
              onClick={() => onToggleSource(s)}
              className={`flex items-center gap-2 w-full text-left px-1 py-1 rounded text-[11px] transition ${
                hidden ? 'opacity-40' : 'opacity-100'
              } hover:bg-[#1c1c24]`}
            >
              <span className="text-zinc-300 flex-1 truncate">{s || 'unknown'}</span>
              <span className="text-zinc-600 text-[10px]">{sourceCounts.get(s)}</span>
            </button>
          )
        })}
      </div>

      {/* Orphans */}
      <div className="px-3 py-3 border-b border-[#1c1c24]">
        <button
          onClick={onToggleHideOrphans}
          className={`flex items-center gap-2 w-full text-left px-1 py-1 rounded text-[11px] hover:bg-[#1c1c24] ${
            hideOrphans ? 'text-zinc-500' : 'text-zinc-300'
          }`}
        >
          <span className="flex-1">Hide orphan nodes</span>
          <span className={`text-[10px] ${hideOrphans ? 'text-teal-400' : 'text-zinc-600'}`}>
            {hideOrphans ? 'on' : 'off'}
          </span>
        </button>
        <div className="text-[10px] text-zinc-600 mt-1 pl-1">
          {orphanCount} orphans in graph
        </div>
      </div>
    </div>
  )
}
