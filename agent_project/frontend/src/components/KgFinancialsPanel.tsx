import { useState } from 'react'
import { Layers } from 'lucide-react'
import type { KgNode } from '../hooks/useKnowledgeGraph'
import { Panel } from './kg/Panel'
import { CategoryBody } from './KgTablePanel'

export interface FinancialsCategory {
  key: string
  label: string
  members: KgNode[]
}

interface Props {
  ticker: string
  categories: FinancialsCategory[]
  highlightIds?: Set<string>
  onClose: () => void
  onCreateBelief?: (ticker: string, field: string, value: unknown) => Promise<void>
  onDeleteNode?: (id: string) => Promise<void>
}

/**
 * Financials hub detail — replaces the old on-canvas category sub-hubs (which
 * cluttered the graph and overlapped labels) with a single dock panel whose
 * tabs switch between categories (Fundamentals / Drivers / Filings / Thesis /
 * Beliefs / …). The canvas stays clean: only company + News + Financials + DCF
 * run hubs render. This panel is also the natural home for the period (YoY)
 * picker once temporal fundamentals are populated.
 */
export function KgFinancialsPanel({
  ticker, categories, highlightIds, onClose, onCreateBelief, onDeleteNode,
}: Props) {
  // Default to the first non-empty category (categories arrive sorted by size),
  // falling back to the first tab.
  const initial = categories.find(c => c.members.length > 0)?.key ?? categories[0]?.key ?? ''
  const [active, setActive] = useState(initial)
  const current = categories.find(c => c.key === active) ?? categories[0]

  return (
    <Panel
      icon={<Layers size={16} />}
      title={`${ticker} · Financials`}
      subtitle={`${categories.reduce((n, c) => n + c.members.length, 0)} facts across ${categories.length} categories`}
      onClose={onClose}
    >
      {/* Category tabs */}
      <div className="flex flex-wrap gap-1 px-3 pt-3 pb-2 border-b border-edge">
        {categories.map(c => {
          const isActive = c.key === active
          return (
            <button
              key={c.key}
              onClick={() => setActive(c.key)}
              className={`px-2 py-1 rounded-md text-[11px] border transition ${
                isActive
                  ? 'bg-accent-soft text-accent border-accent/40'
                  : 'text-ink-dim border-edge hover:text-ink-muted hover:bg-surface-2'
              }`}
            >
              {c.label}
              <span className={`ml-1 ${isActive ? 'text-accent/70' : 'text-ink-dim'}`}>{c.members.length}</span>
            </button>
          )
        })}
      </div>

      {/* Active category body */}
      <div className="p-4">
        {current ? (
          <CategoryBody
            members={current.members}
            highlightIds={highlightIds}
            beliefTicker={current.key === 'beliefs' ? ticker : undefined}
            onCreateBelief={onCreateBelief}
            onDeleteNode={onDeleteNode}
          />
        ) : (
          <div className="text-ink-dim text-[12px]">No financial data yet.</div>
        )}
      </div>
    </Panel>
  )
}
