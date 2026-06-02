import { useState, useCallback } from 'react'
import { ShieldAlert, Play, RotateCw, Filter, ChevronDown, ChevronUp, X, AlertCircle, AlertTriangle, Info } from 'lucide-react'

/** Shape of a single audit finding from the backend. */
interface AuditFinding {
  audit_id: string
  timestamp: string
  check_type: string
  ticker: string
  node_type: string
  field: string
  severity: 'info' | 'warning' | 'error'
  finding: string
  recommendation: string
  source_tier: string | null
  existing_value: string | null
  conflicting_value: string | null
  auto_fixed: boolean
}

interface AuditResponse {
  total_findings: number
  by_severity: Record<string, number>
  by_check: Record<string, number>
  findings: AuditFinding[]
}

interface Props {
  /** Currently selected ticker from the KG panel context (used as default) */
  ticker?: string
  /** All tickers present in the graph — user picks which to audit. */
  availableTickers?: string[]
  onClose: () => void
}

const CHECK_LABELS: Record<string, string> = {
  cross_source: 'Cross-source consistency',
  staleness: 'Staleness detection',
  orphan: 'Orphan detection',
  entity_coherence: 'Entity coherence',
  hallucination: 'Hallucination spot-check (LLM)',
}

const SEVERITY_ICON: Record<string, React.ReactNode> = {
  error: <AlertCircle size={14} className="text-red-400 shrink-0" />,
  warning: <AlertTriangle size={14} className="text-yellow-400 shrink-0" />,
  info: <Info size={14} className="text-sky-400 shrink-0" />,
}

export function KgAuditPanel({ ticker, availableTickers = [], onClose }: Props) {
  const [busy, setBusy] = useState(false)
  const [result, setResult] = useState<AuditResponse | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [expanded, setExpanded] = useState<Set<string>>(new Set())
  const [severityFilter, setSeverityFilter] = useState<Set<string>>(new Set())
  const [selectedChecks, setSelectedChecks] = useState<Set<string>>(
    new Set(['cross_source', 'staleness', 'orphan', 'entity_coherence']),
  )
  // Ticker selection — default to all available (or the context ticker if the
  // panel was opened scoped to one). Empty selection = audit the whole graph.
  const [selectedTickers, setSelectedTickers] = useState<Set<string>>(
    () => new Set(ticker ? [ticker] : availableTickers),
  )

  const toggleCheck = (ck: string) => {
    setSelectedChecks(prev => {
      const next = new Set(prev)
      if (next.has(ck)) next.delete(ck); else next.add(ck)
      return next
    })
  }

  const toggleTicker = (t: string) => {
    setSelectedTickers(prev => {
      const next = new Set(prev)
      if (next.has(t)) next.delete(t); else next.add(t)
      return next
    })
  }
  const allTickersSelected = availableTickers.length > 0 && selectedTickers.size === availableTickers.length

  const runAudit = useCallback(async () => {
    setBusy(true)
    setError(null)
    setResult(null)
    try {
      const res = await fetch('/kg/audit', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          // Selecting all (or none available) → audit whole graph; a subset →
          // send the explicit ticker list.
          tickers: allTickersSelected ? null : [...selectedTickers],
          checks: [...selectedChecks],
          sample_size: 5,
          auto_fix: true,
        }),
      })
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      setResult((await res.json()) as AuditResponse)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }, [selectedTickers, allTickersSelected, selectedChecks])

  const toggleExpand = (id: string) => {
    setExpanded(prev => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id); else next.add(id)
      return next
    })
  }

  const toggleSeverityFilter = (sev: string) => {
    setSeverityFilter(prev => {
      const next = new Set(prev)
      if (next.has(sev)) next.delete(sev); else next.add(sev)
      return next
    })
  }

  const filtered = result
    ? result.findings.filter(f => severityFilter.size === 0 || severityFilter.has(f.severity))
    : []

  return (
    <div className="flex flex-col h-full text-xs bg-bg-overlay border-l border-border-accent">
      {/* Header */}
      <div className="flex items-center justify-between px-3 py-2 border-b border-border-accent shrink-0">
        <div className="flex items-center gap-2 text-ink-muted">
          <ShieldAlert size={14} />
          <span className="font-medium">KG Audit</span>
        </div>
        <button onClick={onClose} className="text-ink-dim hover:text-ink-muted">
          <X size={14} />
        </button>
      </div>

      {/* Config section */}
      <div className="p-3 border-b border-border-accent space-y-3 shrink-0">
        {/* Ticker selection */}
        {availableTickers.length > 0 && (
          <div className="space-y-1">
            <div className="flex items-center justify-between mb-1">
              <div className="text-[10px] uppercase text-ink-dim tracking-wider">Tickers to audit</div>
              <button
                onClick={() => setSelectedTickers(allTickersSelected ? new Set() : new Set(availableTickers))}
                className="text-[10px] text-ink-dim hover:text-ink-muted"
              >
                {allTickersSelected ? 'clear' : 'all'}
              </button>
            </div>
            <div className="flex flex-wrap gap-1">
              {availableTickers.map(t => {
                const on = selectedTickers.has(t)
                return (
                  <button
                    key={t}
                    onClick={() => toggleTicker(t)}
                    className={`px-2 py-0.5 rounded-full text-[10px] border font-mono transition-colors ${
                      on
                        ? 'bg-emerald-700/30 text-emerald-400 border-emerald-700/50'
                        : 'bg-transparent text-ink-dim border-edge hover:text-ink-muted'
                    }`}
                  >
                    {t}
                  </button>
                )
              })}
            </div>
            <div className="text-[9px] text-ink-dim">
              {allTickersSelected || selectedTickers.size === 0
                ? 'Auditing all tickers'
                : `Auditing ${selectedTickers.size} of ${availableTickers.length}`}
            </div>
          </div>
        )}

        {/* Check type toggles */}
        <div className="space-y-1">
          <div className="text-[10px] uppercase text-ink-dim tracking-wider mb-1">Checks to run</div>
          {Object.entries(CHECK_LABELS).map(([ck, label]) => (
            <label key={ck} className="flex items-center gap-2 cursor-pointer text-ink-muted hover:text-ink-default">
              <input
                type="checkbox"
                className="accent-emerald-600"
                checked={selectedChecks.has(ck)}
                onChange={() => toggleCheck(ck)}
              />
              <span>{label}</span>
            </label>
          ))}
        </div>

        {/* Run button */}
        <button
          onClick={runAudit}
          disabled={busy || selectedChecks.size === 0}
          className="w-full flex items-center justify-center gap-2 px-3 py-1.5 rounded bg-emerald-700/30 text-emerald-400 border border-emerald-700/50 hover:bg-emerald-700/40 disabled:opacity-40 transition-colors"
        >
          {busy ? (
            <RotateCw size={14} className="animate-spin" />
          ) : (
            <Play size={14} />
          )}
          {busy ? 'Running...' : 'Run Audit'}
        </button>

        {error && (
          <div className="text-red-400 text-[11px] bg-red-900/20 rounded px-2 py-1">{error}</div>
        )}
      </div>

      {/* Summary */}
      {result && (
        <div className="px-3 py-2 border-b border-border-accent space-y-1.5 shrink-0">
          <div className="flex items-center justify-between text-ink-muted">
            <span className="font-medium">{result.total_findings} findings</span>
            <div className="flex gap-2">
              {(['error', 'warning', 'info'] as const).map(sev => {
                const count = result.by_severity[sev]
                if (!count) return null
                const active = severityFilter.size === 0 || severityFilter.has(sev)
                return (
                  <button
                    key={sev}
                    onClick={() => toggleSeverityFilter(sev)}
                    className={`flex items-center gap-1 px-1.5 py-0.5 rounded-full text-[10px] border transition-colors ${
                      active
                        ? sev === 'error'
                          ? 'bg-red-900/30 border-red-700/40 text-red-400'
                          : sev === 'warning'
                            ? 'bg-yellow-900/30 border-yellow-700/40 text-yellow-400'
                            : 'bg-sky-900/30 border-sky-700/40 text-sky-400'
                        : 'border-transparent text-ink-dim'
                    }`}
                  >
                    {sev} ({count})
                  </button>
                )
              })}
            </div>
          </div>
          {/* By check type */}
          <div className="flex gap-2 flex-wrap">
            {Object.entries(result.by_check).map(([ck, count]) => (
              <span key={ck} className="text-[10px] text-ink-dim bg-bg-muted rounded px-1.5 py-0.5">
                {CHECK_LABELS[ck] || ck}: {count}
              </span>
            ))}
          </div>
        </div>
      )}

      {/* Findings list */}
      <div className="flex-1 overflow-y-auto p-3 space-y-2">
        {!result && !error && (
          <div className="text-ink-dim text-center py-8">
            Select checks and ticker, then click Run Audit to analyze KG quality.
          </div>
        )}

        {result && filtered.length === 0 && (
          <div className="text-emerald-400 text-center py-8">
            No findings match the current filters.
          </div>
        )}

        {filtered.map((finding) => {
          const isOpen = expanded.has(finding.audit_id)
          return (
            <div
              key={finding.audit_id}
              className={`rounded border text-[11px] overflow-hidden ${
                finding.severity === 'error'
                  ? 'border-red-800/40 bg-red-900/10'
                  : finding.severity === 'warning'
                    ? 'border-yellow-800/40 bg-yellow-900/10'
                    : 'border-sky-800/40 bg-sky-900/10'
              }`}
            >
              {/* Row */}
              <button
                onClick={() => toggleExpand(finding.audit_id)}
                className="w-full flex items-start gap-2 px-2.5 py-2 text-left hover:bg-white/5 transition-colors"
              >
                {SEVERITY_ICON[finding.severity] || null}
                <div className="flex-1 min-w-0">
                  <div className="text-ink-muted leading-snug">{finding.finding}</div>
                  <div className="flex items-center gap-2 mt-1 text-ink-dim text-[10px]">
                    <span>{finding.check_type}</span>
                    <span className="opacity-30">|</span>
                    <span className="font-mono">
                      {finding.ticker}::{finding.node_type}/{finding.field}
                    </span>
                    {finding.auto_fixed && (
                      <span className="text-emerald-400">(auto-fixed)</span>
                    )}
                  </div>
                </div>
                {isOpen ? <ChevronUp size={12} className="text-ink-dim shrink-0 mt-0.5" /> : <ChevronDown size={12} className="text-ink-dim shrink-0 mt-0.5" />}
              </button>

              {/* Expanded detail */}
              {isOpen && (
                <div className="px-2.5 pb-2 space-y-1.5 text-ink-dim text-[10px] border-t border-white/5 pt-2">
                  {finding.source_tier && (
                    <div>
                      <span className="text-ink-faint">Source tier:</span>{' '}
                      <span className="text-ink-muted">{finding.source_tier}</span>
                    </div>
                  )}
                  {finding.existing_value && (
                    <div>
                      <span className="text-ink-faint">Existing:</span>{' '}
                      <span className="text-ink-muted font-mono">{finding.existing_value}</span>
                    </div>
                  )}
                  {finding.conflicting_value && (
                    <div>
                      <span className="text-ink-faint">Conflicting:</span>{' '}
                      <span className="text-ink-muted font-mono">{finding.conflicting_value}</span>
                    </div>
                  )}
                  {finding.recommendation && (
                    <div className="text-emerald-400/80">
                      → {finding.recommendation}
                    </div>
                  )}
                  <div className="text-ink-faint pt-0.5">
                    {new Date(finding.timestamp).toLocaleString()}
                  </div>
                </div>
              )}
            </div>
          )
        })}
      </div>
    </div>
  )
}
