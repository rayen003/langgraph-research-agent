import { useState, useMemo, useEffect, useRef } from 'react'
import type { ConfidenceBreakdown, DcfReviewState, EvidenceItem, StepState, ToolCall } from '../types'
import type { ActivityEntry, ActivityScope } from '../lib/activity'
import { activityStatusToToolStatus } from '../lib/activity'
import { cleanToolSummary, getToolDisplay, summarizeToolActions } from '../lib/toolLabels'

// Re-export for backward compat
export { activityStatusToToolStatus }

// ── Settle hook ───────────────────────────────────────────────────────────────
// Fires a brief CSS animation when `value` transitions to `target`.
function useSettleOn(value: string, target: string): boolean {
  const [settling, setSettling] = useState(false)
  const prev = useRef(value)
  useEffect(() => {
    if (prev.current !== target && value === target) {
      setSettling(true)
      const t = setTimeout(() => setSettling(false), 450)
      prev.current = value
      return () => clearTimeout(t)
    }
    prev.current = value
  }, [value, target])
  return settling
}

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
// ChildEntry: a workflow_step that may itself contain nested sub-steps
type ChildEntry = ActivityEntry & { subChildren?: ActivityEntry[] }
type WorkflowGroup = { kind: 'group'; parent: ActivityEntry; children: ChildEntry[] }
type RowItem = FlatRow | WorkflowGroup

function groupActivities(entries: ActivityEntry[], scope?: ActivityScope): RowItem[] {
  const safe = Array.isArray(entries) ? entries : []
  const filtered = scope ? safe.filter(e => !scope || e.scope === scope || e.scope === 'workflow') : safe

  // Pass 1 — build childrenByParent for ALL parent_activity_ids (two levels)
  const childrenByParent = new Map<string, ActivityEntry[]>()
  for (const e of filtered) {
    if (e.kind === 'workflow_step' && e.parent_activity_id) {
      const list = childrenByParent.get(e.parent_activity_id) ?? []
      list.push(e)
      childrenByParent.set(e.parent_activity_id, list)
    }
  }

  // Pass 2 — all workflow_steps are excluded from top-level (childIds).
  // Subgroup entries (those with their own sub-children) are marked for
  // sub-step attachment rather than removed from children lists.
  const childIds = new Set<string>()
  for (const e of filtered) {
    if (e.kind === 'workflow_step' && e.parent_activity_id) {
      childIds.add(e.activity_id)
    }
  }

  // For every workflow group present, suppress the flat tool row that spawned it
  // (e.g. workflow:dcf group → hide run_dcf_workflow row; workflow:deck → hide
  // run_deck_workflow).  Convention: tool name is `run_<workflow>_workflow`.
  const suppressedToolNames = new Set<string>()
  for (const e of filtered) {
    if (e.kind === 'workflow' && e.name.startsWith('workflow:')) {
      const wfId = e.name.split(':')[1]
      if (wfId) suppressedToolNames.add(`run_${wfId}_workflow`)
    }
  }

  const result: RowItem[] = []
  for (const e of filtered) {
    if (childIds.has(e.activity_id)) continue
    if (e.kind === 'tool' && suppressedToolNames.has(e.name)) continue
    if (e.kind === 'workflow' || childrenByParent.has(e.activity_id)) {
      // Attach subChildren to any child that is itself a subgroup container
      const rawChildren = childrenByParent.get(e.activity_id) ?? []
      const children: ChildEntry[] = rawChildren.map(child => {
        const subs = childrenByParent.get(child.activity_id)
        return subs ? { ...child, subChildren: subs } : child
      })
      result.push({ kind: 'group', parent: e, children })
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
  const items = meta.items as Array<{
    evidence_id: string; kind: string; source_tier: string; source: string
    title?: string; url?: string; text?: string; as_of?: string
    filing_type?: string; section?: string
  }> | undefined
  const [expandedId, setExpandedId] = useState<string | null>(null)

  if (!tierSummary) return null
  const TIER_ORDER = ['filing', 'structured_api', 'document', 'news', 'generic_web']
  const TIER_COLORS: Record<string, string> = {
    filing: 'text-violet-400',
    structured_api: 'text-blue-400',
    document: 'text-emerald-400',
    news: 'text-amber-400',
    generic_web: 'text-ink-dim',
  }
  const TIER_BG: Record<string, string> = {
    filing: 'bg-violet-500/20',
    structured_api: 'bg-blue-500/20',
    document: 'bg-emerald-500/20',
    news: 'bg-amber-500/20',
    generic_web: 'bg-zinc-500/20',
  }
  const kindLabel = (k: string) => {
    const map: Record<string, string> = {
      filing_excerpt: 'filing', web_excerpt: 'web', document_excerpt: 'doc',
      structured_fundamental: 'api', market_data: 'mkt', profile: 'profile',
    }
    return map[k] ?? k.slice(0, 6)
  }
  return (
    <div className="space-y-1.5 text-[11px]">
      <div className="text-ink-dim mb-1">{totalItems} evidence items</div>
      {items && items.length > 0 && (
        <details className="mb-1">
          <summary className="text-ink-dim hover:text-ink-muted cursor-pointer">Inspect {items.length} sources</summary>
          <div className="mt-1 max-h-64 overflow-y-auto space-y-1">
            {items.map((item, i) => {
              const isOpen = expandedId === item.evidence_id
              const tier = item.source_tier
              const hasContent = !!(item.text?.trim() || item.url)
              const label = item.title || item.section || item.kind || '—'
              return (
                <div key={i} className="border-l-2 border-zinc-800 pl-2">
                  <button
                    onClick={() => hasContent && setExpandedId(isOpen ? null : item.evidence_id)}
                    disabled={!hasContent}
                    className="w-full text-left flex items-start gap-1.5 disabled:cursor-default"
                  >
                    <span className={`px-1 py-px rounded text-[9px] font-medium uppercase ${TIER_BG[tier] ?? 'bg-zinc-800'} ${TIER_COLORS[tier] ?? 'text-ink-muted'}`}>
                      {kindLabel(item.kind)}
                    </span>
                    <span className="text-ink-dim truncate flex-1">{label}</span>
                    {item.as_of && <span className="text-zinc-700 text-[10px]">{item.as_of}</span>}
                  </button>
                  {isOpen && (
                    <div className="mt-1 text-ink-dim space-y-1 pb-1">
                      {item.kind === 'structured_fundamental' || item.kind === 'market_data' ? (
                        <div>
                          <span className="text-ink-dim">{item.title}: </span>
                          <span className="text-ink-muted">{item.text}</span>
                        </div>
                      ) : item.text ? (
                        <div className="leading-relaxed">{item.text}</div>
                      ) : null}
                      {item.url && (
                        <a href={item.url} target="_blank" rel="noopener" className="text-indigo-400 hover:underline break-all block">
                          {item.url.length > 80 ? item.url.slice(0, 80) + '…' : item.url}
                        </a>
                      )}
                    </div>
                  )}
                </div>
              )
            })}
          </div>
        </details>
      )}
      {TIER_ORDER.filter(t => (tierSummary[t] ?? 0) > 0).map(t => (
        <div key={t} className="flex items-center gap-2">
          <span className={`w-16 ${TIER_COLORS[t] ?? 'text-ink-muted'}`}>{t.replace('_', ' ')}</span>
          <div className="flex-1 bg-zinc-900 rounded-full h-1 overflow-hidden">
            <div
              className="h-full bg-zinc-600 rounded-full"
              style={{ width: `${Math.round((tierSummary[t] / Math.max(totalItems, 1)) * 100)}%` }}
            />
          </div>
          <span className="text-ink-dim w-4 text-right">{tierSummary[t]}</span>
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
  // Lifecycle signals (drive memo's choice of optional DCF mechanics)
  const lifecycleStage = meta.lifecycle_stage as string | undefined
  const marginTrajectory = meta.margin_trajectory as string | undefined
  const sbcIntensity = meta.sbc_intensity as string | undefined
  const capitalReturnPolicy = meta.capital_return_policy as string | undefined

  const MARGIN_COLOR: Record<string, string> = {
    improving: 'text-emerald-400',
    stable: 'text-blue-400',
    declining: 'text-red-400',
    volatile: 'text-amber-400',
  }
  const LIFECYCLE_COLOR: Record<string, string> = {
    hypergrowth: 'text-violet-400',
    scaling: 'text-emerald-400',
    mature: 'text-blue-400',
    declining: 'text-red-400',
    cyclical: 'text-amber-400',
  }
  const TRAJECTORY_COLOR: Record<string, string> = {
    expanding: 'text-emerald-400',
    stable: 'text-blue-400',
    compressing: 'text-red-400',
  }
  const SBC_COLOR: Record<string, string> = {
    high: 'text-red-400',
    moderate: 'text-amber-400',
    low: 'text-emerald-400',
  }

  return (
    <div className="space-y-2">
      {/* Lifecycle signal pills — render as compact chip row when present */}
      {(lifecycleStage || marginTrajectory || sbcIntensity) && (
        <div className="flex flex-wrap gap-1.5 pb-1">
          {lifecycleStage && (
            <span className={`text-[10px] px-1.5 py-0.5 rounded border border-zinc-800 ${LIFECYCLE_COLOR[lifecycleStage] ?? 'text-ink-muted'}`}>
              {lifecycleStage}
            </span>
          )}
          {marginTrajectory && (
            <span className={`text-[10px] px-1.5 py-0.5 rounded border border-zinc-800 ${TRAJECTORY_COLOR[marginTrajectory] ?? 'text-ink-muted'}`}>
              margins {marginTrajectory}
            </span>
          )}
          {sbcIntensity && (
            <span className={`text-[10px] px-1.5 py-0.5 rounded border border-zinc-800 ${SBC_COLOR[sbcIntensity] ?? 'text-ink-muted'}`}>
              SBC {sbcIntensity}
            </span>
          )}
        </div>
      )}
      {confidence && (
        <div className="flex gap-2">
          <span className="text-ink-dim w-20 flex-shrink-0">confidence</span>
          <span className="text-ink-muted">{confidence}</span>
        </div>
      )}
      {marginTrend && (
        <div className="flex gap-2">
          <span className="text-ink-dim w-20 flex-shrink-0">margin</span>
          <span className={MARGIN_COLOR[marginTrend] ?? 'text-ink-muted'}>{marginTrend}</span>
        </div>
      )}
      {capitalReturnPolicy && (
        <div>
          <div className="text-ink-dim mb-1">capital return</div>
          <p className="text-ink-muted leading-relaxed">{capitalReturnPolicy}</p>
        </div>
      )}
      {growthOutlook && (
        <div>
          <div className="text-ink-dim mb-1">growth outlook</div>
          <p className="text-ink-muted leading-relaxed">{growthOutlook}</p>
        </div>
      )}
      {keyRisks && keyRisks.length > 0 && (
        <div>
          <div className="text-ink-dim mb-1">key risks</div>
          <ul className="space-y-1">
            {keyRisks.map((r, i) => (
              <li key={i} className="flex gap-1.5 text-ink-muted">
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
    base_revenue: 'Base revenue', revenue_growth: 'Rev. growth (Y1)', fcff_margin: 'FCFF margin (Y1)',
    wacc: 'WACC', terminal_growth: 'Perpetuity growth', net_debt: 'Net debt',
    shares_outstanding: 'Shares out.', tax_rate: 'Tax rate',
    // 4 real-world mechanics
    revenue_growth_terminal: 'Rev. growth (Y5)',
    fcff_margin_terminal: 'FCFF margin (Y5)',
    buyback_yield: 'Buyback yield',
    sbc_pct_revenue: 'SBC % rev',
  }

  const formatVal = (field: string, v: number) => {
    if (field === 'base_revenue' || field === 'net_debt') return dollar(v)
    if (field === 'shares_outstanding') return `${v.toFixed(0)}M`  // value already in millions
    return pct(v)
  }

  return (
    <div className="space-y-3">
      {assumptions && (
        <table className="w-full text-[10px]">
          <thead>
            <tr className="text-ink-dim">
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
                  <td className="py-0.5 text-ink-muted">{FIELD_LABELS[field] ?? field}</td>
                  <td className="py-0.5 text-right text-ink-muted">{formatVal(field, val)}</td>
                  <td className="py-0.5 text-right text-ink-dim max-w-[80px] truncate">{prov?.source ?? '—'}</td>
                </tr>
              )
            })}
          </tbody>
        </table>
      )}
      {waccComponents && (
        <div>
          <div className="text-ink-dim mb-1">WACC decomposition</div>
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
                  <span className="text-ink-dim truncate min-w-0 flex-1">{label}</span>
                  <span className="text-ink-muted tabular-nums flex-shrink-0">{display}</span>
                </div>
              )
            })}
          </div>
        </div>
      )}
      {proposals && proposals.length > 0 && (
        <div>
          <div className="text-ink-dim mb-1">Rationale</div>
          <div className="space-y-2">
            {proposals.map((p, i) => (
              <div key={i} className="border-l border-zinc-800 pl-2">
                <div className="flex gap-2 items-baseline">
                  <span className="text-ink-muted font-medium">{FIELD_LABELS[p.field] ?? p.field}</span>
                  <span className="text-ink-muted">{formatVal(p.field, p.value)}</span>
                  {p.confidence && <span className="text-ink-dim text-[10px]">{p.confidence}</span>}
                </div>
                {p.rationale && <p className="text-ink-dim leading-relaxed mt-0.5">{p.rationale}</p>}
              </div>
            ))}
          </div>
        </div>
      )}
      {flags && flags.length > 0 && (
        <div>
          <div className="text-ink-dim mb-1">Flags</div>
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
        <tr className="text-ink-dim">
          <th className="text-left font-normal pb-1">Year</th>
          <th className="text-right font-normal pb-1">Revenue ($B)</th>
          <th className="text-right font-normal pb-1">FCFF ($B)</th>
        </tr>
      </thead>
      <tbody className="divide-y divide-zinc-900">
        {projections.map(row => (
          <tr key={row.year}>
            <td className="py-0.5 text-ink-dim">Y{row.year}</td>
            <td className="py-0.5 text-right text-ink-muted">{row.revenue_B.toFixed(2)}</td>
            <td className="py-0.5 text-right text-ink-muted">{row.fcff_B.toFixed(2)}</td>
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
    <div className="border border-border rounded">
      <button
        onClick={() => setOpen(o => !o)}
        className="w-full flex items-center gap-2 px-2 py-1 text-[10px] hover:bg-bg-overlay transition-colors"
      >
        <span className="text-ink-dim">confidence</span>
        <span className={`font-medium ${LABEL_COLOR[breakdown.label] ?? 'text-ink-muted'}`}>
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
        <div className="border-t border-border px-2 py-1.5 space-y-1.5 bg-bg">
          {Object.entries(breakdown.components).map(([key, comp]) => (
            <div key={key} className="space-y-0.5">
              <div className="flex items-center gap-2">
                <span className="text-ink-dim w-24 truncate">{COMP_LABELS[key] ?? key}</span>
                <div className="flex-1 bg-zinc-900 rounded-full h-1 overflow-hidden">
                  <div className={`h-full rounded-full ${SCORE_COLOR(comp.score)}`} style={{ width: `${Math.round(comp.score * 100)}%` }} />
                </div>
                <span className={`w-10 text-right tabular-nums ${LABEL_COLOR[comp.label] ?? 'text-ink-muted'}`}>
                  {Math.round(comp.score * 100)}%
                </span>
              </div>
              <p className="text-[9px] text-zinc-700 pl-26 leading-tight">{comp.reason}</p>
            </div>
          ))}
          {breakdown.summary && (
            <p className="text-[9px] text-ink-dim leading-relaxed border-t border-border pt-1 mt-1">
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
            <span className="text-ink-dim">confidence</span>
            <span className={conf === 'HIGH' ? 'text-emerald-400' : conf === 'LOW' ? 'text-amber-400' : 'text-ink-muted'}>{conf}</span>
          </div>
        )
      }
      <table className="w-full text-[10px]">
        <tbody className="divide-y divide-zinc-900">
          {rows.map(row => row.value != null && (
            <tr key={row.label}>
              <td className="py-0.5 text-ink-dim">{row.label}</td>
              <td className={`py-0.5 text-right ${row.bold ? 'text-ink font-medium' : 'text-ink-muted'}`}>
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
            <th className="text-left font-normal text-ink-dim pr-2 pb-1">WACC \ TGR</th>
            {tgrs.map(t => (
              <th key={t} className="text-right font-normal text-ink-dim pb-1 px-1.5">
                {(t * 100).toFixed(1)}%
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="divide-y divide-zinc-900">
          {waccs.map(w => (
            <tr key={w}>
              <td className="py-0.5 pr-2 text-ink-dim">{(w * 100).toFixed(1)}%</td>
              {tgrs.map(t => {
                const price = lookup.get(`${w},${t}`)
                const base = isBase(w, t)
                return (
                  <td key={t} className={`py-0.5 px-1.5 text-right ${base ? 'text-ink font-semibold' : 'text-ink-muted'}`}>
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
  const CONF_COLOR: Record<string, string> = { HIGH: 'text-emerald-400', MEDIUM: 'text-ink-muted', LOW: 'text-amber-400' }
  return (
    <div className="flex gap-4 text-[11px]">
      {implied != null && (
        <div>
          <span className="text-ink-dim">implied price </span>
          <span className="text-ink font-medium">${implied.toFixed(2)}</span>
        </div>
      )}
      {conf && (
        <div>
          <span className="text-ink-dim">confidence </span>
          <span className={CONF_COLOR[conf] ?? 'text-ink-muted'}>{conf}</span>
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
      <div><span className="text-ink-dim">spot </span><span className="text-ink">${snapshot.price.toFixed(2)}</span></div>
      {snapshot.source && <div><span className="text-ink-dim">via </span><span className="text-ink-muted">{snapshot.source}</span></div>}
    </div>
  )
}

function ThesisDetail({ meta }: { meta: Record<string, unknown> }) {
  const bull = meta.bull_thesis as string | undefined
  const bear = meta.bear_thesis as string | undefined
  const narrative = meta.narrative as string | undefined
  const drivers = meta.key_drivers as Array<{ driver: string; direction: string; conviction: string }> | undefined
  const isFallback = meta.thesis_quality === 'fallback'
  if (!bull && !bear && !narrative && !drivers?.length && !isFallback) return null
  return (
    <div className="space-y-2 text-[11px]">
      {isFallback && (
        <div className="px-2 py-1.5 rounded bg-amber-500/8 border border-amber-500/20 text-amber-400/90 text-[10px] leading-snug">
          <span className="font-semibold">⚠ Thesis could not be grounded in evidence.</span>
          {' '}Assumptions may not reflect company-specific dynamics. Review subgraph will flag this as high-severity.
        </div>
      )}
      {bull && (
        <div>
          <span className="text-emerald-500/80 font-medium">Bull case </span>
          <span className="text-ink-muted">{bull}</span>
        </div>
      )}
      {bear && (
        <div>
          <span className="text-red-400/80 font-medium">Bear case </span>
          <span className="text-ink-muted">{bear}</span>
        </div>
      )}
      {drivers && drivers.length > 0 && (
        <div>
          <span className="text-ink-dim">Key drivers </span>
          <div className="flex flex-wrap gap-1 mt-0.5">
            {drivers.map((d, i) => (
              <span key={i} className={`px-1.5 py-0.5 rounded text-[10px] ${
                d.direction === 'positive' ? 'bg-emerald-500/10 text-emerald-400' :
                d.direction === 'negative' ? 'bg-red-500/10 text-red-400' :
                'bg-zinc-500/10 text-ink-muted'
              }`}>
                {d.driver} ({d.conviction})
              </span>
            ))}
          </div>
        </div>
      )}
      {narrative && (
        <div>
          <span className="text-ink-dim">Narrative </span>
          <span className="text-ink-muted">{narrative}</span>
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
          <span className={`font-medium ${severe ? 'text-red-400' : 'text-ink-dim'}`}>{severe ?? 0} severe</span>
        </div>
        <div>
          <span className={`font-medium ${warnings ? 'text-amber-400' : 'text-ink-dim'}`}>{warnings ?? 0} warnings</span>
        </div>
      </div>
      {stop && <div><span className="text-ink-dim">Stop: </span><span className="text-ink-muted">{stop}</span></div>}
      {interpretation && <div><span className="text-ink-muted">{interpretation}</span></div>}
      {flags && flags.length > 0 && (
        <div className="space-y-0.5">
          {flags.map((f, i) => (
            <div key={i} className="flex items-center gap-1.5">
              <span className={`w-1 h-1 rounded-full ${
                f.severity === 'severe' ? 'bg-red-500' :
                f.severity === 'warning' ? 'bg-amber-500' :
                'bg-zinc-600'
              }`} />
              <span className="text-ink-dim">{f.signal.replace(/_/g, ' ')}:</span>
              <span className="text-ink-muted">{String(f.value ?? '?')}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

function RefineDetail({ meta }: { meta: Record<string, unknown> }) {
  const changes = meta.changes as string[] | undefined
  const interpretation = meta.interpretation as string | undefined
  const adjustments = meta.adjustments_applied as Record<string, number> | undefined
  const hadChanges = meta.had_changes as boolean | undefined

  return (
    <div className="space-y-1.5 text-[11px]">
      {interpretation && <div className="text-ink-muted">{interpretation}</div>}
      {adjustments && Object.keys(adjustments).length > 0 ? (
        <div className="space-y-0.5">
          {Object.entries(adjustments).map(([field, delta]) => (
            <div key={field} className="flex items-center gap-1.5">
              <span className="text-ink-dim">{field}:</span>
              <span className={delta >= 0 ? 'text-emerald-400' : 'text-red-400'}>
                {delta >= 0 ? '+' : ''}{delta.toFixed(4)}
              </span>
            </div>
          ))}
        </div>
      ) : hadChanges === false ? (
        <div className="text-ink-dim">No adjustments — critique produced no suggestions</div>
      ) : null}
      {changes && changes.length > 0 && (
        <div className="text-ink-dim text-[10px]">
          {changes.map((c, i) => <div key={i}>{c}</div>)}
        </div>
      )}
    </div>
  )
}

function ScenarioRunnerDetail({ meta }: { meta: Record<string, unknown> }) {
  const results = meta.scenario_results as Array<{
    name: string; probability: number;
    valuation: { implied_share_price?: number }
  }> | undefined
  if (!results?.length) {
    const exp = meta.expected_value as number | undefined
    const lo = meta.range_low as number | undefined
    const hi = meta.range_high as number | undefined
    if (exp == null) return null
    return (
      <div className="flex gap-4 text-[11px]">
        <div><span className="text-ink-dim">expected </span><span className="text-ink font-medium">${exp.toFixed(2)}</span></div>
        {lo != null && hi != null && <div><span className="text-ink-dim">range </span><span className="text-ink-muted">${lo.toFixed(2)}–${hi.toFixed(2)}</span></div>}
      </div>
    )
  }
  return (
    <div className="space-y-1.5 text-[11px]">
      <div className="grid grid-cols-3 gap-2 text-ink-dim">
        <span>Scenario</span><span>Prob</span><span>Price</span>
      </div>
      {results.map((r, i) => {
        const price = r.valuation?.implied_share_price
        const color = r.name === 'bull' ? 'text-emerald-400' : r.name === 'bear' ? 'text-red-400' : 'text-ink-muted'
        return (
          <div key={i} className="grid grid-cols-3 gap-2">
            <span className={color}>{r.name}</span>
            <span className="text-ink-dim">{(r.probability * 100).toFixed(0)}%</span>
            <span className="text-ink-muted">{price != null ? `$${price.toFixed(2)}` : '—'}</span>
          </div>
        )
      })}
    </div>
  )
}

function ScenarioGeneratorDetail({ meta }: { meta: Record<string, unknown> }) {
  const scenarios = meta.scenarios as Array<{
    name: string; probability: number; assumptions: Record<string, number>; rationale: string
  }> | undefined
  if (!scenarios?.length) return null
  return (
    <div className="space-y-2 text-[11px]">
      {scenarios.map((s, i) => {
        const color = s.name === 'bull' ? 'text-emerald-400' : s.name === 'bear' ? 'text-red-400' : 'text-ink-muted'
        const a = s.assumptions
        return (
          <div key={i}>
            <span className={color + ' font-medium'}>{s.name}</span>
            <span className="text-ink-dim"> ({(s.probability * 100).toFixed(0)}%)</span>
            <div className="text-ink-dim mt-0.5">
              growth={((a.revenue_growth ?? 0) * 100).toFixed(1)}% margin={((a.fcff_margin ?? 0) * 100).toFixed(1)}% TGR={((a.terminal_growth ?? 0) * 100).toFixed(1)}%
            </div>
            {s.rationale && <div className="text-ink-dim">{s.rationale}</div>}
          </div>
        )
      })}
    </div>
  )
}

function MarketSignalsDetail({ meta }: { meta: Record<string, unknown> }) {
  const ws = meta.wacc_sanity as Record<string, unknown> | undefined
  const ig = meta.implied_growth as number | undefined | null
  const im = meta.implied_margin as number | undefined | null
  if (!ws && ig == null && im == null) return null
  const pct = (v: number) => `${(v * 100).toFixed(1)}%`
  return (
    <div className="space-y-1 text-[10px]">
      {ws && (
        <div className="flex gap-3">
          <span className="text-ink-dim">WACC</span>
          <span className="text-ink-muted">{pct(ws.capm_wacc as number)}</span>
          <span className="text-ink-dim">→ imp</span>
          <span className="text-ink-muted">{pct(ws.implied_wacc as number)}</span>
          {ws.gap_bps != null && (
            <span className={Math.abs(ws.gap_bps as number) > 200 ? 'text-amber-400' : 'text-ink-dim'}>
              {(ws.gap_bps as number) > 0 ? '+' : ''}{ws.gap_bps as number}bps
            </span>
          )}
        </div>
      )}
      {ig != null && (
        <div className="flex gap-3">
          <span className="text-ink-dim">Growth</span>
          <span className="text-ink-muted">{pct(ig)} imp</span>
        </div>
      )}
      {im != null && (
        <div className="flex gap-3">
          <span className="text-ink-dim">Margin</span>
          <span className="text-ink-muted">{pct(im)} imp</span>
        </div>
      )}
    </div>
  )
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
        <div><span className="text-ink-dim">CAPM </span><span className="text-ink-muted">{pct(capm)}</span></div>
        {implied != null && (
          <div><span className="text-ink-dim">market-implied </span><span className="text-ink-muted">{pct(implied)}</span></div>
        )}
        {gapBps != null && (
          <div className={flag ? 'text-amber-400' : 'text-ink-dim'}>
            {gapBps > 0 ? '+' : ''}{gapBps}bps {flag ? '⚠' : '✓'}
          </div>
        )}
      </div>
      {interpretation && <p className="text-ink-dim leading-relaxed">{interpretation}</p>}
    </div>
  )
}

function ReviewDeepDiveDetail({ meta }: { meta: Record<string, unknown> }) {
  const total = meta.total_findings as number | undefined
  const high = meta.high_findings as number | undefined
  const evidenceMemo = meta.evidence_memo_count as number | undefined
  const thesisAss = meta.thesis_assumption_count as number | undefined
  const consistency = meta.consistency_count as number | undefined
  const distinguishability = meta.distinguishability_count as number | undefined
  const anchoring = meta.anchoring_flags as string[] | undefined
  const shouldStop = meta.should_stop as boolean | undefined
  const iteration = meta.iteration as number | undefined

  return (
    <div className="space-y-1.5 text-[11px]">
      {iteration != null && (
        <div className="text-ink-dim">Iteration {iteration + 1}</div>
      )}
      <div className="flex gap-3">
        <span className={high ? 'text-amber-400 font-medium' : 'text-ink-dim'}>{high ?? 0} high-severity</span>
        <span className="text-ink-dim">{total ?? 0} total findings</span>
      </div>
      {(evidenceMemo != null || thesisAss != null || consistency != null || distinguishability != null) && (
        <div className="grid grid-cols-2 gap-x-3 gap-y-0.5 text-[10px]">
          {evidenceMemo != null && <div><span className="text-ink-dim">evidence↔memo </span><span className="text-ink-muted">{evidenceMemo}</span></div>}
          {thesisAss != null && <div><span className="text-ink-dim">thesis↔assumptions </span><span className="text-ink-muted">{thesisAss}</span></div>}
          {consistency != null && <div><span className="text-ink-dim">consistency </span><span className="text-ink-muted">{consistency}</span></div>}
          {distinguishability != null && <div><span className="text-ink-dim">distinguishability </span><span className="text-ink-muted">{distinguishability}</span></div>}
        </div>
      )}
      {anchoring && anchoring.length > 0 && (
        <div>
          <div className="text-ink-dim mb-0.5">Anchoring flags</div>
          {anchoring.map((f, i) => <div key={i} className="text-amber-400/80 text-[10px]">· {f}</div>)}
        </div>
      )}
      {shouldStop && <div className="text-emerald-500/70 text-[10px]">✓ No further review needed</div>}
    </div>
  )
}

function SynthesizeAdjustmentsDetail({ meta }: { meta: Record<string, unknown> }) {
  const changes = meta.changes as string[] | undefined
  const meaningful = meta.meaningful_count as number | undefined
  const shouldStop = meta.should_stop as boolean | undefined
  const adjustments = meta.adjustments as Record<string, Record<string, number>> | undefined

  return (
    <div className="space-y-1.5 text-[11px]">
      {meaningful != null && (
        <div className={meaningful > 0 ? 'text-ink-muted' : 'text-ink-dim'}>
          {meaningful > 0 ? `${meaningful} adjustments queued` : 'No meaningful adjustments'}
        </div>
      )}
      {adjustments && Object.entries(adjustments).some(([, f]) => Object.keys(f).length > 0) && (
        <div className="space-y-1">
          {Object.entries(adjustments).map(([scenario, fields]) =>
            Object.keys(fields).length > 0 ? (
              <div key={scenario}>
                <span className={`text-[10px] font-medium ${
                  scenario === 'bull' ? 'text-emerald-400' :
                  scenario === 'bear' ? 'text-red-400' : 'text-ink-muted'
                }`}>{scenario}</span>
                <div className="ml-2 space-y-0.5">
                  {Object.entries(fields).map(([field, delta]) => (
                    <div key={field} className="flex items-center gap-1.5 text-[10px]">
                      <span className="text-ink-dim">{field}:</span>
                      <span className={delta >= 0 ? 'text-emerald-400' : 'text-red-400'}>
                        {delta >= 0 ? '+' : ''}{delta.toFixed(4)}
                      </span>
                    </div>
                  ))}
                </div>
              </div>
            ) : null
          )}
        </div>
      )}
      {changes && changes.length > 0 && (
        <div className="text-ink-dim text-[10px] space-y-0.5">
          {changes.map((c, i) => <div key={i}>{c}</div>)}
        </div>
      )}
      {shouldStop && <div className="text-emerald-500/70 text-[10px]">✓ Converged</div>}
    </div>
  )
}

function ReviewSubgraphDetail({ meta }: { meta: Record<string, unknown> }) {
  const changes = meta.changes as string[] | undefined
  const reviewSummary = meta.review_summary as string | undefined
  const shouldStop = meta.should_stop as boolean | undefined

  return (
    <div className="space-y-1.5 text-[11px]">
      {reviewSummary && <p className="text-ink-dim leading-relaxed">{reviewSummary}</p>}
      {changes && changes.length > 0 && (
        <div>
          <div className="text-ink-dim mb-0.5">Changes applied</div>
          <div className="space-y-0.5 text-[10px]">
            {changes.map((c, i) => <div key={i} className="text-ink-muted">{c}</div>)}
          </div>
        </div>
      )}
      {shouldStop && <div className="text-emerald-500/70 text-[10px]">✓ No further review needed</div>}
    </div>
  )
}

function AssumptionJourneyDetail({ meta }: { meta: Record<string, unknown> }) {
  type Iter = {
    iteration: number
    adjustments?: Record<string, Record<string, number>>
    findings_summary?: string
    changes?: string[]
  }
  type AssumptionMap = Record<string, number>
  type InitialShape = { base?: AssumptionMap; scenarios?: Record<string, AssumptionMap> }

  const iterations = (meta.iterations as Iter[] | undefined) ?? []
  const initial = (meta.initial as InitialShape | undefined) ?? {}
  const final = (meta.final as InitialShape | undefined) ?? {}

  const FIELD_LABELS: Record<string, string> = {
    revenue_growth: 'Rev. growth',
    fcff_margin: 'FCFF margin',
    wacc: 'WACC',
    terminal_growth: 'Terminal growth',
    tax_rate: 'Tax rate',
  }
  const TRACKED_FIELDS = ['revenue_growth', 'fcff_margin', 'wacc', 'terminal_growth', 'tax_rate']
  const SCENARIO_COLOR: Record<string, string> = {
    base: 'text-ink-muted',
    bull: 'text-emerald-400',
    bear: 'text-red-400',
  }

  const pct = (v: number | undefined) => v == null ? '—' : `${(v * 100).toFixed(1)}%`

  // Build per-scenario journey rows: for each (scenario, field), show
  // initial value, per-iteration arrow + value, final value.
  const scenarioNames = ['base', ...Object.keys(initial.scenarios ?? {})].filter(
    (v, i, a) => a.indexOf(v) === i
  )

  const getValue = (scenario: string, field: string, source: InitialShape | undefined): number | undefined => {
    if (!source) return undefined
    if (scenario === 'base') return source.base?.[field]
    return source.scenarios?.[scenario]?.[field]
  }

  // Reconstruct per-iteration intermediate values: initial + cumulative deltas
  const valueAt = (scenario: string, field: string, upToIter: number): number | undefined => {
    const init = getValue(scenario, field, initial)
    if (init == null) return undefined
    let v = init
    for (let i = 0; i < upToIter; i++) {
      const delta = iterations[i]?.adjustments?.[scenario]?.[field]
      if (typeof delta === 'number') v += delta
    }
    return v
  }

  return (
    <div className="space-y-3 text-[11px]">
      <div className="text-ink-dim">
        {iterations.length} review iteration{iterations.length !== 1 ? 's' : ''} —
        showing how assumptions evolved
      </div>

      {scenarioNames.map(scenario => {
        const hasAnyData =
          getValue(scenario, 'revenue_growth', initial) != null ||
          getValue(scenario, 'fcff_margin', initial) != null
        if (!hasAnyData) return null

        return (
          <div key={scenario} className="border border-border rounded overflow-hidden">
            <div className={`px-2 py-1 text-[10px] font-medium uppercase tracking-wide bg-bg ${SCENARIO_COLOR[scenario] ?? 'text-ink-muted'}`}>
              {scenario}
            </div>
            <table className="w-full text-[10px]">
              <thead>
                <tr className="text-ink-dim">
                  <th className="text-left font-normal px-2 py-1">Field</th>
                  <th className="text-right font-normal px-2 py-1">Initial</th>
                  {iterations.map(it => (
                    <th key={it.iteration} className="text-right font-normal px-2 py-1">
                      After Rev {it.iteration + 1}
                    </th>
                  ))}
                  <th className="text-right font-normal px-2 py-1 text-ink-muted">Final</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-border-subtle">
                {TRACKED_FIELDS.map(field => {
                  const init = getValue(scenario, field, initial)
                  if (init == null) return null
                  const fin = getValue(scenario, field, final)
                  return (
                    <tr key={field}>
                      <td className="px-2 py-0.5 text-ink-dim">{FIELD_LABELS[field] ?? field}</td>
                      <td className="px-2 py-0.5 text-right text-ink-muted tabular-nums">{pct(init)}</td>
                      {iterations.map(it => {
                        const delta = it.adjustments?.[scenario]?.[field]
                        const after = valueAt(scenario, field, it.iteration + 1)
                        if (delta == null) {
                          return (
                            <td key={it.iteration} className="px-2 py-0.5 text-right text-zinc-700 tabular-nums">—</td>
                          )
                        }
                        const arrow = delta > 0 ? '↑' : '↓'
                        const color = delta > 0 ? 'text-emerald-400' : 'text-red-400'
                        return (
                          <td key={it.iteration} className={`px-2 py-0.5 text-right tabular-nums ${color}`}>
                            {arrow} {pct(after)}
                          </td>
                        )
                      })}
                      <td className="px-2 py-0.5 text-right text-ink font-medium tabular-nums">{pct(fin)}</td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        )
      })}

      {iterations.length > 0 && (
        <div className="space-y-1.5">
          <div className="text-ink-dim text-[10px] uppercase tracking-wide">Rationale</div>
          {iterations.map(it => (
            <div key={it.iteration} className="border-l-2 border-border-hover pl-2">
              <div className="text-ink-dim text-[10px] font-medium">
                Rev {it.iteration + 1}
              </div>
              {it.findings_summary && (
                <p className="text-ink-dim leading-relaxed mt-0.5">{it.findings_summary}</p>
              )}
              {it.changes && it.changes.length > 0 && (
                <div className="mt-1 text-[10px] text-ink-dim">
                  {it.changes.map((c, i) => <div key={i}>· {c}</div>)}
                </div>
              )}
            </div>
          ))}
        </div>
      )}
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
    case 'scenario_runner': return <ScenarioRunnerDetail meta={meta} />
    case 'scenario_generator': return <ScenarioGeneratorDetail meta={meta} />
    case 'compute_market_signals': return <MarketSignalsDetail meta={meta} />
    case 'review_subgraph': return <ReviewSubgraphDetail meta={meta} />
    case 'review_deep_dive': return <ReviewDeepDiveDetail meta={meta} />
    case 'synthesize_adjustments': return <SynthesizeAdjustmentsDetail meta={meta} />
    case 'assumption_journey': return <AssumptionJourneyDetail meta={meta} />
    case 'cache_check': return <KgCacheDetail meta={meta} />
    case 'kg_backwrite': return <KgBackwriteDetail meta={meta} />
    case 'detect_divergences': return <DivergencesDetail meta={meta} />
    case 'analysis': return <AnalysisPositionsDetail meta={meta} />
    case 'convergence_gate': return <ConvergenceGateDetail meta={meta} />
    default: return null
  }
}

// ── KG cache-check expander ────────────────────────────────────────────────

type KgCacheResult = {
  node_id: string
  node_type: string
  field: string
  status: 'hit' | 'miss' | 'stale'
  age_s: number | null
  action: string
  source?: string
  confidence?: number
}

function formatAge(s: number | null): string {
  if (s == null) return '—'
  if (s < 60) return `${Math.round(s)}s`
  if (s < 3600) return `${Math.round(s / 60)}m`
  if (s < 86400) return `${Math.round(s / 3600)}h`
  return `${Math.round(s / 86400)}d`
}

function KgCacheDetail({ meta }: { meta: Record<string, unknown> }) {
  const results = (meta.kg_cache_results as KgCacheResult[] | undefined) ?? []
  const hits = (meta.hit_count as number | undefined) ?? 0
  const misses = (meta.miss_count as number | undefined) ?? 0
  if (!results.length) return null

  return (
    <div className="space-y-2 text-[11px]">
      <div className="flex items-center gap-2 text-[10px]">
        <span className="px-1.5 py-0.5 rounded bg-teal-500/10 text-teal-400 border border-teal-500/20">
          {hits} hits
        </span>
        <span className="px-1.5 py-0.5 rounded bg-zinc-700/30 text-ink-dim border border-zinc-700/40">
          {misses} misses
        </span>
        <span className="text-ink-dim">cache-first lookups before expensive nodes</span>
      </div>

      <table className="w-full text-[10px] border-separate border-spacing-0">
        <thead>
          <tr className="text-ink-dim">
            <th className="px-2 py-0.5 text-left font-medium">Node</th>
            <th className="px-2 py-0.5 text-left font-medium">Status</th>
            <th className="px-2 py-0.5 text-right font-medium">Age</th>
            <th className="px-2 py-0.5 text-left font-medium">Action</th>
          </tr>
        </thead>
        <tbody>
          {results.map((r) => (
            <tr key={r.node_id} className="border-t border-border">
              <td className="px-2 py-0.5 font-mono text-ink-muted">
                {r.node_type}::{r.field}
              </td>
              <td className="px-2 py-0.5">
                {r.status === 'hit' && (
                  <span className="text-teal-400">⚡ hit</span>
                )}
                {r.status === 'miss' && (
                  <span className="text-ink-dim">✗ miss</span>
                )}
                {r.status === 'stale' && (
                  <span className="text-amber-400">↻ stale</span>
                )}
              </td>
              <td className="px-2 py-0.5 text-right tabular-nums text-ink-dim">
                {formatAge(r.age_s)}
              </td>
              <td className="px-2 py-0.5 text-ink-dim">{r.action}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function KgBackwriteDetail({ meta }: { meta: Record<string, unknown> }) {
  const runNodeId = meta.run_node_id as string | undefined
  const ticker = meta.ticker as string | undefined
  return (
    <div className="space-y-1 text-[11px]">
      <div className="text-ink-dim">
        Persisted DCF run + assumptions + outputs to KG for{' '}
        <span className="text-ink-muted font-medium">{ticker || '?'}</span>
      </div>
      {runNodeId && (
        <div className="font-mono text-[10px] text-ink-dim">
          {runNodeId}
        </div>
      )}
      <div className="text-[10px] text-ink-dim">
        Next DCF for this ticker can use these as cached priors.
      </div>
    </div>
  )
}

// ── Divergence / Analysis / Convergence-gate detail components ──────────────

interface DivergenceRecord {
  id: string
  kind: string
  severity: string
  summary: string
  details?: Record<string, unknown>
}

function severityBadge(sev: string): { cls: string; label: string } {
  switch (sev) {
    case 'critical': return { cls: 'bg-red-500/15 text-red-300 border-red-500/40', label: 'critical' }
    case 'high':     return { cls: 'bg-orange-500/15 text-orange-300 border-orange-500/40', label: 'high' }
    case 'medium':   return { cls: 'bg-amber-500/15 text-amber-300 border-amber-500/40', label: 'medium' }
    default:         return { cls: 'bg-zinc-700/30 text-ink-muted border-zinc-700/40', label: sev || 'low' }
  }
}

function DivergencesDetail({ meta }: { meta: Record<string, unknown> }) {
  const divergences = (meta.divergences as DivergenceRecord[] | undefined) ?? []
  const count = (meta.count as number | undefined) ?? divergences.length
  if (!divergences.length) {
    return (
      <div className="text-[11px] text-ink-dim">
        No divergences detected — model and market signals align.
      </div>
    )
  }
  return (
    <div className="space-y-2 text-[11px]">
      <div className="text-[10px] text-ink-dim">
        {count} gap(s) between model output and market-implied / evidence signals.
      </div>
      <div className="space-y-1.5">
        {divergences.map(d => {
          const sb = severityBadge(d.severity)
          return (
            <div key={d.id} className="border border-border rounded p-2 bg-bg">
              <div className="flex items-center gap-2 mb-1">
                <span className={`px-1.5 py-0.5 rounded text-[9px] uppercase border ${sb.cls}`}>
                  {sb.label}
                </span>
                <span className="text-[10px] font-mono text-ink-dim">{d.kind}</span>
              </div>
              <div className="text-ink-muted">{d.summary}</div>
            </div>
          )
        })}
      </div>
    </div>
  )
}

interface AnalysisPosition {
  divergence_id: string
  divergence_summary?: string
  divergence_severity?: string
  position: string  // EXPLAINED | UNEXPLAINED
  explanation: string
  evidence_used: string[]
  new_evidence_fetched?: string[]
  adjustment?: { field: string; delta: number; reason: string } | null
  uncertainty_note?: string | null
}

function AnalysisPositionsDetail({ meta }: { meta: Record<string, unknown> }) {
  const positions = (meta.positions as AnalysisPosition[] | undefined) ?? []
  const changes = (meta.changes as string[] | undefined) ?? []
  const eff = meta.effective_confidence as number | undefined
  const base = meta.base_confidence as number | undefined

  if (!positions.length) {
    return <div className="text-[11px] text-ink-dim">Nothing to analyze — model accepted as-is.</div>
  }

  return (
    <div className="space-y-2 text-[11px]">
      {(eff != null && base != null) && (
        <div className="flex items-center gap-3 text-[10px]">
          <span className="text-ink-dim">Confidence:</span>
          <span className="text-ink-muted">base {(base * 100).toFixed(0)}%</span>
          <span className="text-ink-dim">→</span>
          <span className={eff < base ? 'text-amber-400 font-medium' : 'text-emerald-400 font-medium'}>
            effective {(eff * 100).toFixed(0)}%
          </span>
          {eff < base && (
            <span className="text-[9px] px-1.5 py-0.5 rounded bg-amber-500/10 text-amber-400 border border-amber-500/30">
              penalized by unexplained gaps
            </span>
          )}
        </div>
      )}

      {changes.length > 0 && (
        <div className="text-[10px]">
          <div className="text-ink-dim mb-1">Adjustments applied:</div>
          <ul className="space-y-0.5">
            {changes.map((c, i) => (
              <li key={i} className="font-mono text-emerald-300/80 pl-2">• {c}</li>
            ))}
          </ul>
        </div>
      )}

      <div className="space-y-1.5">
        {positions.map((p, i) => {
          const explained = p.position === 'EXPLAINED'
          return (
            <div
              key={i}
              className={`border rounded p-2 ${explained ? 'border-emerald-500/30 bg-emerald-500/5' : 'border-amber-500/30 bg-amber-500/5'}`}
            >
              <div className="flex items-center gap-2 mb-1">
                <span className={`px-1.5 py-0.5 rounded text-[9px] uppercase border ${
                  explained
                    ? 'bg-emerald-500/15 text-emerald-300 border-emerald-500/40'
                    : 'bg-amber-500/15 text-amber-300 border-amber-500/40'
                }`}>
                  {p.position}
                </span>
                <span className="text-[10px] font-mono text-ink-dim">{p.divergence_id}</span>
                {p.divergence_severity && (
                  <span className="text-[9px] text-ink-dim">[{p.divergence_severity}]</span>
                )}
              </div>
              {p.divergence_summary && (
                <div className="text-[10px] text-ink-dim mb-1 italic">{p.divergence_summary}</div>
              )}
              <div className="text-ink-muted leading-snug">{p.explanation}</div>
              {p.adjustment && (
                <div className="mt-1.5 text-[10px] text-emerald-300 font-mono">
                  → {p.adjustment.field} {p.adjustment.delta >= 0 ? '+' : ''}{(p.adjustment.delta * 100).toFixed(2)}pp
                  <div className="text-ink-dim not-italic font-sans mt-0.5">{p.adjustment.reason}</div>
                </div>
              )}
              {p.uncertainty_note && (
                <div className="mt-1.5 text-[10px] text-amber-300/90 italic">⚠ {p.uncertainty_note}</div>
              )}
              {((p.evidence_used?.length ?? 0) + (p.new_evidence_fetched?.length ?? 0)) > 0 && (
                <div className="mt-1.5 text-[9px] text-ink-dim">
                  evidence: {p.evidence_used?.join(', ')}
                  {p.new_evidence_fetched?.length ? (
                    <span className="text-teal-500"> + {p.new_evidence_fetched.length} fetched</span>
                  ) : null}
                </div>
              )}
            </div>
          )
        })}
      </div>
    </div>
  )
}

function ConvergenceGateDetail({ meta }: { meta: Record<string, unknown> }) {
  const validity = (meta.validity as string) || 'valid'
  const reason = (meta.reason as string) || ''
  const iteration = meta.iteration as number | undefined
  const unexplained = meta.unexplained_count as number | undefined
  const adjustments = meta.adjustments_pending as number | undefined
  const critical = meta.critical_unexplained as number | undefined

  const palette = validity === 'invalid'
    ? { box: 'border-red-500/40 bg-red-500/5', badge: 'bg-red-500/15 text-red-300 border-red-500/40' }
    : validity === 'adjusting'
    ? { box: 'border-indigo-500/40 bg-indigo-500/5', badge: 'bg-indigo-500/15 text-indigo-300 border-indigo-500/40' }
    : { box: 'border-emerald-500/40 bg-emerald-500/5', badge: 'bg-emerald-500/15 text-emerald-300 border-emerald-500/40' }

  return (
    <div className={`text-[11px] border rounded p-2 ${palette.box}`}>
      <div className="flex items-center gap-2 mb-1.5">
        <span className={`px-1.5 py-0.5 rounded text-[9px] uppercase font-medium border ${palette.badge}`}>
          {validity}
        </span>
        {iteration != null && (
          <span className="text-[10px] text-ink-dim">iter {iteration}</span>
        )}
      </div>
      <div className="text-ink-muted leading-snug">{reason}</div>
      <div className="mt-2 grid grid-cols-3 gap-2 text-[10px]">
        <div>
          <div className="text-ink-dim">unexplained</div>
          <div className={`tabular-nums ${unexplained ? 'text-amber-400' : 'text-ink-muted'}`}>{unexplained ?? 0}</div>
        </div>
        <div>
          <div className="text-ink-dim">adjustments queued</div>
          <div className={`tabular-nums ${adjustments ? 'text-indigo-300' : 'text-ink-muted'}`}>{adjustments ?? 0}</div>
        </div>
        <div>
          <div className="text-ink-dim">critical</div>
          <div className={`tabular-nums ${critical ? 'text-red-400' : 'text-ink-muted'}`}>{critical ?? 0}</div>
        </div>
      </div>
    </div>
  )
}

// ── DcfSubstepRow ─────────────────────────────────────────────────────────────

function DcfSubstepRow({ entry, isLastStep }: { entry: ChildEntry; isLastStep?: boolean }) {
  const [open, setOpen] = useState(false)
  const settling = useSettleOn(entry.status, 'completed')
  const display = getToolDisplay(entry.name)
  const stepName = entry.name.includes(':') ? entry.name.split(':').pop()! : entry.name
  const meta = entry.meta
  // A row is expandable if it has detail data OR it has sub-children
  const subChildren = entry.subChildren ?? []
  const hasDetail = (entry.status === 'completed' && meta && Object.keys(meta).length > 1) || subChildren.length > 0
  const cleaned = cleanToolSummary(entry.summary)
  // Iteration badge: shown when scenario_runner ran more than once
  const runCount = (meta as Record<string, unknown> | undefined)?.run_count as number | undefined
  const isReRerun = runCount != null && runCount > 1
  // Fallback-thesis warning badge
  const thesisFallback = (meta as Record<string, unknown> | undefined)?.thesis_quality === 'fallback'
  // KG cache hit indicator — node skipped because output was cached
  const kgHit = (meta as Record<string, unknown> | undefined)?.kg_status === 'hit'
    || (meta as Record<string, unknown> | undefined)?.thesis_quality === 'cached'

  //── Dot animation: flash-dot on completion (unless last step in group) ──
  const showFlashDot = entry.status === 'completed' && settling && !isLastStep

  return (
    <div className="text-[11px] animate-step-reveal animate-row-flash rounded-sm px-0.5 -mx-0.5">
      <button
        onClick={() => hasDetail && setOpen(o => !o)}
        disabled={!hasDetail}
        className="w-full flex items-center gap-2 text-left text-ink-dim hover:text-ink-muted disabled:hover:text-ink-dim transition-colors"
      >
        <span className={`w-1 h-1 rounded-full flex-shrink-0 ${
          kgHit ? 'bg-teal-400' :
          thesisFallback ? 'bg-amber-400' :
          entry.status === 'completed' ? `bg-emerald-500 ${showFlashDot ? 'animate-flash-dot' : ''}` :
          entry.status === 'error' ? 'bg-red-500' :
          entry.status === 'skipped' ? 'bg-zinc-700' :
          'bg-indigo-400 animate-pulse'
        }`} />
        <span className={`font-medium ${
          kgHit ? 'text-teal-300' :
          thesisFallback ? 'text-amber-400' : 'text-ink-muted'
        }`}>{display.label}</span>
        {/* KG cache-hit indicator */}
        {kgHit && (
          <span className="flex-shrink-0 px-1 py-px rounded text-[9px] bg-teal-500/10 text-teal-400 border border-teal-500/20">
            ⚡ KG
          </span>
        )}
        {/* Fallback thesis warning badge */}
        {thesisFallback && (
          <span className="flex-shrink-0 px-1 py-px rounded text-[9px] bg-amber-500/10 text-amber-400 border border-amber-500/20">
            ⚠ fallback
          </span>
        )}
        {/* Re-run badge: ×2 when scenario_runner looped after review */}
        {isReRerun && (
          <span className="flex-shrink-0 px-1 py-px rounded text-[9px] bg-indigo-950/60 text-indigo-400">
            ×{runCount}
          </span>
        )}
        {/* Sub-step count badge for subgroup containers (e.g. review_subgraph) */}
        {subChildren.length > 0 && (
          <span className="text-zinc-700 text-[10px] flex-shrink-0">
            {subChildren.filter(s => s.status === 'completed').length}/{subChildren.length}
          </span>
        )}
        {cleaned && <span className="text-ink-dim truncate min-w-0 flex-1">· {cleaned}</span>}
        {hasDetail && (
          <span className="ml-auto text-zinc-700 flex-shrink-0">
            <svg width="7" height="7" viewBox="0 0 8 8" fill="none"
              className={`transition-transform duration-150 ${open ? 'rotate-180' : ''}`}>
              <path d="M1 2.5L4 5.5L7 2.5" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round" />
            </svg>
          </span>
        )}
      </button>

      {/* Smooth height expand via grid-rows trick */}
      <div className={`grid transition-[grid-template-rows] duration-200 ease-out ${open ? 'grid-rows-[1fr]' : 'grid-rows-[0fr]'}`}>
        <div className="overflow-hidden">
          {/* Sub-steps (e.g. review_deep_dive / synthesize_adjustments inside review_subgraph) */}
          {subChildren.length > 0 && (
            <div className="ml-3 mt-1 pl-2 border-l border-border space-y-1 pb-1">
              {subChildren.map((child, i) => (
                <DcfSubstepRow key={child.activity_id || `sub-${i}`} entry={child} />
              ))}
            </div>
          )}
          {/* Step detail panel */}
          {meta && Object.keys(meta).length > 1 && (
            <div className="ml-3 mt-1.5 pl-2 border-l border-border-hover pb-1">
              <DcfStepDetail stepName={stepName} meta={meta as Record<string, unknown>} />
            </div>
          )}
        </div>
      </div>
    </div>
  )
}

// ── Evidence Panel ────────────────────────────────────────────────────────────

const TIER_BADGE: Record<string, { bg: string; text: string; label: string }> = {
  filing:         { bg: 'bg-violet-950/50', text: 'text-violet-400', label: 'filing' },
  structured_api: { bg: 'bg-blue-950/50',   text: 'text-blue-400',   label: 'api' },
  document:       { bg: 'bg-emerald-950/50',text: 'text-emerald-400',label: 'doc' },
  news:           { bg: 'bg-amber-950/50',  text: 'text-amber-400',  label: 'news' },
  generic_web:    { bg: 'bg-zinc-900',      text: 'text-ink-dim',   label: 'web' },
}

const TIER_ORDER = ['filing', 'structured_api', 'document', 'news', 'generic_web']

function EvidenceItemRow({ item }: { item: EvidenceItem }) {
  const [expanded, setExpanded] = useState(false)
  const badge = TIER_BADGE[item.source_tier] ?? { bg: 'bg-zinc-900', text: 'text-ink-dim', label: item.source_tier }
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
                className="text-ink-muted hover:text-ink truncate flex-1 underline underline-offset-2"
                title={title}
              >
                {title}
              </a>
            ) : (
              <span className="text-ink-muted truncate flex-1" title={title}>{title}</span>
            )}
            {item.as_of && <span className="text-zinc-700 flex-shrink-0">{item.as_of.slice(0, 10)}</span>}
            {hasText && (
              <button onClick={() => setExpanded(o => !o)} className="text-zinc-700 hover:text-ink-muted flex-shrink-0">
                <svg width="6" height="6" viewBox="0 0 8 8" fill="none" className={`transition-transform duration-100 ${expanded ? 'rotate-180' : ''}`}>
                  <path d="M1 2.5L4 5.5L7 2.5" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round" />
                </svg>
              </button>
            )}
          </div>
          {item.value != null && (
            <span className="text-ink-dim">{item.field}: {item.value}</span>
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

/** Compact inline citation used inside per-assumption expanded rows. */
function CitationChip({ item }: { item: EvidenceItem }) {
  const [open, setOpen] = useState(false)
  const badge = TIER_BADGE[item.source_tier] ?? { bg: 'bg-zinc-900', text: 'text-ink-dim', label: item.source_tier }
  const title = item.title || item.field || item.source || item.evidence_id
  return (
    <div className="text-[9px]">
      <div className="flex items-start gap-1.5">
        <span className={`flex-shrink-0 mt-0.5 px-1 py-0.5 rounded font-medium ${badge.bg} ${badge.text}`}>
          {badge.label}
        </span>
        <div className="min-w-0 flex-1">
          {item.url ? (
            <a href={item.url} target="_blank" rel="noopener noreferrer"
              className="text-ink-dim hover:text-ink-muted underline underline-offset-2 truncate block transition-colors"
              title={title}>
              {title}
            </a>
          ) : (
            <span className="text-ink-dim truncate block" title={title}>{title}</span>
          )}
        </div>
        {item.text && (
          <button onClick={() => setOpen(o => !o)} className="flex-shrink-0 text-zinc-700 hover:text-ink-muted mt-0.5">
            <svg width="6" height="6" viewBox="0 0 8 8" fill="none" className={`transition-transform duration-100 ${open ? 'rotate-180' : ''}`}>
              <path d="M1 2.5L4 5.5L7 2.5" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round" />
            </svg>
          </button>
        )}
      </div>
      {open && item.text && (
        <p className="mt-0.5 ml-8 text-zinc-700 leading-relaxed border-l border-zinc-800 pl-2 line-clamp-3">
          {item.text.slice(0, 300)}
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
    <div className="border-t border-border">
      <button
        onClick={() => setOpen(o => !o)}
        className="w-full flex items-center gap-2 px-3 py-1.5 text-[10px] hover:bg-bg-overlay transition-colors text-left"
      >
        <span className="text-ink-dim font-medium">Sources</span>
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
        <div className="px-3 pb-2 divide-y divide-border-subtle max-h-56 overflow-y-auto">
          {sorted.map(item => <EvidenceItemRow key={item.evidence_id} item={item} />)}
        </div>
      )}
    </div>
  )
}

// ── Main ActivityTrace ────────────────────────────────────────────────────────

type DcfHitlField = {
  key: string
  label: string
  optional?: boolean
  group?: string
}

const DCF_HITL_FIELDS: DcfHitlField[] = [
  // Core (required)
  { key: 'revenue_growth', label: 'Revenue Growth (Y1-Y2)' },
  { key: 'revenue_growth_terminal', label: 'Revenue Growth (Y5 fade)', optional: true, group: 'glide' },
  { key: 'fcff_margin', label: 'FCFF Margin (Y1)' },
  { key: 'fcff_margin_terminal', label: 'FCFF Margin (Y5 terminal)', optional: true, group: 'glide' },
  { key: 'terminal_growth', label: 'Perpetuity Growth' },
  { key: 'tax_rate', label: 'Tax Rate' },
  { key: 'wacc', label: 'WACC' },
  // Capital structure & dilution (optional, real-world mechanics)
  { key: 'buyback_yield', label: 'Buyback Yield', optional: true, group: 'capital' },
  { key: 'sbc_pct_revenue', label: 'SBC % of Revenue', optional: true, group: 'capital' },
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

  // Build evidence lookup by ID so per-field citations can resolve items.
  const evidenceById = useMemo(() => {
    const map: Record<string, EvidenceItem> = {}
    for (const item of review.evidence_items ?? []) {
      map[item.evidence_id] = item
    }
    return map
  }, [review.evidence_items])

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
    <div className="mt-3 rounded border border-border bg-bg overflow-hidden">
      <div className="flex items-center gap-2 px-3 py-2 border-b border-border text-[11px]">
        <span className="w-1.5 h-1.5 rounded-full bg-amber-400 animate-pulse flex-shrink-0" />
        <span className="text-ink-muted font-medium">Review assumptions — {review.ticker}</span>
        <span className="ml-auto text-ink-dim">{review.horizon_years}yr horizon</span>
      </div>
      <div className="divide-y divide-border-subtle">
        {DCF_HITL_FIELDS.map(f => {
          const val = review.assumptions[f.key]
          const prov = review.provenance[f.key] ?? {}
          const proposal = review.memo_proposals?.[f.key]
          const isOpen = !!expanded[f.key]
          // Resolve per-field citation items from evidence_refs list in provenance.
          const fieldRefs = (prov.evidence_refs ?? [])
            .map(id => evidenceById[id])
            .filter((item): item is EvidenceItem => item != null)
          const hasDetail = !!(proposal?.rationale || prov.source || fieldRefs.length > 0)
          const conf = proposal?.confidence ?? (prov.confidence as number | undefined)
          const confPct = conf != null ? Math.round(conf * 100) : null
          const confColor = confPct == null ? '' : confPct >= 80 ? 'text-emerald-400' : confPct >= 60 ? 'text-amber-400' : 'text-red-400'
          // Hide optional fields when LLM didn't propose them (no value, no proposal)
          if (f.optional && val == null && !proposal) {
            return null
          }
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
                  <span className="text-ink-dim">{f.label}</span>
                  {f.optional && (
                    <span className="text-[8px] uppercase tracking-wider text-zinc-700 ml-1">opt</span>
                  )}
                  {fieldRefs.length > 0 && !isOpen && (
                    <span className="text-[8px] text-zinc-700 ml-1">[{fieldRefs.length}]</span>
                  )}
                </button>
                <span className="text-ink font-medium tabular-nums">{fmtPct(val)}</span>
                <span className="text-zinc-700 truncate text-[10px]">{prov.source ?? '—'}</span>
                {confPct != null ? (
                  <span className={`${confColor} tabular-nums`}>{confPct}%</span>
                ) : <span />}
              </div>
              {isOpen && (
                <div className="px-3 pb-2 space-y-1.5 bg-bg">
                  {proposal?.rationale && (
                    <p className="text-[10px] text-ink-dim leading-relaxed border-l border-zinc-800 pl-2">
                      {proposal.rationale}
                    </p>
                  )}
                  {fieldRefs.length > 0 && (
                    <div className="space-y-1 border-l border-zinc-800 pl-2">
                      <span className="text-[9px] text-zinc-700 uppercase tracking-wider">Sources</span>
                      {fieldRefs.map(item => (
                        <CitationChip key={item.evidence_id} item={item} />
                      ))}
                    </div>
                  )}
                  <div className="flex items-center gap-1.5">
                    <span className="text-[10px] text-zinc-700 w-16 flex-shrink-0">Override %</span>
                    <input
                      type="number" step="0.1"
                      placeholder={fmtPct(val)}
                      value={edits[f.key] ?? ''}
                      onChange={e => setEdits(prev => ({ ...prev, [f.key]: e.target.value }))}
                      className="flex-1 px-2 py-0.5 rounded bg-bg-input border border-border-hover text-ink-muted text-[10px] placeholder-ink-dim focus:outline-none"
                    />
                    {edits[f.key] && (
                      <button
                        onClick={() => setEdits(prev => { const n = { ...prev }; delete n[f.key]; return n })}
                        className="text-[10px] text-zinc-700 hover:text-ink-muted"
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
      <div className="flex items-center gap-2 px-3 py-2 border-t border-border">
        <button
          onClick={handleApprove}
          className="px-2.5 py-1 rounded bg-indigo-600 hover:bg-indigo-500 text-[11px] font-medium text-white transition-colors"
        >
          {hasEdits ? 'Apply edits & Run' : 'Approve & Run'}
        </button>
        <button
          onClick={handleReject}
          className="px-2.5 py-1 rounded border border-border-hover text-[11px] text-ink-dim hover:text-ink-muted transition-colors"
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
  variant = 'panel',
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
  /** `panel` = bordered box (right-bar / report card). `inline` = borderless
   *  conversational summary line rendered in the chat flow. */
  variant?: 'panel' | 'inline'
  dcfReview?: DcfReviewState
  onDcfApprove?: (overrides?: Record<string, number>) => void
  onDcfReject?: () => void
  threadId?: string
}) {
  const inline = variant === 'inline'
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
  } else if (inline) {
    // Settled inline strip: ChatGPT-style aggregated phrasing, e.g.
    // "Searched web ×2 · Calculated". Falls back to step count if empty.
    const names =
      grouped !== null
        ? grouped.map(item => (item.kind === 'group' ? item.parent.name : item.entry.name))
        : flatRows.map(tc => tc.tool_name)
    summaryText = summarizeToolActions(names) || `${total} step${total === 1 ? '' : 's'}`
  } else {
    summaryText = `${total} step${total === 1 ? '' : 's'}`
  }

  return (
    <div className={inline ? 'overflow-hidden' : 'rounded-lg border border-border bg-bg overflow-hidden'}>
      <button
        onClick={() => setOpen(o => !o)}
        className={inline
          ? 'w-full flex items-center gap-2 py-1 text-left text-[11px] text-ink-dim hover:text-ink-muted transition-colors'
          : 'w-full flex items-center gap-2 px-3 py-1.5 text-left text-[11px] hover:bg-bg-overlay transition-colors'}
      >
        <span className={`w-1.5 h-1.5 rounded-full flex-shrink-0 ${
          running > 0 ? 'bg-indigo-400 animate-pulse' : errors > 0 ? 'bg-red-500' : 'bg-emerald-500'
        }`} />
        {!inline && <span className="text-ink-muted font-medium tracking-wide">{label}</span>}
        {!inline && <span className="text-zinc-700">·</span>}
        <span className="text-ink-dim">{summaryText}</span>
        <span className="ml-auto text-zinc-700">
          <svg width="9" height="9" viewBox="0 0 8 8" fill="none"
            className={`transition-transform duration-150 ${open ? 'rotate-180' : ''}`}>
            <path d="M1 2.5L4 5.5L7 2.5" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round" />
          </svg>
        </span>
      </button>

      <div className={`grid transition-[grid-template-rows] duration-200 ease-out ${open ? 'grid-rows-[1fr]' : 'grid-rows-[0fr]'}`}>
        <div className="overflow-hidden">
          <div className={inline ? 'pl-3.5 py-1.5 space-y-1.5' : 'border-t border-border px-3 py-3 space-y-1.5'}>
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
        </div>
      </div>
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
  const settling = useSettleOn(parent.status, 'completed')

  const isDcf = parent.name === 'workflow:dcf'

  const confidenceColor =
    parent.confidence_label === 'HIGH' ? 'bg-emerald-950/80 text-emerald-400' :
    parent.confidence_label === 'MEDIUM' ? 'bg-zinc-900 text-ink-muted' :
    parent.confidence_label === 'LOW' ? 'bg-amber-950/80 text-amber-400' : null

  return (
    <div className="rounded border border-border bg-bg overflow-hidden">
      <button
        onClick={() => setOpen(o => !o)}
        className="w-full flex items-center gap-2 px-2.5 py-1.5 text-left text-[11px] hover:bg-bg-overlay transition-colors"
      >
        <span className={`w-1 h-1 rounded-full flex-shrink-0 ${
          isRunning ? 'bg-indigo-400 animate-pulse' :
          isError ? 'bg-red-500' :
          `bg-violet-500 ${settling ? 'animate-settle' : ''}`
        }`} />
        <span className="font-medium text-violet-300">{display.label}</span>
        {children.length > 0 && (
          <span className="text-ink-dim transition-opacity duration-200">{doneCount}/{children.length}</span>
        )}
        {parent.summary && (
          <span className="text-ink-dim truncate min-w-0 flex-1">· {parent.summary}</span>
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

      <div className={`grid transition-[grid-template-rows] duration-200 ease-out ${open && children.length > 0 ? 'grid-rows-[1fr]' : 'grid-rows-[0fr]'}`}>
        <div className="overflow-hidden">
          <div className="border-t border-border-subtle px-3 py-1.5 space-y-1.5 ml-1">
            {children.map((child, i) =>
              isDcf
                ? <DcfSubstepRow key={child.activity_id || `c-${i}`} entry={child} isLastStep={i === children.length - 1} />
                : <ActivityRow key={child.activity_id || `c-${i}`} tc={entryToRow(child)} />
            )}
          </div>
        </div>
      </div>
    </div>
  )
}

// ── ActivityRow ───────────────────────────────────────────────────────────────

function ActivityRow({ tc }: { tc: ToolCall }) {
  const [open, setOpen] = useState(false)
  const settling = useSettleOn(tc.status, 'done')
  const display = getToolDisplay(tc.tool_name)
  const cleaned = cleanToolSummary(tc.summary)
  const expandable = tc.status !== 'running' && cleaned.length > 0

  return (
    <div className="text-[11px]">
      <button
        onClick={() => expandable && setOpen(o => !o)}
        disabled={!expandable}
        className="w-full flex items-center gap-2 text-left text-ink-dim hover:text-ink-muted disabled:hover:text-ink-dim transition-colors"
      >
        <span className={`w-1 h-1 rounded-full flex-shrink-0 ${
          tc.status === 'done' ? `bg-emerald-500 ${settling ? 'animate-settle' : ''}` :
          tc.status === 'error' ? 'bg-red-500' :
          'bg-indigo-400 animate-pulse'
        }`} />
        <span className={`font-medium ${display.group === 'workflow' ? 'text-violet-300' : 'text-ink-muted'}`}>
          {display.label}
        </span>
        {tc.args_preview && (
          <span className="text-ink-dim truncate min-w-0">"{tc.args_preview}"</span>
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

      {/* Smooth height expand */}
      <div className={`grid transition-[grid-template-rows] duration-200 ease-out ${open ? 'grid-rows-[1fr]' : 'grid-rows-[0fr]'}`}>
        <div className="overflow-hidden">
          {cleaned && (
            <p className="ml-3 mt-1 pl-2 border-l border-border-hover text-ink-dim leading-relaxed">{cleaned}</p>
          )}
        </div>
      </div>
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
    <div className="rounded-lg border border-border bg-bg overflow-hidden">
      <button
        onClick={() => setOpen(o => !o)}
        className="w-full flex items-center gap-2 px-3 py-1.5 text-left text-[11px] hover:bg-bg-overlay transition-colors"
      >
        <span className={`w-1.5 h-1.5 rounded-full flex-shrink-0 ${failed > 0 ? 'bg-red-500' : 'bg-emerald-500'}`} />
        <span className="text-ink-muted font-medium tracking-wide">Research plan</span>
        <span className="text-zinc-700">·</span>
        <span className="text-ink-dim">
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
        <div className="border-t border-border px-3 py-2.5 space-y-2">
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
        <span className="font-medium text-ink-dim tabular-nums w-5 flex-shrink-0">
          {String(index + 1).padStart(2, '0')}
        </span>
        <span className="text-ink-muted leading-relaxed">{step.description || 'Research step'}</span>
      </div>
      {toolCalls.length > 0 && (
        <div className="ml-7 mt-1 space-y-1">
          {toolCalls.map((tc, i) => <ActivityRow key={`${step.id}-${i}`} tc={tc} />)}
        </div>
      )}
    </div>
  )
}
