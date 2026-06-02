import { useMemo, useState } from 'react'
import { Lightbulb, ListChecks, BarChart3, FileText, X } from 'lucide-react'
import type { KgNode } from '../hooks/useKnowledgeGraph'
import { KgValueView } from './KgValueView'
import { Panel } from './kg/Panel'

interface Props {
  title: string
  subtitle?: string
  members: KgNode[]
  /** raw node ids matched by a query → glow those rows. */
  highlightIds?: Set<string>
  onClose: () => void
  /** When set, this category is the Beliefs hub → show an add-belief composer. */
  beliefTicker?: string
  onCreateBelief?: (ticker: string, field: string, value: unknown) => Promise<void>
  onDeleteNode?: (id: string) => Promise<void>
}

const DIR_STYLE: Record<string, string> = {
  positive: 'bg-emerald-500/15 text-emerald-300 border-emerald-500/40',
  negative: 'bg-rose-500/15 text-rose-300 border-rose-500/40',
  neutral: 'bg-surface-2 text-ink-muted border-edge',
}
const CONV_STYLE: Record<string, string> = {
  high: 'bg-amber-500/20 text-amber-300 border-amber-500/40',
  medium: 'bg-sky-500/15 text-sky-300 border-sky-500/30',
  low: 'bg-surface-2 text-ink-muted border-edge',
}

function asObj(v: unknown): Record<string, unknown> {
  return v && typeof v === 'object' && !Array.isArray(v) ? (v as Record<string, unknown>) : {}
}

function humanizeField(f: string): string {
  return f.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase())
}

// ── Driver table (direction + conviction, dedup by text) ──────────────────────
function DriverTable({ members, highlightIds, onDelete }: { members: KgNode[]; highlightIds?: Set<string>; onDelete?: (id: string) => Promise<void> }) {
  const [dirFilter, setDirFilter] = useState<string | null>(null)
  const [convFilter, setConvFilter] = useState<string | null>(null)

  // Dedup by driver text (latest wins) — reruns dump the same drivers repeatedly.
  const rows = useMemo(() => {
    const byText = new Map<string, { node: KgNode; text: string; dir: string; conv: string }>()
    for (const n of members) {
      const o = asObj(n.value)
      const text = String(o.driver ?? o.label ?? o.text ?? n.field).trim()
      const dir = String(o.direction ?? 'neutral').toLowerCase()
      const conv = String(o.conviction ?? '').toLowerCase()
      const key = text.toLowerCase()
      const prev = byText.get(key)
      if (!prev || n.updated_at > prev.node.updated_at) {
        byText.set(key, { node: n, text, dir, conv })
      }
    }
    return Array.from(byText.values()).sort((a, b) => {
      // positive first, then by conviction high→low
      const dr = (a.dir === 'negative' ? 1 : 0) - (b.dir === 'negative' ? 1 : 0)
      if (dr !== 0) return dr
      const order: Record<string, number> = { high: 0, medium: 1, low: 2, '': 3 }
      return (order[a.conv] ?? 3) - (order[b.conv] ?? 3)
    })
  }, [members])

  const filtered = rows.filter(r =>
    (!dirFilter || r.dir === dirFilter) && (!convFilter || r.conv === convFilter))

  const dirs = Array.from(new Set(rows.map(r => r.dir))).filter(Boolean)
  const convs = Array.from(new Set(rows.map(r => r.conv))).filter(Boolean)

  return (
    <div>
      {/* Filter chips */}
      <div className="flex flex-wrap items-center gap-1.5 mb-2">
        {dirs.map(d => (
          <button
            key={d}
            onClick={() => setDirFilter(dirFilter === d ? null : d)}
            className={`px-2 py-0.5 rounded-full text-[10px] border capitalize transition ${
              dirFilter === d ? DIR_STYLE[d] || DIR_STYLE.neutral : 'bg-transparent text-ink-dim border-edge hover:text-ink-muted'
            }`}
          >
            {d}
          </button>
        ))}
        {convs.length > 0 && <span className="text-ink-dim text-[10px]">·</span>}
        {convs.map(c => (
          <button
            key={c}
            onClick={() => setConvFilter(convFilter === c ? null : c)}
            className={`px-2 py-0.5 rounded-full text-[10px] border capitalize transition ${
              convFilter === c ? CONV_STYLE[c] || CONV_STYLE.low : 'bg-transparent text-ink-dim border-edge hover:text-ink-muted'
            }`}
          >
            {c}
          </button>
        ))}
        {(dirFilter || convFilter) && (
          <button
            onClick={() => { setDirFilter(null); setConvFilter(null) }}
            className="text-[10px] text-ink-dim hover:text-ink-muted ml-auto"
          >
            clear
          </button>
        )}
      </div>

      <div className="text-[9px] text-ink-dim mb-1.5">
        {filtered.length} of {rows.length} unique · {members.length} raw
      </div>

      {/* Table */}
      <div className="space-y-1">
        {filtered.map(r => {
          const hot = highlightIds?.has(r.node.id)
          return (
            <div
              key={r.node.id}
              className={`group flex items-start gap-2 px-2 py-1.5 rounded border transition ${
                hot ? 'border-accent/60 ring-1 ring-accent/40' : 'border-edge bg-surface'
              }`}
            >
              <span className={`mt-0.5 inline-block w-1.5 h-1.5 rounded-full flex-shrink-0 ${
                r.dir === 'positive' ? 'bg-emerald-400' : r.dir === 'negative' ? 'bg-rose-400' : 'bg-zinc-400'
              }`} />
              <span className="flex-1 text-[11px] text-ink-muted leading-snug">{r.text}</span>
              {r.conv && (
                <span className={`px-1.5 py-0.5 rounded text-[9px] border flex-shrink-0 capitalize ${CONV_STYLE[r.conv] || CONV_STYLE.low}`}>
                  {r.conv}
                </span>
              )}
              {onDelete && (
                <button
                  onClick={() => onDelete(r.node.id)}
                  className="text-ink-dim hover:text-rose-400 shrink-0 opacity-0 group-hover:opacity-100 transition-opacity"
                  title="Delete"
                >
                  ✕
                </button>
              )}
            </div>
          )
        })}
        {filtered.length === 0 && <div className="text-ink-dim text-[11px]">No rows match.</div>}
      </div>
    </div>
  )
}

// ── Metrics table (field → value) ─────────────────────────────────────────────
function MetricTable({ members, highlightIds, onDelete }: { members: KgNode[]; highlightIds?: Set<string>; onDelete?: (id: string) => Promise<void> }) {
  const rows = useMemo(
    () => [...members].sort((a, b) => a.field.localeCompare(b.field)),
    [members],
  )
  return (
    <div className="space-y-0.5">
      {rows.map(n => {
        const hot = highlightIds?.has(n.id)
        const v = n.value
        // Document facts carry structured values — extract the numeric value for inline display.
        const inner = (v && typeof v === 'object' && !Array.isArray(v)) ? (v as Record<string,unknown>) : null
        const numeric = inner && typeof inner.value === 'number' ? inner.value : null
        const label = inner && typeof inner.text === 'string' ? inner.text : ''
        const display = numeric !== null
          ? (Math.abs(numeric) >= 1 ? numeric.toLocaleString(undefined, { maximumFractionDigits: 2 }) : String(numeric))
          : typeof v === 'number'
            ? (Math.abs(v) >= 1 ? v.toLocaleString(undefined, { maximumFractionDigits: 2 }) : String(v))
            : typeof v === 'string' ? v : ''
        return (
          <div
            key={n.id}
            className={`flex items-center gap-2 px-2 py-1 rounded transition ${
              hot ? 'ring-1 ring-accent/40' : 'hover:bg-surface-2'
            }`}
          >
            <span className="text-[10px] text-ink-dim flex-1">{humanizeField(n.field)}</span>
            {display
              ? <span className="text-[10px] text-ink font-mono" title={label || undefined}>{display}</span>
              : <div className="flex-1"><KgValueView value={v} nodeType={n.node_type} /></div>}
            {onDelete && (
              <button
                onClick={() => onDelete(n.id)}
                className="text-ink-dim hover:text-rose-400 shrink-0 px-1 opacity-0 group-hover:opacity-100 transition-opacity"
                title="Delete"
              >
                ✕
              </button>
            )}
          </div>
        )
      })}
    </div>
  )
}

// ── Beliefs section (analyst-stated convictions: list + composer) ─────────────
function BeliefSection({
  ticker, members, highlightIds, onCreate, onDelete,
}: {
  ticker: string
  members: KgNode[]
  highlightIds?: Set<string>
  onCreate?: (ticker: string, field: string, value: unknown) => Promise<void>
  onDelete?: (id: string) => Promise<void>
}) {
  const [text, setText] = useState('')
  const [busy, setBusy] = useState(false)

  const rows = useMemo(
    () => [...members].sort((a, b) => b.updated_at - a.updated_at),
    [members],
  )

  const add = async () => {
    const t = text.trim()
    if (!t || !onCreate) return
    setBusy(true)
    // field = slugified text (stable id); value = { statement }.
    const field = t.toLowerCase().replace(/[^a-z0-9]+/g, '_').slice(0, 48).replace(/^_|_$/g, '')
    await onCreate(ticker, field || `belief_${Date.now()}`, { statement: t, stated_at: Date.now() / 1000 })
    setText('')
    setBusy(false)
  }

  return (
    <div>
      {/* Composer */}
      <div className="mb-3 rounded border border-emerald-500/25 bg-emerald-500/[0.04] p-2">
        <div className="text-[9px] uppercase tracking-wider text-emerald-400 mb-1.5">State a belief</div>
        <textarea
          value={text}
          onChange={e => setText(e.target.value)}
          rows={2}
          placeholder={`e.g. "${ticker} services margin will keep expanding through FY27"`}
          className="w-full bg-surface-2 border border-edge rounded px-2 py-1.5 text-ink text-[11px] placeholder:text-ink-dim focus:outline-none focus:border-emerald-500/50"
          onKeyDown={e => { if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) add() }}
        />
        <button
          onClick={add}
          disabled={busy || !text.trim()}
          className="mt-1.5 w-full px-2 py-1 rounded bg-emerald-500/20 text-emerald-300 border border-emerald-500/40 hover:bg-emerald-500/30 disabled:opacity-40 text-[11px]"
        >
          {busy ? 'Saving…' : '+ Add belief'}
        </button>
      </div>

      {/* List */}
      <div className="space-y-1.5">
        {rows.length === 0 && (
          <div className="text-ink-dim text-[11px]">No beliefs yet. Add one above — it locks into the KG and informs future runs.</div>
        )}
        {rows.map(n => {
          const hot = highlightIds?.has(n.id)
          const o = asObj(n.value)
          const statement = String(o.statement ?? o.text ?? n.value ?? n.field)
          return (
            <div
              key={n.id}
              className={`group rounded-md border bg-surface-2 border-l-2 border-l-up/50 px-3 py-2.5 transition ${
                hot ? 'border-accent/60 ring-1 ring-accent/40' : 'border-edge'
              }`}
            >
              <div className="flex items-start gap-2">
                <span className="flex-1 text-[12px] text-ink leading-snug">{statement}</span>
                {onDelete && (
                  <button
                    onClick={() => onDelete(n.id)}
                    className="opacity-0 group-hover:opacity-100 text-ink-dim hover:text-down transition"
                    title="Delete belief"
                  >
                    <X size={13} />
                  </button>
                )}
              </div>
              <div className="text-[9px] text-ink-dim mt-1">analyst-stated · locked</div>
            </div>
          )
        })}
      </div>
    </div>
  )
}

/**
 * Category body — the classified member view (drivers table / metrics table /
 * beliefs composer / fallback cards) WITHOUT panel chrome. Reused both by the
 * standalone KgTablePanel and by KgFinancialsPanel's category tabs.
 */
export function CategoryBody({
  members, highlightIds, beliefTicker, onCreateBelief, onDeleteNode,
}: {
  members: KgNode[]
  highlightIds?: Set<string>
  beliefTicker?: string
  onCreateBelief?: (ticker: string, field: string, value: unknown) => Promise<void>
  onDeleteNode?: (id: string) => Promise<void>
}) {
  const isDocFactDriver = (m: KgNode) => DRIVER_FACT_TYPES.has(baseFactType(m))
  const isDocFactMetric = (m: KgNode) => METRIC_FACT_TYPES.has(baseFactType(m))
  const isDocFact = (m: KgNode) => ['document_fact', 'key_fact', 'snippet_fact'].includes(m.node_type)
  const isDrivers = members.length > 0 && members.every(m => ['driver','risk','theme'].includes(m.node_type) || (isDocFact(m) && isDocFactDriver(m)))
  const isMetrics = members.length > 0 && members.every(m => m.node_type.startsWith('market_metric') || (isDocFact(m) && isDocFactMetric(m)))
  const isBeliefs = !!beliefTicker

  return (
    <>
      {isBeliefs && (
        <BeliefSection
          ticker={beliefTicker!}
          members={members}
          highlightIds={highlightIds}
          onCreate={onCreateBelief}
          onDelete={onDeleteNode}
        />
      )}
      {!isBeliefs && members.length === 0 && <div className="text-ink-dim text-[12px]">No data.</div>}
      {!isBeliefs && isDrivers && <DriverTable members={members} highlightIds={highlightIds} onDelete={onDeleteNode} />}
      {!isBeliefs && isMetrics && <MetricTable members={members} highlightIds={highlightIds} onDelete={onDeleteNode} />}
      {!isBeliefs && !isDrivers && !isMetrics && members.length > 0 && (
        <div className="space-y-2">
          {[...members].sort((a, b) => b.updated_at - a.updated_at).map(n => {
            const hot = highlightIds?.has(n.id)
            return (
              <div
                key={n.id}
                className={`group rounded-md border bg-surface-2 border-l-2 border-l-accent/40 px-3 py-2.5 transition ${
                  hot ? 'border-accent/60 ring-1 ring-accent/40' : 'border-edge'
                }`}
              >
                <div className="flex items-center justify-between">
                  <div className="text-[10px] uppercase tracking-wide text-ink-dim mb-1">{n.field}</div>
                  {onDeleteNode && (
                    <button
                      onClick={() => onDeleteNode(n.id)}
                      className="text-ink-dim hover:text-rose-400 opacity-0 group-hover:opacity-100 transition-opacity mb-1"
                      title="Delete"
                    >
                      ✕
                    </button>
                  )}
                </div>
                <KgValueView value={n.value} nodeType={n.node_type} />
              </div>
            )
          })}
        </div>
      )}
    </>
  )
}

// Base fact_type for a document_fact node: prefer value.fact_type, else strip
// the "::period" suffix from the field key.
function baseFactType(m: KgNode): string {
  const v = asObj(m.value)
  if (typeof v.fact_type === 'string') return v.fact_type
  return m.field.split('::')[0]
}
const DRIVER_FACT_TYPES = new Set(['risk_factor', 'competitive_moat', 'wacc_signal'])
const METRIC_FACT_TYPES = new Set([
  'revenue', 'base_revenue', 'earnings', 'net_income', 'operating_income',
  'gross_profit', 'eps', 'shares_outstanding', 'margin', 'free_cash_flow',
  'growth_rate', 'fcff_margin', 'debt_metric', 'net_debt', 'valuation_metric',
  'capital_allocation', 'effective_tax_rate', 'ebitda_margin',
])

/**
 * Category table panel — tier-3 detail for a single Financials category.
 * Drivers get a dedup'd, filterable table; metrics get field→value rows;
 * everything else falls back to KgValueView cards.
 */
export function KgTablePanel({
  title, subtitle, members, highlightIds, onClose,
  beliefTicker, onCreateBelief, onDeleteNode,
}: Props) {
  const isBeliefs = !!beliefTicker
  const isDrivers = members.length > 0 && members.every(m => ['driver','risk','theme'].includes(m.node_type) || DRIVER_FACT_TYPES.has(baseFactType(m)))
  const isMetrics = members.length > 0 && members.every(m => m.node_type.startsWith('market_metric') || METRIC_FACT_TYPES.has(baseFactType(m)))
  const Icon = isBeliefs ? Lightbulb : isDrivers ? ListChecks : isMetrics ? BarChart3 : FileText
  return (
    <Panel icon={<Icon size={16} />} title={title} subtitle={subtitle} onClose={onClose}>
      <div className="p-4">
        <CategoryBody
          members={members}
          highlightIds={highlightIds}
          beliefTicker={beliefTicker}
          onCreateBelief={onCreateBelief}
          onDeleteNode={onDeleteNode}
        />
      </div>
    </Panel>
  )
}
