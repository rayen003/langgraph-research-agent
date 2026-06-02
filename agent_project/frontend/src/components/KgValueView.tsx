import { useState, type ReactNode } from 'react'

/**
 * Structured, progressive-disclosure renderer for KG node values.
 *
 * Replaces the raw JSON dump with typed, colored cards:
 *  - Categorical status fields (lifecycle_stage, margin_trajectory, …) render
 *    as colored chips.
 *  - Long prose fields render as bordered cards with a label header.
 *  - Inline source refs (`ev_web_1_evb_1780095889780`) are extracted and
 *    rendered as timestamped source chips.
 *  - Known semantic shapes (thesis bull/bear, key_drivers, news) get bespoke
 *    cards; unknown objects/arrays fall back to collapsible sections.
 */

// ── Direction / conviction / sentiment color tokens ───────────────────────────
const DIRECTION_STYLE: Record<string, { dot: string; text: string }> = {
  positive: { dot: 'bg-emerald-400', text: 'text-emerald-300' },
  negative: { dot: 'bg-rose-400', text: 'text-rose-300' },
  neutral: { dot: 'bg-zinc-400', text: 'text-ink-muted' },
  bullish: { dot: 'bg-emerald-400', text: 'text-emerald-300' },
  bearish: { dot: 'bg-rose-400', text: 'text-rose-300' },
}

const CONVICTION_STYLE: Record<string, string> = {
  high: 'bg-amber-500/20 text-amber-300 border-amber-500/40',
  medium: 'bg-sky-500/15 text-sky-300 border-sky-500/30',
  low: 'bg-zinc-700/40 text-ink-muted border-zinc-600/40',
}

// Categorical status vocab → chip color. Keyed by lowercased value.
const STATUS_STYLE: Record<string, string> = {
  // lifecycle / growth posture
  scaling: 'bg-sky-500/15 text-sky-300 border-sky-500/30',
  growth: 'bg-emerald-500/15 text-emerald-300 border-emerald-500/30',
  mature: 'bg-zinc-600/30 text-ink-muted border-zinc-500/40',
  declining: 'bg-rose-500/15 text-rose-300 border-rose-500/30',
  emerging: 'bg-violet-500/15 text-violet-300 border-violet-500/30',
  // trajectory / trend
  improving: 'bg-emerald-500/15 text-emerald-300 border-emerald-500/30',
  stable: 'bg-sky-500/15 text-sky-300 border-sky-500/30',
  deteriorating: 'bg-rose-500/15 text-rose-300 border-rose-500/30',
  expanding: 'bg-emerald-500/15 text-emerald-300 border-emerald-500/30',
  compressing: 'bg-rose-500/15 text-rose-300 border-rose-500/30',
  // intensity
  low: 'bg-emerald-500/15 text-emerald-300 border-emerald-500/30',
  moderate: 'bg-amber-500/15 text-amber-300 border-amber-500/30',
  high: 'bg-rose-500/15 text-rose-300 border-rose-500/30',
}

// Fields whose scalar value is a short categorical status → render as chip.
const CATEGORICAL_KEYS = new Set([
  'lifecycle_stage', 'margin_trajectory', 'margin_trend', 'sbc_intensity',
  'sentiment', 'direction', 'conviction', 'stage', 'trend', 'trajectory',
])

function prettifyKey(k: string): string {
  return k.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase())
}

// ── Source-ref extraction ─────────────────────────────────────────────────────
// Refs look like "ev_web_1_evb_1780095889780" or "(evb_1780095889780)".
// The trailing digit run is an epoch timestamp (ms if 13 digits, else s).
interface SourceRef { kind: string; ts: number | null; raw: string }

const SOURCE_RE = /\(?\b(ev_([a-z]+)_\d+_)?evb_(\d{10,13})\)?/gi

function parseSources(text: string): { clean: string; sources: SourceRef[] } {
  const sources: SourceRef[] = []
  const clean = text.replace(SOURCE_RE, (m, _pre, kind, digits) => {
    const n = Number(digits)
    const ts = digits.length >= 13 ? n : n * 1000
    sources.push({ kind: kind || 'source', ts: isFinite(ts) ? ts : null, raw: m })
    return ''
  }).replace(/\s+([.,;])/g, '$1').replace(/\(\s*\)/g, '').replace(/\s{2,}/g, ' ').trim()
  return { clean, sources }
}

function fmtDate(ts: number | null): string {
  if (!ts) return ''
  const d = new Date(ts)
  if (isNaN(d.getTime())) return ''
  return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' })
}

const KIND_ICON: Record<string, string> = {
  web: '🌐', filing: '📄', news: '📰', source: '🔗',
}

function SourceChips({ sources }: { sources: SourceRef[] }) {
  if (!sources.length) return null
  // Dedupe by kind+ts
  const seen = new Set<string>()
  const uniq = sources.filter(s => {
    const k = `${s.kind}:${s.ts}`
    if (seen.has(k)) return false
    seen.add(k)
    return true
  })
  return (
    <div className="flex flex-wrap gap-1 mt-1.5">
      {uniq.map((s, i) => (
        <span
          key={i}
          className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded-full bg-surface-2 border border-edge text-[9px] text-ink-muted"
          title={s.raw}
        >
          <span>{KIND_ICON[s.kind] || KIND_ICON.source}</span>
          <span className="capitalize">{s.kind}</span>
          {s.ts && <span className="text-ink-dim">· {fmtDate(s.ts)}</span>}
        </span>
      ))}
    </div>
  )
}

// ── Text with extracted sources (prose body) ──────────────────────────────────
function SourcedText({ text, className = '' }: { text: string; className?: string }) {
  const { clean, sources } = parseSources(text)
  return (
    <div>
      <div className={`leading-relaxed break-words ${className}`}>{clean}</div>
      <SourceChips sources={sources} />
    </div>
  )
}

// ── Categorical status chip ───────────────────────────────────────────────────
function StatusChip({ value }: { value: string }) {
  // First token may carry the status; keep qualifier text (e.g. "moderate (3% …)").
  const head = value.trim().split(/[\s(]/)[0].toLowerCase()
  const style = STATUS_STYLE[head] || 'bg-zinc-700/40 text-ink-muted border-zinc-600/40'
  const tail = value.trim().slice(head.length).trim()
  return (
    <span className="inline-flex items-center gap-1.5">
      <span className={`px-2 py-0.5 rounded-full text-[10px] border font-medium capitalize ${style}`}>
        {head}
      </span>
      {tail && <span className="text-[10px] text-ink-dim">{tail.replace(/^[()]|[()]$/g, '')}</span>}
    </span>
  )
}

// ── Collapsible section ──────────────────────────────────────────────────────
function Section({
  title, defaultOpen = false, accent = 'text-ink-muted', children,
}: {
  title: string; defaultOpen?: boolean; accent?: string; children: ReactNode
}) {
  const [open, setOpen] = useState(defaultOpen)
  return (
    <div className="rounded border border-edge bg-surface-2 overflow-hidden">
      <button
        onClick={() => setOpen(o => !o)}
        className="w-full flex items-center gap-1.5 px-2 py-1.5 text-left hover:bg-surface-2 transition"
      >
        <span className={`text-[9px] ${open ? 'rotate-90' : ''} transition-transform text-ink-dim`}>▶</span>
        <span className={`text-[10px] uppercase tracking-wider font-medium ${accent}`}>{title}</span>
      </button>
      {open && <div className="px-2 pb-2 space-y-1.5">{children}</div>}
    </div>
  )
}

// ── Field renderer: picks chip / prose-card / inline scalar ───────────────────
function Field({ k, v }: { k: string; v: unknown }) {
  const label = prettifyKey(k)

  // Categorical status → chip
  if (typeof v === 'string' && CATEGORICAL_KEYS.has(k.toLowerCase()) && v.length < 80) {
    return (
      <div className="flex items-center gap-2 px-2 py-1.5">
        <span className="text-[10px] text-ink-dim flex-shrink-0 w-32">{label}</span>
        <StatusChip value={v} />
      </div>
    )
  }

  // Long prose → bordered card with header + extracted sources
  if (typeof v === 'string' && v.length > 90) {
    return (
      <div className="rounded border border-edge bg-surface-2 border-l-2 border-l-accent/40 px-2.5 py-2">
        <div className="text-[9px] uppercase tracking-wider text-ink-dim font-medium mb-1">{label}</div>
        <SourcedText text={v} className="text-ink-muted text-[11px]" />
      </div>
    )
  }

  // Short scalar → inline row
  if (v === null || v === undefined || typeof v !== 'object') {
    return (
      <div className="flex gap-2 px-2 py-1">
        <span className="text-[10px] text-ink-dim flex-shrink-0">{label}</span>
        <span className="text-[10px] text-ink break-words text-right ml-auto">
          {v === null || v === undefined ? '—' : String(v)}
        </span>
      </div>
    )
  }

  // Array of primitive strings (often source-laden bullets) → bulleted card
  if (Array.isArray(v)) {
    if (v.length === 0) {
      return (
        <div className="flex gap-2 px-2 py-1">
          <span className="text-[10px] text-ink-dim">{label}</span>
          <span className="text-[10px] text-ink-dim ml-auto">(empty)</span>
        </div>
      )
    }
    const allStr = v.every(x => typeof x === 'string')
    if (allStr) {
      return (
        <Section title={`${label} (${v.length})`} accent="text-accent" defaultOpen>
          {(v as string[]).map((item, i) => (
            <div key={i} className="flex gap-1.5 px-2 py-1.5 rounded bg-surface border border-edge">
              <span className="text-accent text-[10px] flex-shrink-0">•</span>
              <SourcedText text={item} className="text-ink-muted text-[11px] flex-1" />
            </div>
          ))}
        </Section>
      )
    }
    return (
      <Section title={`${label} (${v.length})`} accent="text-accent">
        {(v as unknown[]).map((item, i) => (
          <div key={i} className="rounded bg-surface border border-edge p-1.5">
            <ValueBody v={item} />
          </div>
        ))}
      </Section>
    )
  }

  // Nested object → collapsible section
  return (
    <Section title={label}>
      {Object.keys(v as Record<string, unknown>).map(ck => (
        <Field key={ck} k={ck} v={(v as Record<string, unknown>)[ck]} />
      ))}
    </Section>
  )
}

// ── Thesis card (bull / bear / drivers) ───────────────────────────────────────
function DriverRow({ d }: { d: Record<string, unknown> }) {
  const dir = String(d.direction || 'neutral').toLowerCase()
  const conv = String(d.conviction || '').toLowerCase()
  const ds = DIRECTION_STYLE[dir] || DIRECTION_STYLE.neutral
  const cs = CONVICTION_STYLE[conv]
  return (
    <div className="flex items-start gap-2 px-2 py-1.5 rounded bg-surface border border-edge">
      <span className={`mt-1 inline-block w-1.5 h-1.5 rounded-full flex-shrink-0 ${ds.dot}`} />
      <div className="flex-1 min-w-0">
        <SourcedText text={String(d.driver ?? d.label ?? '—')} className={`${ds.text} text-[11px]`} />
        {conv && (
          <span className={`inline-block mt-1 px-1.5 py-0.5 rounded text-[9px] border ${cs}`}>
            {conv} conviction
          </span>
        )}
      </div>
    </div>
  )
}

function ThesisCard({ v }: { v: Record<string, unknown> }) {
  const bull = v.bull_thesis as string | undefined
  const bear = v.bear_thesis as string | undefined
  const drivers = Array.isArray(v.key_drivers) ? (v.key_drivers as Record<string, unknown>[]) : []
  const rest = Object.keys(v).filter(k => !['bull_thesis', 'bear_thesis', 'key_drivers'].includes(k))
  return (
    <div className="space-y-2">
      {bull && (
        <div className="rounded border border-emerald-500/25 bg-emerald-500/[0.06] px-2.5 py-2">
          <div className="text-[9px] uppercase tracking-wider text-emerald-400 font-medium mb-1">▲ Bull</div>
          <SourcedText text={bull} className="text-ink-muted text-[11px]" />
        </div>
      )}
      {bear && (
        <div className="rounded border border-rose-500/25 bg-rose-500/[0.06] px-2.5 py-2">
          <div className="text-[9px] uppercase tracking-wider text-rose-400 font-medium mb-1">▼ Bear</div>
          <SourcedText text={bear} className="text-ink-muted text-[11px]" />
        </div>
      )}
      {drivers.length > 0 && (
        <Section title={`Key Drivers (${drivers.length})`} accent="text-accent" defaultOpen>
          {drivers.map((d, i) => <DriverRow key={i} d={d} />)}
        </Section>
      )}
      {rest.length > 0 && (
        <div className="space-y-1.5">
          {rest.map(k => <Field key={k} k={k} v={(v as Record<string, unknown>)[k]} />)}
        </div>
      )}
    </div>
  )
}

// ── News item card ────────────────────────────────────────────────────────────
function NewsCard({ v }: { v: Record<string, unknown> }) {
  const sentiment = String(v.sentiment || '').toLowerCase()
  const ss = DIRECTION_STYLE[sentiment] || DIRECTION_STYLE.neutral
  const url = v.url as string | undefined
  const ts = v.published_at ?? v.date
  return (
    <div className="rounded border border-edge bg-surface-2 px-2.5 py-2">
      {sentiment && (
        <span className={`inline-flex items-center gap-1 text-[9px] ${ss.text} mb-1`}>
          <span className={`inline-block w-1.5 h-1.5 rounded-full ${ss.dot}`} />
          {sentiment}
        </span>
      )}
      <div className="text-ink text-[11px] font-medium leading-snug">
        {String(v.headline ?? v.title ?? '—')}
      </div>
      {Boolean(v.summary) && (
        <SourcedText text={String(v.summary)} className="text-ink-muted text-[10px] mt-1" />
      )}
      <div className="flex items-center gap-2 mt-1.5 text-[9px] text-ink-dim">
        {Boolean(v.source) && <span>{String(v.source)}</span>}
        {Boolean(ts) && <span>· {String(ts)}</span>}
        {url && (
          <a href={url} target="_blank" rel="noreferrer" className="text-sky-400 hover:underline ml-auto">
            open ↗
          </a>
        )}
      </div>
    </div>
  )
}

// ── Document fact card ──────────────────────────────────────────────────────
const FACT_TYPE_META: Record<string, { label: string; style: string; icon: string }> = {
  guidance:          { label: 'Guidance',         style: 'text-sky-400 bg-sky-500/10 border-sky-500/30',         icon: '📅' },
  risk_factor:       { label: 'Risk',             style: 'text-amber-400 bg-amber-500/10 border-amber-500/30',   icon: '⚠️' },
  competitive_moat:  { label: 'Moat',             style: 'text-emerald-400 bg-emerald-500/10 border-emerald-500/30', icon: '🏰' },
  capital_allocation:{ label: 'CapAlloc',         style: 'text-violet-400 bg-violet-500/10 border-violet-500/30', icon: '💸' },
  revenue:           { label: 'Revenue',          style: 'text-indigo-400 bg-indigo-500/10 border-indigo-500/30', icon: '💰' },
  net_income:        { label: 'Net Income',       style: 'text-emerald-400 bg-emerald-500/10 border-emerald-500/30', icon: '💵' },
  operating_income:  { label: 'Op. Income',       style: 'text-indigo-400 bg-indigo-500/10 border-indigo-500/30', icon: '🏭' },
  gross_profit:      { label: 'Gross Profit',     style: 'text-indigo-400 bg-indigo-500/10 border-indigo-500/30', icon: '📐' },
  eps:               { label: 'EPS',              style: 'text-sky-400 bg-sky-500/10 border-sky-500/30',         icon: '🪙' },
  shares_outstanding:{ label: 'Shares',           style: 'text-zinc-400 bg-zinc-500/10 border-zinc-500/30',      icon: '📦' },
  fcff_margin:       { label: 'FCFF Margin',      style: 'text-indigo-400 bg-indigo-500/10 border-indigo-500/30', icon: '📊' },
  growth_rate:       { label: 'Growth',           style: 'text-emerald-400 bg-emerald-500/10 border-emerald-500/30', icon: '📈' },
  margin:            { label: 'Margin',           style: 'text-indigo-400 bg-indigo-500/10 border-indigo-500/30', icon: '📊' },
  wacc_signal:       { label: 'WACC Signal',      style: 'text-violet-400 bg-violet-500/10 border-violet-500/30', icon: '⚖️' },
  debt_metric:       { label: 'Debt',             style: 'text-rose-400 bg-rose-500/10 border-rose-500/30',       icon: '🏦' },
  valuation_metric:  { label: 'Valuation',        style: 'text-amber-400 bg-amber-500/10 border-amber-500/30',   icon: '🎯' },
  effective_tax_rate:{ label: 'Tax Rate',         style: 'text-zinc-400 bg-zinc-500/10 border-zinc-500/30',      icon: '🧾' },
}

function DocumentFactCard({ v, nodeType }: { v: Record<string, unknown>; nodeType?: string }) {
  const numeric = typeof v.value === 'number' ? v.value : null
  const text = typeof v.text === 'string' ? v.text : ''
  const period = typeof v.as_of === 'string' ? v.as_of : ''
  const conf = typeof v.confidence === 'number' ? v.confidence : null
  const filename = typeof v.source_filename === 'string' ? v.source_filename : ''
  const page = v.source_page
  // fact_type stored in value dict by extract_and_ingest_facts; fall back to nodeType
  const factType = (typeof v.fact_type === 'string' ? v.fact_type : nodeType) || ''
  const typeMeta = FACT_TYPE_META[factType]
  const confBadge = !conf ? '' : conf >= 0.9
    ? 'text-emerald-400 bg-emerald-500/10 border-emerald-500/30'
    : conf >= 0.7
      ? 'text-amber-400 bg-amber-500/10 border-amber-500/30'
      : 'text-rose-400 bg-rose-500/10 border-rose-500/30'
  return (
    <div className="rounded border border-edge bg-surface-2 px-2.5 py-2">
      <div className="flex items-center justify-between gap-2 mb-1">
        {typeMeta ? (
          <span className={`inline-flex items-center gap-1 text-[9px] rounded px-1.5 py-0.5 border font-medium ${typeMeta.style}`}>
            <span>{typeMeta.icon}</span>{typeMeta.label}
          </span>
        ) : factType && factType !== 'other' ? (
          <span className="text-[9px] text-ink-dim bg-surface border border-edge rounded px-1.5 py-0.5 capitalize">
            {factType.replace(/_/g, ' ')}
          </span>
        ) : null}
        <div className="flex items-baseline gap-1.5 ml-auto">
          {period && (
            <span className="text-[9px] text-ink-dim bg-surface border border-edge rounded px-1">{period}</span>
          )}
          {conf !== null && (
            <span className={`text-[9px] rounded px-1 border ${confBadge}`}>{Math.round(conf * 100)}%</span>
          )}
        </div>
      </div>
      {numeric !== null && (
        <div className="text-ink text-[13px] font-mono font-semibold tracking-tight mb-1">
          {Math.abs(numeric) >= 1 ? numeric.toLocaleString(undefined, { maximumFractionDigits: 2 }) : String(numeric)}
        </div>
      )}
      {text && <div className="text-ink-muted text-[11px] leading-snug">{text}</div>}
      <div className="flex items-center gap-2 mt-1.5 text-[9px] text-ink-dim">
        {filename && <span className="truncate max-w-[200px]">📎 {filename}</span>}
        {page !== null && page !== undefined && <span>p.{String(page)}</span>}
      </div>
    </div>
  )
}

// ── Filing card ───────────────────────────────────────────────────────────────
function FilingCard({ v }: { v: Record<string, unknown> }) {
  const ftype = String(v.filing_type || 'sec_filing')
  const period = typeof v.fiscal_period === 'string' ? v.fiscal_period
    : typeof v.as_of === 'string' ? v.as_of : ''
  const filename = typeof v.filename === 'string' ? v.filename : ''
  const chunks = typeof v.chunk_count === 'number' ? v.chunk_count : null
  const pages = typeof v.page_count === 'number' ? v.page_count : null
  const text = typeof v.text === 'string' ? v.text : ''
  const docId = typeof v.source_doc_id === 'string' ? v.source_doc_id : ''
  // SEC-fetched filings carry a public url; uploaded filings carry source_doc_id
  // → open via the document file endpoint.
  const url = typeof v.url === 'string' && v.url
    ? v.url
    : docId ? `/documents/${encodeURIComponent(docId)}/file` : ''
  const typeLabel: Record<string, string> = { sec_filing: 'SEC Filing', annual_report: 'Annual Report' }
  return (
    <div className="rounded border border-indigo-500/25 bg-indigo-500/[0.06] px-2.5 py-2">
      <div className="flex items-center gap-2">
        <span className="text-[9px] uppercase tracking-wider text-indigo-400 font-medium">📄 {typeLabel[ftype] || ftype}</span>
        {period && <span className="text-[9px] text-ink-dim bg-surface border border-edge rounded px-1">{period}</span>}
      </div>
      {filename && <div className="text-ink-muted text-[10px] mt-1 truncate">{filename}</div>}
      {/* Short lead snippet so the card isn't empty; full content is one click away. */}
      {text && (
        <div className="mt-1.5 text-ink-dim text-[10px] leading-snug max-h-20 overflow-hidden border-l-2 border-l-indigo-500/40 pl-2">
          {text}
        </div>
      )}
      <div className="flex items-center gap-3 mt-1.5 text-[9px] text-ink-dim">
        {chunks !== null && <span>{chunks} chunk{chunks !== 1 ? 's' : ''}</span>}
        {pages !== null && <span>{pages} page{pages !== 1 ? 's' : ''}</span>}
        {url && (
          <a href={url} target="_blank" rel="noreferrer" className="text-sky-400 hover:underline ml-auto font-medium">
            Open document ↗
          </a>
        )}
      </div>
    </div>
  )
}

// Node types that carry doc-extraction value shape → DocumentFactCard
const DOC_FACT_NODE_TYPES = new Set([
  'document_fact', 'guidance', 'risk_factor', 'competitive_moat', 'capital_allocation',
])

// ── Body dispatcher ───────────────────────────────────────────────────────────
function ValueBody({ v, nodeType }: { v: unknown; nodeType?: string }) {
  if (v === null || v === undefined) return <div className="text-ink-dim text-[11px]">—</div>
  if (typeof v === 'string') {
    return <SourcedText text={v} className="text-ink-muted text-[11px]" />
  }
  if (typeof v === 'number' || typeof v === 'boolean') {
    return <div className="text-ink-muted text-[11px]">{String(v)}</div>
  }
  if (Array.isArray(v)) {
    return <Field k="items" v={v} />
  }
  const obj = v as Record<string, unknown>
  if ('bull_thesis' in obj || 'bear_thesis' in obj || 'key_drivers' in obj) {
    return <ThesisCard v={obj} />
  }
  if (nodeType === 'news_item' || 'headline' in obj || ('sentiment' in obj && ('summary' in obj || 'url' in obj))) {
    return <NewsCard v={obj} />
  }
  // Filing → bespoke card
  if (nodeType === 'filing' && typeof v === 'object' && v !== null && !Array.isArray(v) && 'filing_type' in obj) {
    return <FilingCard v={obj} />
  }
  // Document fact: explicit node type OR mapped semantic types that carry doc-extraction shape
  if (
    nodeType && DOC_FACT_NODE_TYPES.has(nodeType) &&
    ('source_filename' in obj || 'fact_type' in obj || 'source_doc_id' in obj)
  ) {
    return <DocumentFactCard v={obj} nodeType={nodeType} />
  }
  // Generic object → field list (ticker pinned first if present)
  const keys = Object.keys(obj)
  const ordered = keys.includes('ticker') ? ['ticker', ...keys.filter(k => k !== 'ticker')] : keys
  return (
    <div className="space-y-1.5">
      {ordered.map(k => <Field key={k} k={k} v={obj[k]} />)}
    </div>
  )
}

export function KgValueView({ value, nodeType }: { value: unknown; nodeType?: string }) {
  return <ValueBody v={value} nodeType={nodeType} />
}
