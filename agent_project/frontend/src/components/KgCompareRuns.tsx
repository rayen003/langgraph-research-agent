import { useMemo, useState, useEffect, useRef } from 'react'
import { GitCompare, MessageSquare, X, Check, Plus } from 'lucide-react'
import type { KgNode } from '../hooks/useKnowledgeGraph'
import { Markdown } from './kg/Markdown'

interface ChatTurn { role: 'user' | 'assistant'; content: string }

interface Props {
  nodes: KgNode[]
  /** run ids the user has assembled into the comparison (composite ticker::run_id). */
  selectedRunKeys: string[]
  onToggleRun: (key: string) => void
  onClear: () => void
  /** ask the comparison side-chat; returns answer text. */
  onChat: (diff: unknown, question: string, history?: ChatTurn[]) => Promise<string | null>
  onClose: () => void
}

const ASSUMPTION_LABELS: Record<string, string> = {
  revenue_growth: 'Revenue growth', fcff_margin: 'FCFF margin', wacc: 'WACC',
  terminal_growth: 'Terminal growth', tax_rate: 'Tax rate', base_revenue: 'Base revenue',
  shares_outstanding: 'Shares out', net_debt: 'Net debt',
  fcff_margin_terminal: 'FCFF margin (term)', revenue_growth_terminal: 'Rev growth (term)',
  sbc_pct_revenue: 'SBC % revenue', buyback_yield: 'Buyback yield',
}
const OUTPUT_LABELS: Record<string, string> = {
  implied_share_price: 'Implied price', equity_value: 'Equity value',
  enterprise_value: 'Enterprise value', terminal_pv: 'Terminal PV',
  pv_cash_flows: 'PV cash flows', current_price: 'Spot price',
}
const RATIO_FIELDS = new Set([
  'revenue_growth', 'fcff_margin', 'wacc', 'terminal_growth', 'tax_rate',
  'fcff_margin_terminal', 'revenue_growth_terminal', 'sbc_pct_revenue', 'buyback_yield',
])

function num(v: unknown): number | null {
  const n = typeof v === 'number' ? v : Number(v)
  return isFinite(n) ? n : null
}
function fmt(field: string, v: unknown): string {
  const n = num(v)
  if (n === null) return v == null ? '—' : String(v)
  if (RATIO_FIELDS.has(field)) return (n * 100).toFixed(2) + '%'
  if (Math.abs(n) >= 1e6) return n.toLocaleString(undefined, { maximumFractionDigits: 0 })
  if (Math.abs(n) >= 1) return n.toFixed(2)
  return n.toFixed(4)
}
function fmtDate(ts: number): string {
  return new Date(ts * 1000).toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
}
function runKey(n: KgNode): string { return `${n.ticker}::${n.run_id || n.id}` }

interface RunCol { key: string; runId: string; ticker: string; label: string; updated: number; node: KgNode }

/**
 * Assembled cross-run comparison artifact + side-chat.
 *
 * Runs are NOT auto-loaded — the user assembles the set by clicking DCF run
 * nodes on the canvas, dragging them onto this panel, or toggling the checklist
 * here. The diff matrix + side-chat operate only on the assembled set.
 */
export function KgCompareRuns({
  nodes, selectedRunKeys, onToggleRun, onClear, onChat, onClose,
}: Props) {
  const selected = useMemo(() => new Set(selectedRunKeys), [selectedRunKeys])

  // All dcf_run nodes, grouped by ticker, for the picker checklist.
  const allRuns = useMemo(() => {
    const runs = nodes.filter(n => n.node_type === 'dcf_run')
    runs.sort((a, b) => (a.ticker.localeCompare(b.ticker)) || (a.updated_at - b.updated_at))
    return runs
  }, [nodes])

  // Assembled columns (only selected), sorted oldest→newest.
  const runCols = useMemo<RunCol[]>(() => {
    const cols: RunCol[] = []
    for (const n of allRuns) {
      if (!selected.has(runKey(n))) continue
      const o = (n.value && typeof n.value === 'object') ? n.value as Record<string, unknown> : {}
      const horizon = o.horizon_years ? `${o.horizon_years}y` : ''
      cols.push({
        key: runKey(n), runId: n.run_id || n.id, ticker: n.ticker,
        label: `${n.ticker} · ${fmtDate(n.updated_at)}${horizon ? ' · ' + horizon : ''}`,
        updated: n.updated_at, node: n,
      })
    }
    cols.sort((a, b) => a.updated - b.updated)
    return cols
  }, [allRuns, selected])

  const selectedRunIds = useMemo(() => new Set(runCols.map(c => c.runId)), [runCols])

  // Build field → runId → value rows (only for selected runs).
  const { assumptionRows, outputRows } = useMemo(() => {
    const assumption = new Map<string, Map<string, unknown>>()
    const output = new Map<string, Map<string, unknown>>()
    for (const n of nodes) {
      if (!n.run_id || !selectedRunIds.has(n.run_id)) continue
      const target = n.node_type === 'run_assumption' ? assumption
        : n.node_type === 'run_output' ? output : null
      if (!target) continue
      const row = target.get(n.field) || new Map<string, unknown>()
      row.set(n.run_id, n.value)
      target.set(n.field, row)
    }
    const order = (m: Map<string, Map<string, unknown>>, labels: Record<string, string>) =>
      Array.from(m.entries()).sort((a, b) =>
        (labels[a[0]] || a[0]).localeCompare(labels[b[0]] || b[0]))
    return { assumptionRows: order(assumption, ASSUMPTION_LABELS), outputRows: order(output, OUTPUT_LABELS) }
  }, [nodes, selectedRunIds])

  // Structured diff payload for the side-chat (matches kg/compare.py shape).
  const diffPayload = useMemo(() => {
    const buildRows = (rows: [string, Map<string, unknown>][], labels: Record<string, string>) =>
      rows.map(([field, byRun]) => ({
        field, label: labels[field] || field, is_ratio: RATIO_FIELDS.has(field),
        values: runCols.map(c => byRun.get(c.runId) ?? null),
      }))
    return {
      ticker: runCols[0]?.ticker || '?',
      runs: runCols.map(c => {
        const o = (c.node.value && typeof c.node.value === 'object') ? c.node.value as Record<string, unknown> : {}
        return {
          run_id: c.runId, label: c.label, date: fmtDate(c.updated),
          trigger: o.trigger ?? 'initial', parent_run_id: o.parent_run_id ?? null,
        }
      }),
      assumptions: buildRows(assumptionRows, ASSUMPTION_LABELS),
      outputs: buildRows(outputRows, OUTPUT_LABELS),
    }
  }, [runCols, assumptionRows, outputRows])

  // ── Side-chat state ────────────────────────────────────────────────────────
  const [chat, setChat] = useState<ChatTurn[]>([])
  const [draft, setDraft] = useState('')
  const [busy, setBusy] = useState(false)
  const [showChat, setShowChat] = useState(false)
  const chatEndRef = useRef<HTMLDivElement | null>(null)
  useEffect(() => { chatEndRef.current?.scrollIntoView({ behavior: 'smooth' }) }, [chat, busy])

  const ask = async () => {
    const q = draft.trim()
    if (!q || runCols.length < 2) return
    setDraft('')
    const history = chat.slice()
    setChat(c => [...c, { role: 'user', content: q }])
    setBusy(true)
    const answer = await onChat(diffPayload, q, history)
    setChat(c => [...c, { role: 'assistant', content: answer || 'No response.' }])
    setBusy(false)
  }

  // ── Drop zone (canvas drag-to-add) ──────────────────────────────────────────
  const [dragOver, setDragOver] = useState(false)
  const onDrop = (e: React.DragEvent) => {
    e.preventDefault(); setDragOver(false)
    const key = e.dataTransfer.getData('application/x-kg-run')
    if (key && !selected.has(key)) onToggleRun(key)
  }

  const renderSection = (title: string, rows: [string, Map<string, unknown>][], labels: Record<string, string>) => (
    <div className="mb-4">
      <div className="text-[11px] uppercase tracking-wide text-ink-dim font-medium mb-2 px-1">{title}</div>
      <div className="space-y-0.5">
        {rows.map(([field, byRun]) => {
          const cells = runCols.map(rc => byRun.get(rc.runId))
          return (
            <div key={field} className="flex items-center gap-1 text-[11px] py-0.5">
              <div className="w-28 flex-shrink-0 text-ink-muted truncate">{labels[field] || field}</div>
              {cells.map((v, i) => {
                const prev = i > 0 ? num(cells[i - 1]) : null
                const cur = num(v)
                let cls = 'text-ink'; let arrow = ''
                if (prev !== null && cur !== null && prev !== cur) {
                  const up = cur > prev
                  cls = up ? 'text-up' : 'text-down'
                  arrow = up ? ' ▲' : ' ▼'
                }
                return <div key={i} className={`flex-1 text-right font-mono tabular-nums ${cls} truncate`}>{fmt(field, v)}{arrow}</div>
              })}
            </div>
          )
        })}
        {rows.length === 0 && <div className="text-ink-dim text-[11px] px-1">No data.</div>}
      </div>
    </div>
  )

  return (
    <div
      onDragOver={e => { e.preventDefault(); setDragOver(true) }}
      onDragLeave={() => setDragOver(false)}
      onDrop={onDrop}
      className={`h-full flex flex-col bg-surface transition ${
        dragOver ? 'ring-2 ring-inset ring-warn/50' : ''
      }`}
    >
      <header className="flex items-center gap-2 px-4 h-12 border-b border-edge flex-shrink-0">
        <GitCompare size={16} className="text-ink-muted" />
        <span className="text-[14px] font-medium text-ink">Compare</span>
        <span className="text-[11px] text-ink-dim tabular-nums">{runCols.length}</span>
        {runCols.length >= 2 && (
          <button
            onClick={() => setShowChat(s => !s)}
            className={`ml-2 flex items-center gap-1 text-[11px] px-2 py-1 rounded-md border transition ${
              showChat ? 'bg-accent-soft text-accent border-accent/40'
                : 'text-ink-muted border-edge hover:bg-surface-2'
            }`}
          ><MessageSquare size={12} /> Discuss</button>
        )}
        {runCols.length > 0 && (
          <button onClick={onClear} className="text-[11px] text-ink-dim hover:text-ink transition">clear</button>
        )}
        <button onClick={onClose} aria-label="Close" className="ml-auto text-ink-dim hover:text-ink p-1 -mr-1 rounded hover:bg-surface-2 transition">
          <X size={16} />
        </button>
      </header>

      {/* Run picker */}
      <div className="px-4 py-3 border-b border-edge flex-shrink-0">
        <div className="text-[11px] uppercase tracking-wide text-ink-dim font-medium mb-2">
          Add runs <span className="text-ink-dim/70 normal-case tracking-normal">· click nodes, drag here, or toggle</span>
        </div>
        <div className="flex flex-wrap gap-1.5 max-h-24 overflow-y-auto">
          {allRuns.map(n => {
            const key = runKey(n)
            const on = selected.has(key)
            return (
              <button
                key={key}
                onClick={() => onToggleRun(key)}
                className={`flex items-center gap-1 px-2 py-1 rounded-md text-[11px] font-mono border transition ${
                  on ? 'bg-accent-soft text-accent border-accent/50'
                    : 'text-ink-dim border-edge hover:text-ink hover:border-edge-2'
                }`}
              >
                {on ? <Check size={11} /> : <Plus size={11} />}{n.ticker} {fmtDate(n.updated_at)}
              </button>
            )
          })}
          {allRuns.length === 0 && <span className="text-ink-dim text-[11px]">No DCF runs yet.</span>}
        </div>
      </div>

      {runCols.length === 0 ? (
        <div className="flex-1 flex items-center justify-center p-6 text-center text-ink-dim text-[12px]">
          {dragOver ? 'Drop run to add' : 'Add ≥2 runs above, or drag DCF nodes from the graph.'}
        </div>
      ) : (
        <div className="overflow-auto flex-1 p-4">
          <div className="flex items-center gap-1 mb-2 sticky top-0 bg-surface pb-2">
            <div className="w-28 flex-shrink-0" />
            {runCols.map((rc, i) => (
              <div key={rc.key} className="flex-1 text-right group">
                <div className="text-ink text-[11px] font-medium truncate">{rc.label}</div>
                <div className="text-ink-dim text-[10px] flex items-center justify-end gap-1">
                  {i === 0 ? 'baseline' : 'vs ←'}
                  <button onClick={() => onToggleRun(rc.key)} aria-label="Remove run" className="text-ink-dim hover:text-down transition">
                    <X size={10} />
                  </button>
                </div>
              </div>
            ))}
          </div>
          {renderSection('Assumptions', assumptionRows, ASSUMPTION_LABELS)}
          {renderSection('Outputs', outputRows, OUTPUT_LABELS)}
          <div className="text-[10px] text-ink-dim mt-2 px-1">▲ / ▼ vs previous column</div>
        </div>
      )}

      {/* Side-chat */}
      {showChat && runCols.length >= 2 && (
        <div className="border-t border-edge flex flex-col flex-shrink-0" style={{ maxHeight: '42vh' }}>
          <div className="px-4 py-2 text-[11px] uppercase tracking-wide text-ink-dim font-medium border-b border-edge">
            Discuss this comparison
          </div>
          <div className="overflow-y-auto flex-1 p-4 space-y-3 min-h-[90px]">
            {chat.length === 0 && (
              <div className="text-ink-dim text-[12px] leading-relaxed">
                Ask about the changes — “what drove the implied price move?” · “which assumption changed most?”
              </div>
            )}
            {chat.map((t, i) => (
              <div key={i}>
                <div className={`text-[10px] uppercase tracking-wide mb-1 ${t.role === 'user' ? 'text-accent' : 'text-up'}`}>
                  {t.role === 'user' ? 'you' : 'agent'}
                </div>
                {t.role === 'user'
                  ? <div className="text-[13px] text-ink leading-relaxed">{t.content}</div>
                  : <Markdown text={t.content} />}
              </div>
            ))}
            {busy && <div className="text-ink-dim text-[12px]">thinking…</div>}
            <div ref={chatEndRef} />
          </div>
          <div className="p-3 border-t border-edge flex gap-1.5">
            <input
              value={draft}
              onChange={e => setDraft(e.target.value)}
              onKeyDown={e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); ask() } }}
              placeholder="Ask about these runs…"
              className="flex-1 bg-surface-2 border border-edge rounded-md px-2.5 py-1.5 text-ink text-[13px] placeholder:text-ink-dim focus:outline-none focus:border-accent/50"
            />
            <button
              onClick={ask}
              disabled={busy || !draft.trim()}
              className="px-3 py-1.5 rounded-md bg-accent-soft text-accent border border-accent/40 hover:bg-accent/20 disabled:opacity-40 text-[13px] font-medium transition"
            >Ask</button>
          </div>
        </div>
      )}
    </div>
  )
}
