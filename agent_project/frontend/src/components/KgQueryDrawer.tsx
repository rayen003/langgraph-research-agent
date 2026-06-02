import { useState } from 'react'
import type { KgQueryResult } from '../hooks/useKnowledgeGraph'

interface Props {
  onClose: () => void
  onQuery: (question: string, ticker?: string) => Promise<KgQueryResult | null>
}

const EXAMPLES = [
  'What assumptions did we use in the last META DCF run?',
  'Why did we pick that revenue growth?',
  'Show me all drivers for META',
  'What changed about META since last run?',
]

export function KgQueryDrawer({ onClose, onQuery }: Props) {
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
    <div className="absolute inset-0 z-30 bg-black/40" onClick={onClose}>
      <div
        className="absolute right-0 top-0 h-full w-[420px] bg-bg-overlay border-l border-border-accent shadow-2xl flex flex-col"
        onClick={e => e.stopPropagation()}
      >
        <div className="px-4 py-3 border-b border-border-accent flex items-center justify-between">
          <div className="text-ink-muted font-medium text-sm">Query Knowledge Graph</div>
          <button onClick={onClose} className="text-ink-dim hover:text-ink-muted">✕</button>
        </div>

        <div className="p-4 space-y-3">
          <div>
            <label className="text-[10px] uppercase text-ink-dim tracking-wider">Question</label>
            <textarea
              value={question}
              onChange={e => setQuestion(e.target.value)}
              rows={3}
              placeholder="Ask anything about what the agent knows..."
              className="w-full mt-1 bg-bg border border-border-accent rounded px-2 py-1.5 text-ink text-[12px] placeholder:text-ink-dim"
              onKeyDown={e => {
                if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) handleAsk()
              }}
            />
          </div>

          <div>
            <label className="text-[10px] uppercase text-ink-dim tracking-wider">Ticker (optional)</label>
            <input
              value={ticker}
              onChange={e => setTicker(e.target.value.toUpperCase())}
              placeholder="META"
              className="w-full mt-1 bg-bg border border-border-accent rounded px-2 py-1.5 text-ink text-[12px] font-mono placeholder:text-ink-dim"
            />
          </div>

          <button
            onClick={handleAsk}
            disabled={busy || !question.trim()}
            className="w-full px-3 py-1.5 rounded bg-indigo-500/20 text-indigo-300 border border-indigo-500/40 hover:bg-indigo-500/30 disabled:opacity-50 text-[12px]"
          >
            {busy ? 'Querying…' : 'Ask'}
          </button>

          {!result && (
            <div>
              <div className="text-[10px] uppercase text-ink-dim tracking-wider mb-1">Examples</div>
              <div className="space-y-1">
                {EXAMPLES.map(ex => (
                  <button
                    key={ex}
                    onClick={() => setQuestion(ex)}
                    className="block w-full text-left text-[11px] px-2 py-1 rounded bg-zinc-800/40 text-ink-muted hover:bg-zinc-800/80 hover:text-ink"
                  >
                    {ex}
                  </button>
                ))}
              </div>
            </div>
          )}
        </div>

        {result && (
          <div className="flex-1 overflow-y-auto px-4 pb-4 space-y-3 border-t border-border-accent pt-3">
            <div>
              <div className="text-[10px] uppercase text-ink-dim tracking-wider">Answer</div>
              <pre className="text-ink text-[11px] whitespace-pre-wrap mt-1 leading-relaxed">
                {result.answer}
              </pre>
            </div>

            {result.traversal_path.length > 0 && (
              <div>
                <div className="text-[10px] uppercase text-ink-dim tracking-wider">
                  Traversal path ({result.traversal_path.length} nodes)
                </div>
                <div className="mt-1 space-y-1">
                  {result.traversal_path.slice(0, 30).map(nid => (
                    <div key={nid} className="text-[10px] font-mono text-ink-dim truncate">
                      {nid}
                    </div>
                  ))}
                  {result.traversal_path.length > 30 && (
                    <div className="text-[10px] text-ink-dim">
                      … {result.traversal_path.length - 30} more
                    </div>
                  )}
                </div>
                <div className="mt-2 text-[10px] text-teal-400">
                  ✓ Highlighted in graph — close drawer to view
                </div>
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  )
}
