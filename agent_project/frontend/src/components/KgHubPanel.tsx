import { useMemo } from 'react'
import { Newspaper } from 'lucide-react'
import type { KgNode } from '../hooks/useKnowledgeGraph'
import { KgValueView } from './KgValueView'
import { Panel, Caption } from './kg/Panel'

interface Props {
  title: string
  subtitle?: string
  members: KgNode[]
  /** raw node ids matched by a query → glow those rows. */
  highlightIds?: Set<string>
  onClose: () => void
}

function ageStr(ts: number): string {
  const age = Math.max(0, Date.now() / 1000 - ts)
  if (age < 60) return `${Math.round(age)}s ago`
  if (age < 3600) return `${Math.round(age / 60)}m ago`
  if (age < 86400) return `${Math.round(age / 3600)}h ago`
  return `${Math.round(age / 86400)}d ago`
}

const TYPE_LABEL: Record<string, string> = {
  company_synthesis: 'Synthesis',
  thesis: 'Thesis',
  market_metric_fund: 'Fundamentals',
  market_metric_price: 'Price',
  company_lifecycle: 'Lifecycle',
  filing: 'Filing',
  driver: 'Driver',
  risk: 'Risk',
  theme: 'Theme',
  user_belief: 'User belief',
  news_item: 'News',
  dcf_run: 'DCF run',
}

// Muted left-stripe per type — slate family, one accent. No rainbow.
const TYPE_ACCENT: Record<string, string> = {
  company_synthesis: 'border-l-accent/50',
  thesis: 'border-l-indigo-400/40',
  market_metric_fund: 'border-l-teal-500/40',
  market_metric_price: 'border-l-teal-500/40',
  company_lifecycle: 'border-l-slate-400/40',
  filing: 'border-l-slate-500/40',
  driver: 'border-l-slate-400/40',
  news_item: 'border-l-accent/40',
  dcf_run: 'border-l-indigo-400/40',
}

/**
 * Generic hub detail panel — renders a hub's member nodes as grouped, colored
 * cards. Used for News + Financials hubs. Each member is a collapsible-free
 * card (KgValueView handles internal disclosure). Query matches glow the row.
 */
export function KgHubPanel({ title, subtitle, members, highlightIds, onClose }: Props) {
  // Group members by node_type so related rows sit together.
  const groups = useMemo(() => {
    const m = new Map<string, KgNode[]>()
    for (const n of members) {
      const arr = m.get(n.node_type) || []
      arr.push(n)
      m.set(n.node_type, arr)
    }
    // Sort each group newest-first.
    for (const arr of m.values()) arr.sort((a, b) => b.updated_at - a.updated_at)
    return Array.from(m.entries())
  }, [members])

  return (
    <Panel icon={<Newspaper size={16} />} title={title} subtitle={subtitle} onClose={onClose}>
      <div className="p-4 space-y-4">
        {groups.length === 0 && <div className="text-ink-dim text-[12px]">No data.</div>}
        {groups.map(([type, items]) => (
          <div key={type} className="space-y-2">
            <Caption>{TYPE_LABEL[type] || type} · {items.length}</Caption>
            {items.map(n => {
              const hot = highlightIds?.has(n.id)
              const accent = TYPE_ACCENT[type] || 'border-l-edge-2'
              return (
                <div
                  key={n.id}
                  className={`rounded-md border bg-surface-2 border-l-2 px-3 py-2.5 transition ${accent} ${
                    hot ? 'border-accent/60 ring-1 ring-accent/40' : 'border-edge'
                  }`}
                >
                  <div className="flex items-center justify-between mb-1.5">
                    <span className="text-[10px] uppercase tracking-wide text-ink-dim">{n.field}</span>
                    <span className="text-[10px] text-ink-dim tabular-nums">{ageStr(n.updated_at)}</span>
                  </div>
                  <KgValueView value={n.value} nodeType={n.node_type} />
                </div>
              )
            })}
          </div>
        ))}
      </div>
    </Panel>
  )
}
