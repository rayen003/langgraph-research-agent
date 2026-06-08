import { Calculator, ExternalLink } from 'lucide-react'
import type { KgNode } from '../hooks/useKnowledgeGraph'
import { Panel } from './kg/Panel'

interface Props {
  ticker: string
  runs: KgNode[]
  allNodes: KgNode[]
  highlightIds?: Set<string>
  onClose: () => void
  onSelectRun: (run: KgNode) => void
}

interface HistoryRow {
  run: KgNode
  runId: string
  date: string
  age: string
  horizonYears: number
  implied?: number
  spot?: number
  upside?: number
  reportUrl: string
}

function asObj(v: unknown): Record<string, unknown> {
  return v && typeof v === 'object' && !Array.isArray(v) ? (v as Record<string, unknown>) : {}
}

function fmtDate(ts: number): string {
  return new Date(ts * 1000).toLocaleDateString(undefined, {
    month: 'short',
    day: 'numeric',
    year: 'numeric',
  })
}

function ageStr(ts: number): string {
  const age = Math.max(0, Date.now() / 1000 - ts)
  if (age < 86400) return `${Math.max(1, Math.round(age / 3600))}h ago`
  if (age < 86400 * 30) return `${Math.round(age / 86400)}d ago`
  return `${Math.round(age / (86400 * 30))}mo ago`
}

function money(v?: number): string {
  if (v == null || !isFinite(v)) return '—'
  return `$${v.toLocaleString(undefined, { maximumFractionDigits: 0 })}`
}

function pct(v?: number): string {
  if (v == null || !isFinite(v)) return '—'
  return `${v >= 0 ? '+' : ''}${(v * 100).toFixed(1)}%`
}

function numValue(node: KgNode | undefined): number | undefined {
  if (!node) return undefined
  const raw = node.value
  const value = typeof raw === 'number' ? raw : Number(raw)
  return isFinite(value) ? value : undefined
}

export function KgDcfHistoryPanel({
  ticker,
  runs,
  allNodes,
  highlightIds,
  onClose,
  onSelectRun,
}: Props) {
  const rows: HistoryRow[] = [...runs]
    .sort((a, b) => b.updated_at - a.updated_at)
    .map(run => {
      const runId = run.run_id || ''
      const outputs = allNodes.filter(n => n.ticker === ticker && n.run_id === runId && n.node_type === 'run_output')
      const implied = numValue(outputs.find(n => n.field === 'implied_share_price'))
      const spot = numValue(outputs.find(n => n.field === 'current_price'))
      const upside = implied != null && spot != null && spot !== 0 ? (implied - spot) / spot : undefined
      const value = asObj(run.value)
      const horizonYears = Number(value.horizon_years) || 5
      const threadId = String(value.thread_id ?? '')
      return {
        run,
        runId,
        date: fmtDate(run.updated_at),
        age: ageStr(run.updated_at),
        horizonYears,
        implied,
        spot,
        upside,
        reportUrl: threadId ? `/runs/${encodeURIComponent(threadId)}/dcf-report.pdf?inline=1` : '',
      }
    })

  return (
    <Panel
      icon={<Calculator size={16} />}
      title={`${ticker} · DCF History`}
      subtitle={`${rows.length} older run${rows.length === 1 ? '' : 's'} · newest first`}
      onClose={onClose}
    >
      <div className="p-3">
        <div className="mb-2 grid grid-cols-[1fr_64px_64px_64px_54px] gap-2 px-2 text-[9px] uppercase tracking-wide text-ink-dim">
          <span>Run</span>
          <span className="text-right">Implied</span>
          <span className="text-right">Spot</span>
          <span className="text-right">Upside</span>
          <span className="text-right">Open</span>
        </div>
        <div className="space-y-1.5">
          {rows.map(row => {
            const hot = highlightIds?.has(row.run.id)
            const upsideTone = row.upside == null
              ? 'text-ink-dim'
              : row.upside >= 0
                ? 'text-emerald-300'
                : 'text-rose-300'
            return (
              <div
                key={row.run.id}
                className={`grid grid-cols-[1fr_64px_64px_64px_54px] items-center gap-2 rounded-md border px-2 py-2 transition ${
                  hot
                    ? 'border-accent/60 bg-accent-soft ring-1 ring-accent/30'
                    : 'border-edge bg-surface hover:border-edge-2 hover:bg-surface-2'
                }`}
              >
                <button
                  type="button"
                  onClick={() => onSelectRun(row.run)}
                  className="min-w-0 text-left"
                  title={row.runId}
                >
                  <div className="flex min-w-0 items-center gap-2">
                    <span className="h-1.5 w-1.5 shrink-0 rounded-full bg-accent" />
                    <span className="truncate text-[12px] font-medium text-ink">{row.date}</span>
                    <span className="shrink-0 rounded border border-edge bg-surface-2 px-1.5 py-0.5 text-[9px] text-ink-dim">
                      {row.horizonYears}y
                    </span>
                  </div>
                  <div className="mt-0.5 truncate pl-3.5 font-mono text-[9px] text-ink-dim">
                    {row.age} · {row.runId || row.run.id}
                  </div>
                </button>
                <span className="text-right font-mono text-[11px] text-ink">{money(row.implied)}</span>
                <span className="text-right font-mono text-[11px] text-ink-muted">{money(row.spot)}</span>
                <span className={`text-right font-mono text-[11px] ${upsideTone}`}>{pct(row.upside)}</span>
                <span className="flex justify-end">
                  {row.reportUrl ? (
                    <a
                      href={row.reportUrl}
                      target="_blank"
                      rel="noreferrer"
                      className="inline-flex items-center gap-1 rounded px-1.5 py-1 text-[10px] text-accent hover:bg-accent-soft"
                      title="Open DCF report"
                    >
                      PDF <ExternalLink size={10} />
                    </a>
                  ) : (
                    <button
                      type="button"
                      onClick={() => onSelectRun(row.run)}
                      className="rounded px-1.5 py-1 text-[10px] text-ink-dim hover:bg-surface-2 hover:text-ink-muted"
                    >
                      View
                    </button>
                  )}
                </span>
              </div>
            )
          })}
        </div>
      </div>
    </Panel>
  )
}
