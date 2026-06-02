import { useState } from 'react'
import { Search, X, Check } from 'lucide-react'
import type { KgQueryResult } from '../hooks/useKnowledgeGraph'
import { Markdown } from './kg/Markdown'

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
    <div className="h-full flex flex-col bg-surface">
      <header className="flex items-center gap-2.5 px-4 h-12 border-b border-edge flex-shrink-0">
        <Search size={16} className="text-ink-muted" />
        <div className="text-[14px] font-medium text-ink flex-1">Ask</div>
        <button onClick={onClose} aria-label="Close" className="text-ink-dim hover:text-ink p-1 -mr-1 rounded hover:bg-surface-2 transition">
          <X size={16} />
        </button>
      </header>

      <div className="p-4 space-y-3 border-b border-edge">
        <div>
          <label className="text-[11px] uppercase tracking-wide text-ink-dim font-medium">Question</label>
          <textarea
            value={question}
            onChange={e => setQuestion(e.target.value)}
            rows={3}
            placeholder="Ask anything about what the agent knows…"
            className="w-full mt-1.5 bg-surface-2 border border-edge rounded-md px-2.5 py-2 text-ink text-[13px] placeholder:text-ink-dim focus:outline-none focus:border-accent/50 resize-none"
            onKeyDown={e => { if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) handleAsk() }}
          />
        </div>

        <div>
          <label className="text-[11px] uppercase tracking-wide text-ink-dim font-medium">Ticker (optional)</label>
          <input
            value={ticker}
            onChange={e => setTicker(e.target.value.toUpperCase())}
            placeholder="META"
            className="w-full mt-1.5 bg-surface-2 border border-edge rounded-md px-2.5 py-2 text-ink text-[13px] font-mono placeholder:text-ink-dim focus:outline-none focus:border-accent/50"
          />
        </div>

        <button
          onClick={handleAsk}
          disabled={busy || !question.trim()}
          className="w-full px-3 py-2 rounded-md bg-accent-soft text-accent border border-accent/40 hover:bg-accent/20 disabled:opacity-50 text-[13px] font-medium transition"
        >
          {busy ? 'Querying…' : 'Ask'}
        </button>

        {highlightCount > 0 && (
          <button
            onClick={onClearHighlight}
            className="w-full px-2 py-1.5 rounded-md text-ink-muted border border-edge hover:bg-surface-2 text-[12px] transition"
          >
            Clear highlight · {highlightCount}
          </button>
        )}
      </div>

      <div className="flex-1 overflow-y-auto p-4 space-y-3">
        {!result ? (
          <div>
            <div className="text-[11px] uppercase tracking-wide text-ink-dim font-medium mb-2">Examples</div>
            <div className="space-y-1.5">
              {EXAMPLES.map(ex => (
                <button
                  key={ex}
                  onClick={() => setQuestion(ex)}
                  className="block w-full text-left text-[12px] px-2.5 py-2 rounded-md bg-surface-2 text-ink-muted hover:text-ink hover:border-accent/30 border border-transparent transition"
                >
                  {ex}
                </button>
              ))}
            </div>
          </div>
        ) : (
          <>
            <div>
              <div className="text-[11px] uppercase tracking-wide text-ink-dim font-medium mb-1.5">Answer</div>
              <div className="text-ink-muted">
                <Markdown text={result.answer} />
              </div>
            </div>

            {(result.traversal_edges?.length ?? 0) > 0 && (
              <div>
                <div className="text-[11px] uppercase tracking-wide text-ink-dim font-medium mb-1.5">
                  Route · {result.traversal_edges.length} hops
                </div>
                <div className="space-y-1">
                  {result.traversal_edges.slice(0, 30).map((e, i) => {
                    const labels = (() => {
                      const m = new Map(result.matched_nodes.map(n => [n.id, n.field || n.node_type]))
                      const short = (id: string) => m.get(id) ?? id.split('-').slice(-1)[0]
                      return { src: short(e.src_id), tgt: short(e.tgt_id) }
                    })()
                    return (
                      <div key={`${e.src_id}->${e.tgt_id}-${i}`} className="text-[11px] flex items-center gap-1 truncate">
                        <span className="font-mono text-ink-muted truncate">{labels.src}</span>
                        <span className="text-accent">→</span>
                        <span className="font-mono text-ink-muted truncate">{labels.tgt}</span>
                      </div>
                    )
                  })}
                  {result.traversal_edges.length > 30 && (
                    <div className="text-[11px] text-ink-dim">… {result.traversal_edges.length - 30} more</div>
                  )}
                </div>
                <div className="mt-2 flex items-center gap-1.5 text-[11px] text-accent">
                  <Check size={12} /> {result.traversal_path.length} nodes highlighted
                </div>
              </div>
            )}

            {(result.traversal_edges?.length ?? 0) === 0 && result.traversal_path.length > 0 && (
              <div className="flex items-center gap-1.5 text-[11px] text-accent">
                <Check size={12} /> {result.traversal_path.length} matched · highlighted in graph
              </div>
            )}
          </>
        )}
      </div>
    </div>
  )
}
