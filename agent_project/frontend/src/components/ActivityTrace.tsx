import { useState, useMemo } from 'react'
import type { ConfidenceBreakdown, DcfReviewState, EvidenceItem, StepState, ToolCall } from '../types'
import type { ActivityEntry, ActivityScope } from '../lib/activity'
import { activityStatusToToolStatus } from '../lib/activity'
import { cleanToolSummary, getToolDisplay } from '../lib/toolLabels'

// Re-export for backward compat
export { activityStatusToToolStatus }

/**
 * Convert a unified ActivityEntry[] into the legacy ToolCall[] shape.
 */
export function activitiesToToolCalls(
  entries: ActivityEntry[],
  options: { scope?: ActivityScope; stepId?: string } = {},
): ToolCall[] {
  const { scope, stepId } = options
  const safe = Array.isArray(entries) ? entries : []
  return safe
    .filter(e => {
      if (!e || typeof e !== 'object') return false
      if (scope && e.scope !== scope) return false
      if (stepId && e.step_id !== stepId) return false
      return true
    })
    .map(e => ({
      tool_name: e.name || 'unknown',
      status:
        e.status === 'completed' || e.status === 'skipped'
          ? 'done'
          : e.status === 'error'
            ? 'error'
            : 'running',
      summary: String(e.summary || e.error || ''),
      args_preview: String(e.args_preview || ''),
    }))
}

// ── Grouping helpers ──────────────────────────────────────────────────────────

type FlatRow = { kind: 'flat'; entry: ActivityEntry }
type WorkflowGroup = { kind: 'group'; parent: ActivityEntry; children: ActivityEntry[] }
type RowItem = FlatRow | WorkflowGroup

function groupActivities(entries: ActivityEntry[], scope?: ActivityScope): RowItem[] {
  const safe = Array.isArray(entries) ? entries : []
  const filtered = scope ? safe.filter(e => !scope || e.scope === scope || e.scope === 'workflow') : safe

  // Collect workflow_step children keyed by parent_activity_id
  const childIds = new Set<string>()
  const childrenByParent = new Map<string, ActivityEntry[]>()
  for (const e of filtered) {
    if (e.kind === 'workflow_step' && e.parent_activity_id) {
      childIds.add(e.activity_id)
      const list = childrenByParent.get(e.parent_activity_id) ?? []
      list.push(e)
      childrenByParent.set(e.parent_activity_id, list)
    }
  }

  // Check if a workflow:dcf group exists — if so, suppress the flat run_dcf_workflow tool row
  const hasDcfGroup = filtered.some(e => e.kind === 'workflow' && e.name === 'workflow:dcf')

  const result: RowItem[] = []
  for (const e of filtered) {
    if (childIds.has(e.activity_id)) continue // rendered inside group
    // Suppress redundant flat run_dcf_workflow row when workflow group is present
    if (hasDcfGroup && e.kind === 'tool' && e.name === 'run_dcf_workflow') continue
    if (e.kind === 'workflow' || childrenByParent.has(e.activity_id)) {
      result.push({ kind: 'group', parent: e, children: childrenByParent.get(e.activity_id) ?? [] })
    } else {
      result.push({ kind: 'flat', entry: e })
    }
  }
  return result
}

function entryToRow(e: ActivityEntry): ToolCall {
  return {
    tool_name: e.name || 'unknown',
    status: e.status === 'completed' || e.status === 'skipped' ? 'done' : e.status === 'error' ? 'error' : 'running',
    summary: e.summary || e.error || '',
    args_preview: e.args_preview || '',
  }
}

// ── DCF Step Detail Renderers ─────────────────────────────────────────────────

function EvidenceDetail({ meta }: { meta: Record<string, unknown> }) {
  const tierSummary = meta.tier_summary as Record<string, number> | undefined
  const totalItems = (meta.total_items as number) || 0
  if (!tierSummary) return null
  const TIER_ORDER = ['filing', 'structured_api', 'document', 'news', 'generic_web']
  const TIER_COLORS: Record<string, string> = {
    filing: 'text-violet-400',
    structured_api: 'text-blue-400',
    document: 'text-emerald-400',
    news: 'text-amber-400',
    generic_web: 'text-zinc-500',
  }
  return (
    <div className="space-y-1">
      <div className="text-zinc-500">{totalItems} evidence items</div>
      {TIER_ORDER.filter(t => (tierSummary[t] ?? 0) > 0).map(t => (
        <div key={t} className="flex items-center gap-2">
          <span className={`w-16 ${TIER_COLORS[t] ?? 'text-zinc-400'}`}>{t.replace('_', ' ')}</span>
          <div className="flex-1 bg-zinc-900 rounded-full h-1 overflow-hidden">
            <div
              className="h-full bg-zinc-600 rounded-full"
              style={{ width: `${Math.round((tierSummary[t] / Math.max(totalItems, 1)) * 100)}%` }}
            />
          </div>
          <span className="text-zinc-500 w-4 text-right">{tierSummary[t]}</span>
        </div>
      ))}
    </div>
  )
}

function SynthesisDetail({ meta }: { meta: Record<string, unknown> }) {
  const growthOutlook = meta.growth_outlook as string | undefined
  const marginTrend = meta.margin_trend as string | undefined
  const keyRisks = meta.key_risks as string[] | undefined
  const confidence = meta.confidence as string | undefined
  const MARGIN_COLOR: Record<string, string> = {
    improving: 'text-emerald-400',
    stable: 'text-blue-400',
    declining: 'text-red-400',
    volatile: 'text-amber-400',
  }
  return (
    <div className="space-y-2">
      {confidence && (
        <div className="flex gap-2">
          <span className="text-zinc-600 w-20 flex-shrink-0">confidence</span>
          <span className="text-zinc-300">{confidence}</span>
        </div>
      )}
      {marginTrend && (
        <div className="flex gap-2">
          <span className="text-zinc-600 w-20 flex-shrink-0">margin</span>
          <span className={MARGIN_COLOR[marginTrend] ?? 'text-zinc-300'}>{marginTrend}</span>
        </div>
      )}
      {growthOutlook && (
        <div>
          <div className="text-zinc-600 mb-1">growth outlook</div>
          <p className="text-zinc-400 leading-relaxed">{growthOutlook}</p>
        </div>
      )}
      {keyRisks && keyRisks.length > 0 && (
        <div>
          <div className="text-zinc-600 mb-1">key risks</div>
          <ul className="space-y-1">
            {keyRisks.map((r, i) => (
              <li key={i} className="flex gap-1.5 text-zinc-400">
                <span className="text-zinc-700 flex-shrink-0">·</span>
                <span>{r}</span>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  )
}

function AssumptionsDetail({ meta }: { meta: Record<string, unknown> }) {
  type Proposal = { field: string; value: number; rationale?: string; confidence?: string }
  const assumptions = meta.assumptions as Record<string, number> | undefined
  const provenance = meta.assumption_provenance as Record<string, { source?: string; confidence?: string }> | undefined
  const waccComponents = meta.wacc_components as Record<string, unknown> | undefined
  const proposals = meta.memo_proposals as Proposal[] | undefined
  const flags = meta.assumption_flags as Array<{ field?: string; severity?: string; message?: string }> | undefined

  const pct = (v: number) => `${(v * 100).toFixed(1)}%`
  const dollar = (v: number) => v > 1e9 ? `$${(v / 1e9).toFixed(1)}B` : v > 1e6 ? `$${(v / 1e6).toFixed(0)}M` : `$${v.toFixed(0)}`

  const FIELD_LABELS: Record<string, string> = {
    base_revenue: 'Base revenue', revenue_growth: 'Rev. growth', fcff_margin: 'FCFF margin',
    wacc: 'WACC', terminal_growth: 'Terminal growth', net_debt: 'Net debt',
    shares_outstanding: 'Shares out.', tax_rate: 'Tax rate',
  }

  const formatVal = (field: string, v: number) => {
    if (field === 'base_revenue' || field === 'net_debt') return dollar(v)
    if (field === 'shares_outstanding') return `${(v / 1e6).toFixed(0)}M`
    return pct(v)
  }

  return (
    <div className="space-y-3">
      {assumptions && (
        <table className="w-full text-[10px]">
          <thead>
            <tr className="text-zinc-600">
              <th className="text-left font-normal pb-1">Field</th>
              <th className="text-right font-normal pb-1">Value</th>
              <th className="text-right font-normal pb-1">Source</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-zinc-900">
            {Object.entries(assumptions).map(([field, val]) => {
              const prov = provenance?.[field]
              return (
                <tr key={field}>
                  <td className="py-0.5 text-zinc-400">{FIELD_LABELS[field] ?? field}</td>
                  <td className="py-0.5 text-right text-zinc-300">{formatVal(field, val)}</td>
                  <td className="py-0.5 text-right text-zinc-600 max-w-[80px] truncate">{prov?.source ?? '—'}</td>
                </tr>
              )
            })}
          </tbody>
        </table>
      )}
      {waccComponents && (
        <div>
          <div className="text-zinc-600 mb-1">WACC decomposition</div>
          <div className="grid grid-cols-1 gap-y-0.5 text-[10px]">
            {Object.entries(waccComponents).filter(([, v]) => typeof v === 'number').map(([k, v]) => {
              const num = v as number
              const lower = k.toLowerCase()
              const isPct = lower === 'wacc' || lower.endsWith('_rate') || lower.endsWith('_margin') ||
                            lower.endsWith('_growth') || lower.endsWith('_weight') ||
                            lower === 're' || lower === 'rd' || lower === 'rf' || lower === 'erp' ||
                            lower === 'cost_of_equity' || lower === 'pre_tax_cost_of_debt' ||
                            lower === 'after_tax_cost_of_debt' || lower === 'wacc_pre_clip'
              const isBeta = lower === 'beta'
              const display = isBeta ? num.toFixed(2)
                            : isPct ? pct(num)
                            : Math.abs(num) >= 1e6 ? dollar(num)
                            : num.toFixed(2)
              const label = k.replace(/_/g, ' ')
              return (
                <div key={k} className="flex justify-between gap-2 leading-snug">
                  <span className="text-zinc-600 truncate min-w-0 flex-1">{label}</span>
                  <span className="text-zinc-300 tabular-nums flex-shrink-0">{display}</span>
                </div>
              )
            })}
          </div>
        </div>
      )}
      {proposals && proposals.length > 0 && (
        <div>
          <div className="text-zinc-600 mb-1">Rationale</div>
          <div className="space-y-2">
            {proposals.map((p, i) => (
              <div key={i} className="border-l border-zinc-800 pl-2">
                <div className="flex gap-2 items-baseline">
                  <span className="text-zinc-400 font-medium">{FIELD_LABELS[p.field] ?? p.field}</span>
                  <span className="text-zinc-300">{formatVal(p.field, p.value)}</span>
                  {p.confidence && <span className="text-zinc-600 text-[10px]">{p.confidence}</span>}
                </div>
                {p.rationale && <p className="text-zinc-500 leading-relaxed mt-0.5">{p.rationale}</p>}
              </div>
            ))}
          </div>
        </div>
      )}
      {flags && flags.length > 0 && (
        <div>
          <div className="text-zinc-600 mb-1">Flags</div>
          <div className="space-y-0.5">
            {flags.map((f, i) => (
              <div key={i} className={`text-[10px] ${f.severity === 'block' ? 'text-red-400' : 'text-amber-400'}`}>
                [{f.severity?.toUpperCase() ?? 'WARN'}] {f.field && <span className="font-medium">{f.field}: </span>}{f.message}
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}

function ProjectionsDetail({ meta }: { meta: Record<string, unknown> }) {
  type Row = { year: number; revenue_B: number; fcff_B: number }
  const projections = meta.projections as Row[] | undefined
  if (!projections || !projections.length) return null
  return (
    <table className="w-full text-[10px]">
      <thead>
        <tr className="text-zinc-600">
          <th className="text-left font-normal pb-1">Year</th>
          <th className="text-right font-normal pb-1">Revenue ($B)</th>
          <th className="text-right font-normal pb-1">FCFF ($B)</th>
        </tr>
      </thead>
      <tbody className="divide-y divide-zinc-900">
        {projections.map(row => (
          <tr key={row.year}>
            <td className="py-0.5 text-zinc-500">Y{row.year}</td>
            <td className="py-0.5 text-right text-zinc-300">{row.revenue_B.toFixed(2)}</td>
            <td className="py-0.5 text-right text-zinc-300">{row.fcff_B.toFixed(2)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

function ConfidenceBreakdownPanel({ breakdown }: { breakdown: ConfidenceBreakdown }) {
  const [open, setOpen] = useState(false)
  const LABEL_COLOR: Record<string, string> = { high: 'text-emerald-400', medium: 'text-amber-400', low: 'text-red-400' }
  const SCORE_COLOR = (s: number) => s >= 0.70 ? 'bg-emerald-600' : s >= 0.50 ? 'bg-amber-600' : 'bg-red-600'
  const COMP_LABELS: Record<string, string> = {
    data_quality: 'Data quality', revenue_growth: 'Revenue growth',
    margin_stability: 'Margin stability', wacc_reliability: 'WACC',
    terminal_assumptions: 'Terminal growth',
  }
  return (
    <div className="border border-[#1e1e1e] rounded">
      <button
        onClick={() => setOpen(o => !o)}
        className="w-full flex items-center gap-2 px-2 py-1 text-[10px] hover:bg-[#111] transition-colors"
      >
        <span className="text-zinc-600">confidence</span>
        <span className={`font-medium ${LABEL_COLOR[breakdown.label] ?? 'text-zinc-400'}`}>
          {breakdown.label.toUpperCase()}
        </span>
        <span className="text-zinc-700 tabular-nums">{Math.round(breakdown.aggregate_score * 100)}%</span>
        <span className="ml-auto text-zinc-700">
          <svg width="7" height="7" viewBox="0 0 8 8" fill="none" className={`transition-transform duration-150 ${open ? 'rotate-180' : ''}`}>
            <path d="M1 2.5L4 5.5L7 2.5" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round" />
          </svg>
        </span>
      </button>
      {open && (
        <div className="border-t border-[#1a1a1a] px-2 py-1.5 space-y-1.5 bg-[#070707]">
          {Object.entries(breakdown.components).map(([key, comp]) => (
            <div key={key} className="space-y-0.5">
              <div className="flex items-center gap-2">
                <span className="text-zinc-600 w-24 truncate">{COMP_LABELS[key] ?? key}</span>
                <div className="flex-1 bg-zinc-900 rounded-full h-1 overflow-hidden">
                  <div className={`h-full rounded-full ${SCORE_COLOR(comp.score)}`} style={{ width: `${Math.round(comp.score * 100)}%` }} />
                </div>
                <span className={`w-10 text-right tabular-nums ${LABEL_COLOR[comp.label] ?? 'text-zinc-400'}`}>
                  {Math.round(comp.score * 100)}%
                </span>
              </div>
              <p className="text-[9px] text-zinc-700 pl-26 leading-tight">{comp.reason}</p>
            </div>
          ))}
          {breakdown.summary && (
            <p className="text-[9px] text-zinc-600 leading-relaxed border-t border-[#1a1a1a] pt-1 mt-1">
              {breakdown.summary}
            </p>
          )}
        </div>
      )}
    </div>
  )
}

function ValuationDetail({ meta }: { meta: Record<string, unknown> }) {
  const v = meta.valuation as Record<string, number> | undefined
  const flags = meta.valuation_flags as Array<{ field?: string; severity?: string; message?: string }> | undefined
  const conf = meta.confidence_label as string | undefined
  const breakdown = meta.confidence_breakdown as ConfidenceBreakdown | undefined
  if (!v) return null
  const fmt = (n: number) => n > 1e9 ? `$${(n / 1e9).toFixed(1)}B` : n > 1e6 ? `$${(n / 1e6).toFixed(0)}M` : `$${n.toFixed(2)}`
  const rows = [
    { label: 'PV cash flows', value: v.pv_cash_flows },
    { label: 'PV terminal value', value: v.terminal_pv },
    { label: 'Enterprise value', value: v.enterprise_value, bold: true },
    { label: '– Net debt', value: v.enterprise_value - v.equity_value },
    { label: 'Equity value', value: v.equity_value, bold: true },
    { label: 'Implied price', value: v.implied_share_price, dollar: true },
    { label: 'Current price', value: v.current_price, dollar: true },
  ]
  return (
    <div className="space-y-2">
      {breakdown
        ? <ConfidenceBreakdownPanel breakdown={breakdown} />
        : conf && (
          <div className="flex items-center gap-2">
            <span className="text-zinc-600">confidence</span>
            <span className={conf === 'HIGH' ? 'text-emerald-400' : conf === 'LOW' ? 'text-amber-400' : 'text-zinc-400'}>{conf}</span>
          </div>
        )
      }
      <table className="w-full text-[10px]">
        <tbody className="divide-y divide-zinc-900">
          {rows.map(row => row.value != null && (
            <tr key={row.label}>
              <td className="py-0.5 text-zinc-500">{row.label}</td>
              <td className={`py-0.5 text-right ${row.bold ? 'text-zinc-200 font-medium' : 'text-zinc-300'}`}>
                {row.dollar ? `$${row.value.toFixed(2)}` : fmt(row.value)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {flags && flags.length > 0 && (
        <div className="space-y-0.5">
          {flags.map((f, i) => (
            <div key={i} className={`text-[10px] ${f.severity === 'block' ? 'text-red-400' : 'text-amber-400'}`}>
              [{f.severity?.toUpperCase() ?? 'WARN'}] {f.message}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

function SensitivityDetail({ meta }: { meta: Record<string, unknown> }) {
  type SRow = { wacc: number; terminal_growth: number; implied_share_price: number }
  const table = meta.sensitivity_table as SRow[] | undefined
  const waccBase = meta.wacc_base as number | undefined
  const tgrBase = meta.tgr_base as number | undefined
  if (!table || !table.length) return null

  const waccs = [...new Set(table.map(r => r.wacc))].sort((a, b) => a - b)
  const tgrs = [...new Set(table.map(r => r.terminal_growth))].sort((a, b) => a - b)
  const lookup = new Map(table.map(r => [`${r.wacc},${r.terminal_growth}`, r.implied_share_price]))

  const isBase = (w: number, t: number) =>
    waccBase != null && tgrBase != null &&
    Math.abs(w - waccBase) < 0.0001 && Math.abs(t - tgrBase) < 0.0001

  return (
    <div className="overflow-x-auto">
      <table className="w-full text-[10px] border-collapse">
        <thead>
          <tr>
            <th className="text-left font-normal text-zinc-600 pr-2 pb-1">WACC \ TGR</th>
            {tgrs.map(t => (
              <th key={t} className="text-right font-normal text-zinc-600 pb-1 px-1.5">
                {(t * 100).toFixed(1)}%
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="divide-y divide-zinc-900">
          {waccs.map(w => (
            <tr key={w}>
              <td className="py-0.5 pr-2 text-zinc-500">{(w * 100).toFixed(1)}%</td>
              {tgrs.map(t => {
                const price = lookup.get(`${w},${t}`)
                const base = isBase(w, t)
                return (
                  <td key={t} className={`py-0.5 px-1.5 text-right ${base ? 'text-zinc-100 font-semibold' : 'text-zinc-300'}`}>
                    {price != null ? `$${price.toFixed(0)}` : '—'}
                  </td>
                )
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function FinalizeDetail({ meta }: { meta: Record<string, unknown> }) {
  const conf = meta.confidence_label as string | undefined
  const implied = meta.implied_share_price as number | undefined
  if (!conf && implied == null) return null
  const CONF_COLOR: Record<string, string> = { HIGH: 'text-emerald-400', MEDIUM: 'text-zinc-400', LOW: 'text-amber-400' }
  return (
    <div className="flex gap-4 text-[11px]">
      {implied != null && (
        <div>
          <span className="text-zinc-600">implied price </span>
          <span className="text-zinc-200 font-medium">${implied.toFixed(2)}</span>
        </div>
      )}
      {conf && (
        <div>
          <span className="text-zinc-600">confidence </span>
          <span className={CONF_COLOR[conf] ?? 'text-zinc-400'}>{conf}</span>
        </div>
      )}
    </div>
  )
}

function MarketDataDetail({ meta }: { meta: Record<string, unknown> }) {
  const snapshot = meta.market_snapshot as { price?: number; source?: string } | undefined
  if (!snapshot?.price) return null
  return (
    <div className="flex gap-4 text-[11px]">
      <div><span className="text-zinc-600">spot </span><span className="text-zinc-200">${snapshot.price.toFixed(2)}</span></div>
      {snapshot.source && <div><span className="text-zinc-600">via </span><span className="text-zinc-400">{snapshot.source}</span></div>}
    </div>
  )
}

function ThesisDetail({ meta }: { meta: Record<string, unknown> }) {
  const bull = meta.bull_thesis as string | undefined
  const bear = meta.bear_thesis as string | undefined
  const narrative = meta.narrative as string | undefined
  const drivers = meta.key_drivers as Array<{ driver: string; direction: string; conviction: string }> | undefined
  if (!bull && !bear && !narrative && !drivers?.length) return null
  return (
    <div className="space-y-2 text-[11px]">
      {bull && (
        <div>
          <span className="text-emerald-500/80 font-medium">Bull case </span>
          <span className="text-zinc-400">{bull}</span>
        </div>
      )}
      {bear && (
        <div>
          <span className="text-red-400/80 font-medium">Bear case </span>
          <span className="text-zinc-400">{bear}</span>
        </div>
      )}
      {drivers && drivers.length > 0 && (
        <div>
          <span className="text-zinc-600">Key drivers </span>
          <div className="flex flex-wrap gap-1 mt-0.5">
            {drivers.map((d, i) => (
              <span key={i} className={`px-1.5 py-0.5 rounded text-[10px] ${
                d.direction === 'positive' ? 'bg-emerald-500/10 text-emerald-400' :
                d.direction === 'negative' ? 'bg-red-500/10 text-red-400' :
                'bg-zinc-500/10 text-zinc-400'
              }`}>
                {d.driver} ({d.conviction})
              </span>
            ))}
          </div>
        </div>
      )}
      {narrative && (
        <div>
          <span className="text-zinc-600">Narrative </span>
          <span className="text-zinc-400">{narrative}</span>
        </div>
      )}
    </div>
  )
}

function AnalysisDetail({ meta }: { meta: Record<string, unknown> }) {
  const severe = meta.severe_count as number | undefined
  const warnings = meta.warning_count as number | undefined
  const stop = meta.stop_reason as string | undefined
  const interpretation = meta.interpretation as string | undefined
  const flags = meta.flags as Array<{ signal: string; severity: string; value: unknown }> | undefined
  if (severe == null && warnings == null) return null
  return (
    <div className="space-y-1.5 text-[11px]">
      <div className="flex gap-3">
        <div>
          <span className={`font-medium ${severe ? 'text-red-400' : 'text-zinc-500'}`}>{severe ?? 0} severe</span>
        </div>
        <div>
          <span className={`font-medium ${warnings ? 'text-amber-400' : 'text-zinc-500'}`}>{warnings ?? 0} warnings</span>
        </div>
      </div>
      {stop && <div><span className="text-zinc-600">Stop: </span><span className="text-zinc-400">{stop}</span></div>}
      {interpretation && <div><span className="text-zinc-400">{interpretation}</span></div>}
      {flags && flags.length > 0 && (
        <div className="space-y-0.5">
          {flags.map((f, i) => (
            <div key={i} className="flex items-center gap-1.5">
              <span className={`w-1 h-1 rounded-full ${
                f.severity === 'severe' ? 'bg-red-500' :
                f.severity === 'warning' ? 'bg-amber-500' :
                'bg-zinc-600'
              }`} />
              <span className="text-zinc-500">{f.signal.replace(/_/g, ' ')}:</span>
              <span className="text-zinc-400">{String(f.value ?? '?')}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

function RefineDetail({ meta }: { meta: Record<string, unknown> }) {
  const summary = meta.summary_line as string | undefined
  if (!summary) return null
  return <div className="text-[11px] text-zinc-400">{summary}</div>
}

function ImpliedWaccDetail({ meta }: { meta: Record<string, unknown> }) {
  const capm = meta.capm_wacc as number | undefined
  const implied = meta.implied_wacc as number | undefined
  const gapBps = meta.gap_bps as number | undefined
  const flag = meta.flag as boolean | undefined
  const interpretation = meta.interpretation as string | undefined
  if (capm == null) return null
  const pct = (v: number) => `${(v * 100).toFixed(1)}%`
  return (
    <div className="space-y-1.5 text-[10px]">
      <div className="flex gap-4">
        <div><span className="text-zinc-600">CAPM </span><span className="text-zinc-300">{pct(capm)}</span></div>
        {implied != null && (
          <div><span className="text-zinc-600">market-implied </span><span className="text-zinc-300">{pct(implied)}</span></div>
        )}
        {gapBps != null && (
          <div className={flag ? 'text-amber-400' : 'text-zinc-500'}>
            {gapBps > 0 ? '+' : ''}{gapBps}bps {flag ? '⚠' : '✓'}
          </div>
        )}
      </div>
      {interpretation && <p className="text-zinc-600 leading-relaxed">{interpretation}</p>}
    </div>
  )
}

function DcfStepDetail({ stepName, meta }: { stepName: string; meta: Record<string, unknown> }) {
  switch (stepName) {
    case 'assemble_evidence': return <EvidenceDetail meta={meta} />
    case 'semantic_synthesis': return <SynthesisDetail meta={meta} />
    case 'propose_assumptions':
    case 'assumption_review': return <AssumptionsDetail meta={meta} />
    case 'project_cashflows': return <ProjectionsDetail meta={meta} />
    case 'compute_valuation': return <ValuationDetail meta={meta} />
    case 'compute_implied_wacc': return <ImpliedWaccDetail meta={meta} />
    case 'sensitivity': return <SensitivityDetail meta={meta} />
    case 'collect_market_data': return <MarketDataDetail meta={meta} />
    case 'finalize': return <FinalizeDetail meta={meta} />
    case 'formulate_thesis': return <ThesisDetail meta={meta} />
    case 'analyze_result': return <AnalysisDetail meta={meta} />
    case 'refine_assumptions': return <RefineDetail meta={meta} />
    default: return null
  }
}

// ── DcfSubstepRow ─────────────────────────────────────────────────────────────

function DcfSubstepRow({ entry }: { entry: ActivityEntry }) {
  const [open, setOpen] = useState(false)
  const display = getToolDisplay(entry.name)
  const stepName = entry.name.includes(':') ? entry.name.split(':').pop()! : entry.name
  const meta = entry.meta
  const hasDetail = entry.status === 'completed' && meta && Object.keys(meta).length > 1
  const cleaned = cleanToolSummary(entry.summary)

  return (
    <div className="text-[11px]">
      <button
        onClick={() => hasDetail && setOpen(o => !o)}
        disabled={!hasDetail}
        className="w-full flex items-center gap-2 text-left text-zinc-500 hover:text-zinc-300 disabled:hover:text-zinc-500 transition-colors"
      >
        <span className={`w-1 h-1 rounded-full flex-shrink-0 ${
          entry.status === 'completed' ? 'bg-emerald-500' :
          entry.status === 'error' ? 'bg-red-500' :
          entry.status === 'skipped' ? 'bg-zinc-700' :
          'bg-indigo-400 animate-pulse'
        }`} />
        <span className="font-medium text-zinc-300">{display.label}</span>
        {cleaned && <span className="text-zinc-600 truncate min-w-0 flex-1">· {cleaned}</span>}
        {hasDetail && (
          <span className="ml-auto text-zinc-700 flex-shrink-0">
            <svg width="7" height="7" viewBox="0 0 8 8" fill="none"
              className={`transition-transform duration-150 ${open ? 'rotate-180' : ''}`}>
              <path d="M1 2.5L4 5.5L7 2.5" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round" />
            </svg>
          </span>
        )}
      </button>

      {open && meta && (
        <div className="ml-3 mt-1.5 pl-2 border-l border-[#222] pb-1">
          <DcfStepDetail stepName={stepName} meta={meta as Record<string, unknown>} />
        </div>
      )}
    </div>
  )
}

// ── Evidence Panel ────────────────────────────────────────────────────────────

const TIER_BADGE: Record<string, { bg: string; text: string; label: string }> = {
  filing:         { bg: 'bg-violet-950/50', text: 'text-violet-400', label: 'filing' },
  structured_api: { bg: 'bg-blue-950/50',   text: 'text-blue-400',   label: 'api' },
  document:       { bg: 'bg-emerald-950/50',text: 'text-emerald-400',label: 'doc' },
  news:           { bg: 'bg-amber-950/50',  text: 'text-amber-400',  label: 'news' },
  generic_web:    { bg: 'bg-zinc-900',      text: 'text-zinc-500',   label: 'web' },
}

const TIER_ORDER = ['filing', 'structured_api', 'document', 'news', 'generic_web']

function EvidenceItemRow({ item }: { item: EvidenceItem }) {
  const [expanded, setExpanded] = useState(false)
  const badge = TIER_BADGE[item.source_tier] ?? { bg: 'bg-zinc-900', text: 'text-zinc-500', label: item.source_tier }
  const hasText = !!item.text
  const hasUrl = !!item.url
  const title = item.title || item.field || item.source || item.evidence_id

  return (
    <div className="text-[10px]">
      <div className="flex items-start gap-1.5 py-0.5">
        <span className={`flex-shrink-0 px-1 py-0.5 rounded text-[9px] font-medium ${badge.bg} ${badge.text}`}>
          {badge.label}
        </span>
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-1.5">
            {hasUrl ? (
              <a
                href={item.url}
                target="_blank"
                rel="noopener noreferrer"
                className="text-zinc-400 hover:text-zinc-200 truncate flex-1 underline underline-offset-2"
                title={title}
              >
                {title}
              </a>
            ) : (
              <span className="text-zinc-400 truncate flex-1" title={title}>{title}</span>
            )}
            {item.as_of && <span className="text-zinc-700 flex-shrink-0">{item.as_of.slice(0, 10)}</span>}
            {hasText && (
              <button onClick={() => setExpanded(o => !o)} className="text-zinc-700 hover:text-zinc-400 flex-shrink-0">
                <svg width="6" height="6" viewBox="0 0 8 8" fill="none" className={`transition-transform duration-100 ${expanded ? 'rotate-180' : ''}`}>
                  <path d="M1 2.5L4 5.5L7 2.5" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round" />
                </svg>
              </button>
            )}
          </div>
          {item.value != null && (
            <span className="text-zinc-600">{item.field}: {item.value}</span>
          )}
        </div>
      </div>
      {expanded && item.text && (
        <p className="ml-8 text-zinc-700 leading-relaxed border-l border-zinc-800 pl-2 pb-1 whitespace-pre-wrap break-words">
          {item.text}
        </p>
      )}
    </div>
  )
}

function EvidencePanel({ items }: { items: EvidenceItem[] }) {
  const [open, setOpen] = useState(false)
  if (!items.length) return null

  const tierCounts = items.reduce<Record<string, number>>((acc, item) => {
    acc[item.source_tier] = (acc[item.source_tier] ?? 0) + 1
    return acc
  }, {})

  const sorted = [...items].sort((a, b) => {
    const ai = TIER_ORDER.indexOf(a.source_tier)
    const bi = TIER_ORDER.indexOf(b.source_tier)
    return (ai === -1 ? 99 : ai) - (bi === -1 ? 99 : bi)
  })

  return (
    <div className="border-t border-[#141420]">
      <button
        onClick={() => setOpen(o => !o)}
        className="w-full flex items-center gap-2 px-3 py-1.5 text-[10px] hover:bg-[#0a0a12] transition-colors text-left"
      >
        <span className="text-zinc-600 font-medium">Sources</span>
        <span className="text-zinc-700">({items.length})</span>
        <div className="flex items-center gap-1 ml-1 flex-1 min-w-0 overflow-hidden">
          {TIER_ORDER.filter(t => (tierCounts[t] ?? 0) > 0).map(t => {
            const badge = TIER_BADGE[t]!
            return (
              <span key={t} className={`flex-shrink-0 px-1 py-0.5 rounded text-[9px] font-medium ${badge.bg} ${badge.text}`}>
                {badge.label} ×{tierCounts[t]}
              </span>
            )
          })}
        </div>
        <span className="text-zinc-700 flex-shrink-0">
          <svg width="7" height="7" viewBox="0 0 8 8" fill="none" className={`transition-transform duration-150 ${open ? 'rotate-180' : ''}`}>
            <path d="M1 2.5L4 5.5L7 2.5" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round" />
          </svg>
        </span>
      </button>
      {open && (
        <div className="px-3 pb-2 divide-y divide-[#0f0f18] max-h-56 overflow-y-auto">
          {sorted.map(item => <EvidenceItemRow key={item.evidence_id} item={item} />)}
        </div>
      )}
    </div>
  )
}

// ── Main ActivityTrace ────────────────────────────────────────────────────────

const DCF_HITL_FIELDS: Array<{ key: string; label: string }> = [
  { key: 'revenue_growth', label: 'Revenue Growth' },
  { key: 'fcff_margin', label: 'FCFF Margin' },
  { key: 'terminal_growth', label: 'Terminal Growth' },
  { key: 'tax_rate', label: 'Tax Rate' },
  { key: 'wacc', label: 'WACC' },
]

export function DcfHitlSection({
  review,
  onApprove,
  onReject,
  threadId,
}: {
  review: DcfReviewState
  onApprove?: (overrides?: Record<string, number>) => void
  onReject?: () => void
  threadId?: string
}) {
  const [edits, setEdits] = useState<Record<string, string>>({})
  const [expanded, setExpanded] = useState<Record<string, boolean>>({})
  const [confirmed, setConfirmed] = useState(false)

  const fmtPct = (v: number | undefined) => v == null ? '—' : `${(v * 100).toFixed(1)}%`
  const hasEdits = Object.values(edits).some(v => v !== '')

  const handleApprove = async () => {
    const overrides: Record<string, number> = {}
    for (const [k, v] of Object.entries(edits)) {
      if (v === '') continue
      const n = parseFloat(v) / 100
      if (!isNaN(n)) overrides[k] = n
    }
    setConfirmed(true)

    // Call /runs/{id}/dcf-decision endpoint if threadId available
    if (threadId) {
      try {
        await fetch(`/runs/${threadId}/dcf-decision`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            approved: true,
            assumptions_overrides: Object.keys(overrides).length ? overrides : {},
          }),
        })
      } catch (err) {
        console.error('DCF decision submission failed:', err)
      }
    }

    onApprove?.(Object.keys(overrides).length ? overrides : undefined)
  }

  const handleReject = async () => {
    if (threadId) {
      try {
        await fetch(`/runs/${threadId}/dcf-decision`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            approved: false,
            assumptions_overrides: {},
          }),
        })
      } catch (err) {
        console.error('DCF decision submission failed:', err)
      }
    }
    onReject?.()
  }

  if (confirmed) {
    return (
      <div className="mt-3 flex items-center gap-2 px-1 py-2 text-[11px] text-emerald-400">
        <span className="w-1.5 h-1.5 rounded-full bg-emerald-500 flex-shrink-0" />
        Assumptions approved — running valuation…
      </div>
    )
  }

  return (
    <div className="mt-3 rounded border border-[#1a1a2a] bg-[#07070f] overflow-hidden">
      <div className="flex items-center gap-2 px-3 py-2 border-b border-[#141420] text-[11px]">
        <span className="w-1.5 h-1.5 rounded-full bg-amber-400 animate-pulse flex-shrink-0" />
        <span className="text-zinc-300 font-medium">Review assumptions — {review.ticker}</span>
        <span className="ml-auto text-zinc-600">{review.horizon_years}yr horizon</span>
      </div>
      <div className="divide-y divide-[#0f0f18]">
        {DCF_HITL_FIELDS.map(f => {
          const val = review.assumptions[f.key]
          const prov = review.provenance[f.key] ?? {}
          const proposal = review.memo_proposals?.[f.key]
          const isOpen = !!expanded[f.key]
          const hasDetail = !!(proposal?.rationale || prov.source)
          const conf = proposal?.confidence ?? (prov.confidence as number | undefined)
          const confPct = conf != null ? Math.round(conf * 100) : null
          const confColor = confPct == null ? '' : confPct >= 80 ? 'text-emerald-400' : confPct >= 60 ? 'text-amber-400' : 'text-red-400'
          return (
            <div key={f.key}>
              <div className="grid grid-cols-[1fr_60px_70px_55px] gap-2 items-center px-3 py-1.5 text-[11px]">
                <button
                  onClick={() => hasDetail && setExpanded(prev => ({ ...prev, [f.key]: !prev[f.key] }))}
                  disabled={!hasDetail}
                  className="flex items-center gap-1 text-left disabled:cursor-default"
                >
                  {hasDetail && (
                    <svg width="6" height="6" viewBox="0 0 8 8" fill="none"
                      className={`text-zinc-700 flex-shrink-0 transition-transform duration-100 ${isOpen ? 'rotate-90' : ''}`}>
                      <path d="M2.5 1.5L5.5 4L2.5 6.5" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round" />
                    </svg>
                  )}
                  <span className="text-zinc-500">{f.label}</span>
                </button>
                <span className="text-zinc-200 font-medium tabular-nums">{fmtPct(val)}</span>
                <span className="text-zinc-700 truncate text-[10px]">{prov.source ?? '—'}</span>
                {confPct != null ? (
                  <span className={`${confColor} tabular-nums`}>{confPct}%</span>
                ) : <span />}
              </div>
              {isOpen && (
                <div className="px-3 pb-2 space-y-1.5 bg-[#050509]">
                  {proposal?.rationale && (
                    <p className="text-[10px] text-zinc-600 leading-relaxed border-l border-zinc-800 pl-2">
                      {proposal.rationale}
                    </p>
                  )}
                  <div className="flex items-center gap-1.5">
                    <span className="text-[10px] text-zinc-700 w-16 flex-shrink-0">Override %</span>
                    <input
                      type="number" step="0.1"
                      placeholder={fmtPct(val)}
                      value={edits[f.key] ?? ''}
                      onChange={e => setEdits(prev => ({ ...prev, [f.key]: e.target.value }))}
                      className="flex-1 px-2 py-0.5 rounded bg-[#0f0f18] border border-[#252535] text-zinc-300 text-[10px] placeholder-zinc-700 focus:outline-none"
                    />
                    {edits[f.key] && (
                      <button
                        onClick={() => setEdits(prev => { const n = { ...prev }; delete n[f.key]; return n })}
                        className="text-[10px] text-zinc-700 hover:text-zinc-400"
                      >×</button>
                    )}
                  </div>
                </div>
              )}
            </div>
          )
        })}
      </div>
      {(review.evidence_items?.length ?? 0) > 0 && (
        <EvidencePanel items={review.evidence_items!} />
      )}
      {hasEdits && (
        <div className="px-3 py-1.5 bg-indigo-950/20 border-t border-indigo-900/30">
          <p className="text-[10px] text-indigo-400">
            Overrides: {Object.entries(edits).filter(([, v]) => v !== '').map(([k, v]) => `${k}=${v}%`).join(', ')}
          </p>
        </div>
      )}
      <div className="flex items-center gap-2 px-3 py-2 border-t border-[#141420]">
        <button
          onClick={handleApprove}
          className="px-2.5 py-1 rounded bg-indigo-600 hover:bg-indigo-500 text-[11px] font-medium text-white transition-colors"
        >
          {hasEdits ? 'Apply edits & Run' : 'Approve & Run'}
        </button>
        <button
          onClick={handleReject}
          className="px-2.5 py-1 rounded border border-[#252535] text-[11px] text-zinc-500 hover:text-zinc-300 transition-colors"
        >
          Cancel
        </button>
        <span className="ml-auto text-[10px] text-zinc-700">expand field to override</span>
      </div>
    </div>
  )
}

export function ActivityTrace({
  toolCalls,
  activities,
  scope,
  stepId,
  defaultOpen,
  label = 'Activity',
  emptyHint,
  dcfReview,
  onDcfApprove,
  onDcfReject,
  threadId,
}: {
  toolCalls?: ToolCall[]
  activities?: ActivityEntry[]
  scope?: ActivityScope
  stepId?: string
  defaultOpen?: boolean
  label?: string
  emptyHint?: string
  dcfReview?: DcfReviewState
  onDcfApprove?: (overrides?: Record<string, number>) => void
  onDcfReject?: () => void
  threadId?: string
}) {
  const [open, setOpen] = useState<boolean>(defaultOpen ?? !!dcfReview)

  const grouped = useMemo<RowItem[] | null>(() => {
    if (!activities || !activities.length) return null
    const scoped = stepId
      ? activities.filter(e => e.step_id === stepId || e.scope === 'workflow')
      : activities
    return groupActivities(scoped, scope)
  }, [activities, scope, stepId])

  const flatRows = useMemo<ToolCall[]>(() => {
    if (grouped !== null) return []
    return Array.isArray(toolCalls) ? toolCalls : []
  }, [grouped, toolCalls])

  const isEmpty = grouped !== null ? grouped.length === 0 : flatRows.length === 0
  if (isEmpty) {
    if (!emptyHint) return null
    return <p className="text-[11px] text-zinc-700 italic px-3 py-1.5">{emptyHint}</p>
  }

  let running = 0, errors = 0, total = 0
  if (grouped !== null) {
    for (const item of grouped) {
      total++
      if (item.kind === 'group') {
        const s = item.parent.status
        if (s === 'started' || s === 'running') running++
        else if (s === 'error') errors++
      } else {
        const s = item.entry.status
        if (s === 'started' || s === 'running') running++
        else if (s === 'error') errors++
      }
    }
  } else {
    total = flatRows.length
    running = flatRows.filter(t => t.status === 'running').length
    errors = flatRows.filter(t => t.status === 'error').length
  }
  const done = total - running - errors

  let summaryText: string
  if (running > 0) {
    summaryText = `${done}/${total} done · ${running} running${errors ? ` · ${errors} error${errors === 1 ? '' : 's'}` : ''}`
  } else if (errors > 0) {
    summaryText = `${done}/${total} done · ${errors} error${errors === 1 ? '' : 's'}`
  } else {
    summaryText = `${total} step${total === 1 ? '' : 's'}`
  }

  return (
    <div className="rounded-lg border border-[#1c1c1c] bg-[#0c0c0c] overflow-hidden">
      <button
        onClick={() => setOpen(o => !o)}
        className="w-full flex items-center gap-2 px-3 py-1.5 text-left text-[11px] hover:bg-[#101010] transition-colors"
      >
        <span className={`w-1.5 h-1.5 rounded-full flex-shrink-0 ${
          running > 0 ? 'bg-indigo-400 animate-pulse' : errors > 0 ? 'bg-red-500' : 'bg-emerald-500'
        }`} />
        <span className="text-zinc-400 font-medium tracking-wide">{label}</span>
        <span className="text-zinc-700">·</span>
        <span className="text-zinc-600">{summaryText}</span>
        <span className="ml-auto text-zinc-700">
          <svg width="9" height="9" viewBox="0 0 8 8" fill="none"
            className={`transition-transform duration-150 ${open ? 'rotate-180' : ''}`}>
            <path d="M1 2.5L4 5.5L7 2.5" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round" />
          </svg>
        </span>
      </button>

      {open && (
        <div className="border-t border-[#161616] px-3 py-2 space-y-1.5">
          {grouped !== null
            ? grouped.map((item, i) =>
                item.kind === 'group'
                  ? <WorkflowGroupRow key={item.parent.activity_id || `g-${i}`} group={item} />
                  : <ActivityRow key={item.entry.activity_id || `f-${i}`} tc={entryToRow(item.entry)} />
              )
            : flatRows.map((tc, i) => (
                <ActivityRow key={`${tc.tool_name}-${i}`} tc={tc} />
              ))
          }
          {/* DcfHitlSection now rendered exclusively in ExecutionSidebar */}
        </div>
      )}
    </div>
  )
}

// ── WorkflowGroupRow ──────────────────────────────────────────────────────────

function WorkflowGroupRow({ group }: { group: WorkflowGroup }) {
  const { parent, children } = group
  const isRunning = parent.status === 'started' || parent.status === 'running'
  const isError = parent.status === 'error'
  const display = getToolDisplay(parent.name)
  const doneCount = children.filter(c => c.status === 'completed' || c.status === 'skipped').length
  const [open, setOpen] = useState(isRunning || parent.status === 'started')

  const isDcf = parent.name === 'workflow:dcf'

  const confidenceColor =
    parent.confidence_label === 'HIGH' ? 'bg-emerald-950/80 text-emerald-400' :
    parent.confidence_label === 'MEDIUM' ? 'bg-zinc-900 text-zinc-400' :
    parent.confidence_label === 'LOW' ? 'bg-amber-950/80 text-amber-400' : null

  return (
    <div className="rounded border border-[#1e1e28] bg-[#090910] overflow-hidden">
      <button
        onClick={() => setOpen(o => !o)}
        className="w-full flex items-center gap-2 px-2.5 py-1.5 text-left text-[11px] hover:bg-[#0d0d18] transition-colors"
      >
        <span className={`w-1 h-1 rounded-full flex-shrink-0 ${
          isRunning ? 'bg-indigo-400 animate-pulse' : isError ? 'bg-red-500' : 'bg-violet-500'
        }`} />
        <span className="font-medium text-violet-300">{display.label}</span>
        {children.length > 0 && (
          <span className="text-zinc-600">{doneCount}/{children.length}</span>
        )}
        {parent.summary && (
          <span className="text-zinc-600 truncate min-w-0 flex-1">· {parent.summary}</span>
        )}
        {confidenceColor && parent.confidence_label && (
          <span className={`flex-shrink-0 px-1.5 py-0.5 rounded text-[10px] font-medium ${confidenceColor}`}>
            {parent.confidence_label}
          </span>
        )}
        {parent.flag_count != null && parent.flag_count > 0 && (
          <span className="flex-shrink-0 px-1.5 py-0.5 rounded text-[10px] bg-amber-950/60 text-amber-400">
            {parent.flag_count} flag{parent.flag_count === 1 ? '' : 's'}
          </span>
        )}
        <span className="ml-auto text-zinc-700 flex-shrink-0">
          <svg width="7" height="7" viewBox="0 0 8 8" fill="none"
            className={`transition-transform duration-150 ${open ? 'rotate-180' : ''}`}>
            <path d="M1 2.5L4 5.5L7 2.5" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round" />
          </svg>
        </span>
      </button>

      {open && children.length > 0 && (
        <div className="border-t border-[#18181f] px-3 py-1.5 space-y-1 ml-1">
          {children.map((child, i) =>
            isDcf
              ? <DcfSubstepRow key={child.activity_id || `c-${i}`} entry={child} />
              : <ActivityRow key={child.activity_id || `c-${i}`} tc={entryToRow(child)} />
          )}
        </div>
      )}
    </div>
  )
}

// ── ActivityRow ───────────────────────────────────────────────────────────────

function ActivityRow({ tc }: { tc: ToolCall }) {
  const [open, setOpen] = useState(false)
  const display = getToolDisplay(tc.tool_name)
  const cleaned = cleanToolSummary(tc.summary)
  const expandable = tc.status !== 'running' && cleaned.length > 0

  return (
    <div className="text-[11px]">
      <button
        onClick={() => expandable && setOpen(o => !o)}
        disabled={!expandable}
        className="w-full flex items-center gap-2 text-left text-zinc-500 hover:text-zinc-300 disabled:hover:text-zinc-500 transition-colors"
      >
        <span className={`w-1 h-1 rounded-full flex-shrink-0 ${
          tc.status === 'done' ? 'bg-emerald-500' : tc.status === 'error' ? 'bg-red-500' : 'bg-indigo-400 animate-pulse'
        }`} />
        <span className={`font-medium ${display.group === 'workflow' ? 'text-violet-300' : 'text-zinc-300'}`}>
          {display.label}
        </span>
        {tc.args_preview && (
          <span className="text-zinc-600 truncate min-w-0">"{tc.args_preview}"</span>
        )}
        {expandable && (
          <span className="ml-auto text-zinc-700 flex-shrink-0">
            <svg width="7" height="7" viewBox="0 0 8 8" fill="none"
              className={`transition-transform duration-150 ${open ? 'rotate-180' : ''}`}>
              <path d="M1 2.5L4 5.5L7 2.5" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round" />
            </svg>
          </span>
        )}
      </button>

      {open && cleaned && (
        <p className="ml-3 mt-1 pl-2 border-l border-[#222] text-zinc-600 leading-relaxed">{cleaned}</p>
      )}
    </div>
  )
}

// ── ResearchStepsTrace ────────────────────────────────────────────────────────

export function ResearchStepsTrace({ steps, defaultOpen }: { steps: StepState[]; defaultOpen?: boolean }) {
  const [open, setOpen] = useState<boolean>(defaultOpen ?? false)
  const safe = Array.isArray(steps) ? steps : []
  if (!safe.length) return null

  const completed = safe.filter(s => s.status === 'completed').length
  const failed = safe.filter(s => s.status === 'failed').length
  const total = safe.length

  return (
    <div className="rounded-lg border border-[#1c1c1c] bg-[#0c0c0c] overflow-hidden">
      <button
        onClick={() => setOpen(o => !o)}
        className="w-full flex items-center gap-2 px-3 py-1.5 text-left text-[11px] hover:bg-[#101010] transition-colors"
      >
        <span className={`w-1.5 h-1.5 rounded-full flex-shrink-0 ${failed > 0 ? 'bg-red-500' : 'bg-emerald-500'}`} />
        <span className="text-zinc-400 font-medium tracking-wide">Research plan</span>
        <span className="text-zinc-700">·</span>
        <span className="text-zinc-600">
          {completed}/{total} step{total === 1 ? '' : 's'}{failed ? ` · ${failed} failed` : ''}
        </span>
        <span className="ml-auto text-zinc-700">
          <svg width="9" height="9" viewBox="0 0 8 8" fill="none"
            className={`transition-transform duration-150 ${open ? 'rotate-180' : ''}`}>
            <path d="M1 2.5L4 5.5L7 2.5" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round" />
          </svg>
        </span>
      </button>

      {open && (
        <div className="border-t border-[#161616] px-3 py-2.5 space-y-2">
          {safe.map((step, idx) => <PersistedStepRow key={step.id || idx} step={step} index={idx} />)}
        </div>
      )}
    </div>
  )
}

function PersistedStepRow({ step, index }: { step: StepState; index: number }) {
  const isComplete = step.status === 'completed'
  const isFailed = step.status === 'failed'
  const toolCalls = Array.isArray(step.tool_calls) ? step.tool_calls : []
  return (
    <div className="text-[11px]">
      <div className="flex items-start gap-2">
        <span className={`mt-1 w-1.5 h-1.5 rounded-full flex-shrink-0 ${
          isFailed ? 'bg-red-500' : isComplete ? 'bg-emerald-500' : 'bg-zinc-700'
        }`} />
        <span className="font-medium text-zinc-500 tabular-nums w-5 flex-shrink-0">
          {String(index + 1).padStart(2, '0')}
        </span>
        <span className="text-zinc-300 leading-relaxed">{step.description || 'Research step'}</span>
      </div>
      {toolCalls.length > 0 && (
        <div className="ml-7 mt-1 space-y-1">
          {toolCalls.map((tc, i) => <ActivityRow key={`${step.id}-${i}`} tc={tc} />)}
        </div>
      )}
    </div>
  )
}
