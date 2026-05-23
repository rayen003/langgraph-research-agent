import { useMemo, useState } from 'react'
import type { KgNode } from '../hooks/useKnowledgeGraph'

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

export function KgRunInspector({ runNode, allNodes, onClose, onRerun, rerunBusy }: Props) {
  // ── Extract this run's assumptions + outputs ────────────────────────────
  const runId = runNode.run_id || ''
  const ticker = runNode.ticker

  const { assumptions, outputs, horizonYears } = useMemo(() => {
    const a: KgNode[] = []
    const o: KgNode[] = []
    for (const n of allNodes) {
      if (n.run_id !== runId) continue
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
    <div className="absolute top-3 right-3 z-20 w-[420px] max-h-[calc(100vh-100px)] bg-[#11111a] border border-[#2a2a36] rounded-md shadow-2xl flex flex-col text-[11px]">
      {/* Header */}
      <div className="px-3 py-2 border-b border-[#2a2a36] flex items-center justify-between flex-shrink-0">
        <div>
          <div className="text-zinc-200 font-medium text-[12px]">
            {ticker} · DCF Run · {horizonYears}y
          </div>
          <div className="text-zinc-600 text-[10px] mt-0.5">
            {runId.slice(0, 24)}… · {ageStr(runNode.updated_at)}
          </div>
        </div>
        <button onClick={onClose} className="text-zinc-500 hover:text-zinc-300 text-[13px]">✕</button>
      </div>

      <div className="overflow-y-auto flex-1">
        {/* Assumptions */}
        <div className="px-3 py-2 border-b border-[#2a2a36]">
          <div className="text-[9px] uppercase text-zinc-600 tracking-wider mb-1.5">
            Assumptions
          </div>
          <div className="space-y-1">
            {assumptions.map(a => {
              const editable = TIER_B_EDITABLE.has(a.field)
              const locked = TIER_A_LOCKED.has(a.field)
              return (
                <div key={a.id} className="flex items-center gap-2">
                  <div className="w-32 flex-shrink-0 text-zinc-400">
                    {ASSUMPTION_LABELS[a.field] || a.field}
                  </div>
                  {editable ? (
                    <input
                      value={draft[a.field] ?? ''}
                      onChange={e => updateField(a.field, e.target.value)}
                      className="flex-1 bg-[#0a0a0a] border border-[#2a2a36] rounded px-1.5 py-0.5 text-zinc-200 font-mono text-[10px] focus:outline-none focus:border-indigo-500/50"
                      placeholder="—"
                    />
                  ) : (
                    <div className="flex-1 text-zinc-500 font-mono text-[10px] truncate">
                      {fmtNum(a.value)}
                    </div>
                  )}
                  <div className="w-12 text-right text-zinc-600 text-[9px]">
                    {locked ? 'LOCKED' : editable ? (isRatio(a.field) ? '%' : '') : ''}
                  </div>
                </div>
              )
            })}
            {assumptions.length === 0 && (
              <div className="text-zinc-600 text-[10px]">No assumptions persisted for this run.</div>
            )}
          </div>

          {dirty && (
            <button
              onClick={resetDraft}
              className="mt-2 text-[10px] text-zinc-500 hover:text-zinc-300"
            >
              ↶ reset edits
            </button>
          )}
        </div>

        {/* Outputs */}
        <div className="px-3 py-2 border-b border-[#2a2a36]">
          <div className="text-[9px] uppercase text-zinc-600 tracking-wider mb-1.5">
            Outputs
          </div>
          <div className="space-y-1">
            {outputs.map(o => {
              const isPrice = o.field === 'implied_share_price' || o.field === 'current_price'
              return (
                <div key={o.id} className="flex items-center gap-2">
                  <div className="w-36 flex-shrink-0 text-zinc-400">
                    {OUTPUT_LABELS[o.field] || o.field}
                  </div>
                  <div className={`flex-1 font-mono text-[10px] truncate ${
                    o.field === 'implied_share_price' ? 'text-emerald-300' : 'text-zinc-300'
                  }`}>
                    {isPrice ? `$${fmtNum(o.value)}` : fmtNum(o.value)}
                  </div>
                </div>
              )
            })}
            {outputs.length === 0 && (
              <div className="text-zinc-600 text-[10px]">No outputs persisted.</div>
            )}
          </div>
        </div>

        {/* Confidence */}
        <div className="px-3 py-2 border-b border-[#2a2a36] flex items-center gap-2">
          <div className="text-zinc-400">Confidence:</div>
          <div className="text-zinc-300 font-mono">
            {(runNode.confidence * 100).toFixed(0)}%
          </div>
          <div className="ml-auto text-[10px] text-zinc-500">
            source: {runNode.source}
          </div>
        </div>
      </div>

      {/* Footer actions */}
      <div className="px-3 py-2 border-t border-[#2a2a36] flex gap-2 flex-shrink-0">
        <button
          onClick={() => handleRerun(true)}
          disabled={rerunBusy || !dirty}
          className="flex-1 px-2 py-1.5 rounded bg-indigo-500/20 text-indigo-300 border border-indigo-500/40 hover:bg-indigo-500/30 disabled:opacity-40 text-[11px]"
          title={!dirty ? 'Edit a Tier B assumption first' : 'Run DCF with edited assumptions'}
        >
          {rerunBusy ? 'Running…' : '↻ Rerun with edits'}
        </button>
        <button
          onClick={() => handleRerun(false)}
          disabled={rerunBusy}
          className="px-2 py-1.5 rounded bg-zinc-800 text-zinc-400 border border-zinc-700 hover:bg-zinc-700 disabled:opacity-40 text-[11px]"
          title="Clone this run with the same assumptions"
        >
          Clone
        </button>
      </div>
    </div>
  )
}
