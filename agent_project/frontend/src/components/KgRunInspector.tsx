import { useMemo, useState } from 'react'
import { Calculator, RotateCw, RotateCcw, Copy, Lock, FileText } from 'lucide-react'
import type { KgNode } from '../hooks/useKnowledgeGraph'
import { Panel, Caption } from './kg/Panel'

// Tier A: locked (canonical fundamentals). Tier B: editable assumptions.
const TIER_B_EDITABLE = new Set([
  'revenue_growth',
  'fcff_margin',
  'wacc',
  'terminal_growth',
  'tax_rate',
])

const TIER_A_LOCKED = new Set([
  'base_revenue',
  'shares_outstanding',
  'net_debt',
])

const ASSUMPTION_LABELS: Record<string, string> = {
  revenue_growth: 'Revenue growth',
  fcff_margin: 'FCFF margin',
  wacc: 'WACC',
  terminal_growth: 'Terminal growth',
  tax_rate: 'Tax rate',
  base_revenue: 'Base revenue',
  shares_outstanding: 'Shares out',
  net_debt: 'Net debt',
  fcff_margin_terminal: 'FCFF margin (term)',
  revenue_growth_terminal: 'Rev growth (term)',
  sbc_pct_revenue: 'SBC % revenue',
  buyback_yield: 'Buyback yield',
}

const OUTPUT_LABELS: Record<string, string> = {
  implied_share_price: 'Implied share price',
  equity_value: 'Equity value',
  enterprise_value: 'Enterprise value',
  terminal_pv: 'Terminal PV',
  pv_cash_flows: 'PV cash flows',
  current_price: 'Spot price',
}

interface Props {
  runNode: KgNode                     // the dcf_run node
  allNodes: KgNode[]                  // entire graph (to find run_assumption/run_output children)
  onClose: () => void
  onRerun: (overrides: Record<string, number>, horizonYears: number) => Promise<void>
  rerunBusy: boolean
  /** raw node ids matched by a query → glow those assumption/output rows. */
  highlightIds?: Set<string>
}

function fmtNum(v: unknown): string {
  if (typeof v !== 'number' || !isFinite(v)) return String(v ?? '—')
  if (Math.abs(v) >= 1e6) return v.toLocaleString(undefined, { maximumFractionDigits: 0 })
  if (Math.abs(v) >= 1) return v.toFixed(4)
  return v.toFixed(6)
}

function fmtPct(v: unknown): string {
  if (typeof v !== 'number' || !isFinite(v)) return '—'
  return (v * 100).toFixed(2) + '%'
}

function isRatio(field: string): boolean {
  return ['revenue_growth', 'fcff_margin', 'wacc', 'terminal_growth', 'tax_rate'].includes(field)
}

function ageStr(ts: number): string {
  const age = Math.max(0, Date.now() / 1000 - ts)
  if (age < 60) return `${Math.round(age)}s ago`
  if (age < 3600) return `${Math.round(age / 60)}m ago`
  if (age < 86400) return `${Math.round(age / 3600)}h ago`
  return `${Math.round(age / 86400)}d ago`
}

export function KgRunInspector({ runNode, allNodes, onClose, onRerun, rerunBusy, highlightIds }: Props) {
  // ── Extract this run's assumptions + outputs ────────────────────────────
  const runId = runNode.run_id || ''
  const ticker = runNode.ticker

  const { assumptions, outputs, horizonYears } = useMemo(() => {
    const a: KgNode[] = []
    const o: KgNode[] = []
    for (const n of allNodes) {
      // Must match BOTH run_id AND ticker — multiple tickers can share the same
      // run_id (e.g. "workflow_dcf") when runs are batched in one session.
      if (n.run_id !== runId || n.ticker !== ticker) continue
      if (n.node_type === 'run_assumption') a.push(n)
      else if (n.node_type === 'run_output') o.push(n)
    }
    // sort assumptions: tier B first, then tier A
    a.sort((x, y) => {
      const xt = TIER_B_EDITABLE.has(x.field) ? 0 : 1
      const yt = TIER_B_EDITABLE.has(y.field) ? 0 : 1
      if (xt !== yt) return xt - yt
      return x.field.localeCompare(y.field)
    })
    o.sort((x, y) => x.field.localeCompare(y.field))

    const hy = runNode.value && typeof runNode.value === 'object' && 'horizon_years' in runNode.value
      ? Number((runNode.value as Record<string, unknown>).horizon_years) || 5
      : 5

    return { assumptions: a, outputs: o, horizonYears: hy }
  }, [allNodes, runId, runNode.value])

  // ── Editable assumption draft ────────────────────────────────────────────
  const initialDraft = useMemo(() => {
    const d: Record<string, string> = {}
    for (const a of assumptions) {
      const v = typeof a.value === 'number' ? a.value : Number(a.value)
      d[a.field] = isFinite(v) ? String(v) : ''
    }
    return d
  }, [assumptions])

  const [draft, setDraft] = useState<Record<string, string>>(initialDraft)
  const [dirty, setDirty] = useState(false)

  // The persisted DCF report lives at /runs/{thread_id}/dcf-report.pdf. The
  // dcf_run node stores its thread_id; open the PDF inline in a new tab.
  const threadId = runNode.value && typeof runNode.value === 'object'
    ? String((runNode.value as Record<string, unknown>).thread_id ?? '')
    : ''
  const reportUrl = threadId
    ? `/runs/${encodeURIComponent(threadId)}/dcf-report.pdf?inline=1`
    : ''

  function updateField(field: string, value: string) {
    setDraft(prev => ({ ...prev, [field]: value }))
    setDirty(true)
  }

  function resetDraft() {
    setDraft(initialDraft)
    setDirty(false)
  }

  async function handleRerun(useEdited: boolean) {
    // Compose overrides: send ALL assumptions (Tier A + Tier B), since backend
    // fast path expects full all_assumptions dict. Tier A pulled from original
    // values; Tier B from draft (if editing) or original.
    const overrides: Record<string, number> = {}
    for (const a of assumptions) {
      const orig = typeof a.value === 'number' ? a.value : Number(a.value)
      if (useEdited && TIER_B_EDITABLE.has(a.field)) {
        const parsed = Number(draft[a.field])
        overrides[a.field] = isFinite(parsed) ? parsed : orig
      } else {
        overrides[a.field] = orig
      }
    }
    await onRerun(overrides, horizonYears)
  }

  return (
    <Panel
      icon={<Calculator size={16} />}
      title={`${ticker} · DCF Run · ${horizonYears}y`}
      subtitle={`${runId.slice(0, 24)}… · ${ageStr(runNode.updated_at)}`}
      onClose={onClose}
      actions={reportUrl ? (
        <a
          href={reportUrl}
          target="_blank"
          rel="noreferrer"
          title="Open the full DCF report (PDF) in a new tab"
          className="flex items-center gap-1.5 px-2 py-1 rounded-md text-[11px] text-accent border border-accent/40 bg-accent-soft hover:bg-accent/20 transition"
        >
          <FileText size={12} /> Report ↗
        </a>
      ) : undefined}
      footer={
        <div className="flex gap-2">
          <button
            onClick={() => handleRerun(true)}
            disabled={rerunBusy || !dirty}
            className="flex-1 flex items-center justify-center gap-1.5 px-2 py-2 rounded-md bg-accent-soft text-accent border border-accent/40 hover:bg-accent/20 disabled:opacity-40 text-[12px] font-medium transition"
            title={!dirty ? 'Edit a Tier B assumption first' : 'Run DCF with edited assumptions'}
          >
            <RotateCw size={13} className={rerunBusy ? 'animate-spin' : ''} />
            {rerunBusy ? 'Running…' : 'Rerun with edits'}
          </button>
          <button
            onClick={() => handleRerun(false)}
            disabled={rerunBusy}
            className="flex items-center gap-1.5 px-3 py-2 rounded-md text-ink-muted border border-edge hover:bg-surface-2 disabled:opacity-40 text-[12px] transition"
            title="Clone this run with the same assumptions"
          >
            <Copy size={13} /> Clone
          </button>
        </div>
      }
    >
      {/* Assumptions */}
      <div className="px-4 py-3 border-b border-edge">
        <Caption>Assumptions</Caption>
        <div className="space-y-1">
          {assumptions.map(a => {
            const editable = TIER_B_EDITABLE.has(a.field)
            const locked = TIER_A_LOCKED.has(a.field)
            const hot = highlightIds?.has(a.id)
            return (
              <div key={a.id} className={`flex items-center gap-2 rounded-md px-1.5 py-1 -mx-1.5 transition ${
                hot ? 'ring-1 ring-accent/50 bg-accent-soft' : ''
              }`}>
                <div
                  className="w-36 flex-shrink-0 text-[12px] text-ink-muted truncate"
                  title={ASSUMPTION_LABELS[a.field] || a.field}
                >
                  {ASSUMPTION_LABELS[a.field] || a.field}
                </div>
                {editable ? (
                  <input
                    value={draft[a.field] ?? ''}
                    onChange={e => updateField(a.field, e.target.value)}
                    className="flex-1 bg-surface-2 border border-edge rounded px-2 py-1 text-ink font-mono text-[12px] focus:outline-none focus:border-accent/50"
                    placeholder="—"
                  />
                ) : (
                  <div className="flex-1 text-ink-muted font-mono text-[12px] tabular-nums truncate">
                    {fmtNum(a.value)}
                  </div>
                )}
                <div className="w-12 flex justify-end text-ink-dim text-[10px]">
                  {locked ? <Lock size={11} /> : editable ? (isRatio(a.field) ? '%' : '') : ''}
                </div>
              </div>
            )
          })}
          {assumptions.length === 0 && (
            <div className="text-ink-dim text-[12px]">No assumptions persisted for this run.</div>
          )}
        </div>
        {dirty && (
          <button onClick={resetDraft} className="mt-2 flex items-center gap-1 text-[11px] text-ink-dim hover:text-ink transition">
            <RotateCcw size={11} /> reset edits
          </button>
        )}
      </div>

      {/* Outputs */}
      <div className="px-4 py-3 border-b border-edge">
        <Caption>Outputs</Caption>
        <div className="space-y-1">
          {outputs.map(o => {
            const isPrice = o.field === 'implied_share_price' || o.field === 'current_price'
            const hot = highlightIds?.has(o.id)
            return (
              <div key={o.id} className={`flex items-center gap-2 rounded-md px-1.5 py-1 -mx-1.5 transition ${
                hot ? 'ring-1 ring-accent/50 bg-accent-soft' : ''
              }`}>
                <div
                  className="w-36 flex-shrink-0 text-[12px] text-ink-muted truncate"
                  title={OUTPUT_LABELS[o.field] || o.field}
                >
                  {OUTPUT_LABELS[o.field] || o.field}
                </div>
                <div className={`flex-1 font-mono text-[12px] tabular-nums truncate ${
                  o.field === 'implied_share_price' ? 'text-accent font-medium' : 'text-ink'
                }`}>
                  {isPrice ? `$${fmtNum(o.value)}` : fmtNum(o.value)}
                </div>
              </div>
            )
          })}
          {outputs.length === 0 && (
            <div className="text-ink-dim text-[12px]">No outputs persisted.</div>
          )}
        </div>
      </div>

      {/* Confidence */}
      <div className="px-4 py-3 flex items-center gap-2 text-[12px]">
        <span className="text-ink-muted">Confidence</span>
        <span className="text-ink font-mono tabular-nums">{(runNode.confidence * 100).toFixed(0)}%</span>
        <span className="ml-auto text-[11px] text-ink-dim">{runNode.source}</span>
      </div>
    </Panel>
  )
}
