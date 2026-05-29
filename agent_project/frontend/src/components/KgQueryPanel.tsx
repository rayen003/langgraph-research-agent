import { useState } from 'react'
import type { KgQueryResult } from '../hooks/useKnowledgeGraph'

interface Props {
  onClose: () => void
  onQuery: (question: string, ticker?: string) => Promise<KgQueryResult | null>
  onClearHighlight: () => void
  highlightCount: number
}

const EXAMPLES = [
  'What assumptions did we use in the last META DCF run?',
  'Why did we pick that revenue growth?',
  'Show me all drivers for META',
  'What changed about META since last run?',
]

export function KgQueryPanel({ onClose, onQuery, onClearHighlight, highlightCount }: Props) {
  const [question, setQuestion] = useState('')
  const [ticker, setTicker] = useState('')
  const [result, setResult] = useState<KgQueryResult | null>(null)
  const [busy, setBusy] = useState(false)

  const handleAsk = async () => {
    if (!question.trim()) return
    setBusy(true)
    const r = await onQuery(question.trim(), ticker.trim() || undefined)
    setResult(r)
    setBusy(false)
  }

  return (
    <div className="h-full w-[360px] flex-shrink-0 border-l border-[#1c1c24] bg-[#0c0c12] flex flex-col">
      <div className="px-4 py-3 border-b border-[#1c1c24] flex items-center justify-between">
        <div className="text-zinc-300 font-medium text-sm">Query</div>
        <button onClick={onClose} className="text-zinc-500 hover:text-zinc-300 text-[13px]">✕</button>
      </div>

      <div className="p-4 space-y-3 border-b border-[#1c1c24]">
        <div>
          <label className="text-[10px] uppercase text-zinc-600 tracking-wider">Question</label>
          <textarea
            value={question}
            onChange={e => setQuestion(e.target.value)}
            rows={3}
            placeholder="Ask anything about what the agent knows..."
            className="w-full mt-1 bg-[#0a0a0a] border border-[#2a2a36] rounded px-2 py-1.5 text-zinc-200 text-[12px] placeholder:text-zinc-700 focus:outline-none focus:border-indigo-500/50"
            onKeyDown={e => {
              if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) handleAsk()
            }}
          />
        </div>

        <div>
          <label className="text-[10px] uppercase text-zinc-600 tracking-wider">Ticker (optional)</label>
          <input
            value={ticker}
            onChange={e => setTicker(e.target.value.toUpperCase())}
            placeholder="META"
            className="w-full mt-1 bg-[#0a0a0a] border border-[#2a2a36] rounded px-2 py-1.5 text-zinc-200 text-[12px] font-mono placeholder:text-zinc-700 focus:outline-none focus:border-indigo-500/50"
          />
        </div>

        <button
          onClick={handleAsk}
          disabled={busy || !question.trim()}
          className="w-full px-3 py-1.5 rounded bg-indigo-500/20 text-indigo-300 border border-indigo-500/40 hover:bg-indigo-500/30 disabled:opacity-50 text-[12px]"
        >
          {busy ? 'Querying…' : 'Ask'}
        </button>

        {highlightCount > 0 && (
          <button
            onClick={onClearHighlight}
            className="w-full px-2 py-1 rounded bg-teal-500/10 text-teal-400 border border-teal-500/30 text-[11px] hover:bg-teal-500/20"
          >
            Clear highlight ({highlightCount} nodes)
          </button>
        )}
      </div>

      <div className="flex-1 overflow-y-auto p-4 space-y-3">
        {!result ? (
          <div>
            <div className="text-[10px] uppercase text-zinc-600 tracking-wider mb-2">Examples</div>
            <div className="space-y-1">
              {EXAMPLES.map(ex => (
                <button
                  key={ex}
                  onClick={() => setQuestion(ex)}
                  className="block w-full text-left text-[11px] px-2 py-1.5 rounded bg-zinc-800/40 text-zinc-400 hover:bg-zinc-800/80 hover:text-zinc-200 transition"
                >
                  {ex}
                </button>
              ))}
            </div>
          </div>
        ) : (
          <>
            <div>
              <div className="text-[10px] uppercase text-zinc-600 tracking-wider mb-1">Answer</div>
              <pre className="text-zinc-200 text-[11px] whitespace-pre-wrap leading-relaxed font-sans">
                {result.answer}
              </pre>
            </div>

            {(result.traversal_edges?.length ?? 0) > 0 && (
              <div>
                <div className="text-[10px] uppercase text-zinc-600 tracking-wider mb-1">
                  Traversal route ({result.traversal_edges.length} hops)
                </div>
                <div className="space-y-1">
                  {result.traversal_edges.slice(0, 30).map((e, i) => {
                    const labels = (() => {
                      const m = new Map(result.matched_nodes.map(n => [n.id, n.field || n.node_type]))
                      const short = (id: string) => m.get(id) ?? id.split('-').slice(-1)[0]
                      return { src: short(e.src_id), tgt: short(e.tgt_id) }
                    })()
                    return (
                      <div key={`${e.src_id}->${e.tgt_id}-${i}`} className="text-[10px] flex items-center gap-1 truncate">
                        <span className="font-mono text-zinc-400 truncate">{labels.src}</span>
                        <span className="text-teal-500">─{e.relation ?? 'REL'}→</span>
                        <span className="font-mono text-zinc-400 truncate">{labels.tgt}</span>
                      </div>
                    )
                  })}
                  {result.traversal_edges.length > 30 && (
                    <div className="text-[10px] text-zinc-600">
                      … {result.traversal_edges.length - 30} more hops
                    </div>
                  )}
                </div>
                <div className="mt-2 text-[10px] text-teal-400">
                  ✓ Route animated in graph · {result.traversal_path.length} nodes highlighted
                </div>
              </div>
            )}

            {(result.traversal_edges?.length ?? 0) === 0 && result.traversal_path.length > 0 && (
              <div>
                <div className="text-[10px] uppercase text-zinc-600 tracking-wider mb-1">
                  Matched nodes ({result.traversal_path.length})
                </div>
                <div className="mt-1 text-[10px] text-teal-400">✓ Highlighted in graph</div>
              </div>
            )}
          </>
        )}
      </div>
    </div>
  )
}
