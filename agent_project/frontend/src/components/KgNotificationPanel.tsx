import { useState, useEffect, useRef } from 'react'
import type { KgNode } from '../hooks/useKnowledgeGraph'
import { X } from 'lucide-react'

/** Category grouping with color tokens. */
type ChangeCategory = 'dcf' | 'financial' | 'document_fact' | 'news' | 'user' | 'filing' | 'risk' | 'other'

interface CategoryMeta {
  label: string
  border: string  // left border color class
  dot: string     // dot color class
  bg: string      // subtle tinted background
}

const CAT_META: Record<ChangeCategory, CategoryMeta> = {
  dcf:           { label: 'DCF',           border: 'border-l-emerald-500', dot: 'bg-emerald-400', bg: 'bg-emerald-500/[0.06]' },
  financial:     { label: 'Financial',     border: 'border-l-sky-500',     dot: 'bg-sky-400',     bg: 'bg-sky-500/[0.06]' },
  document_fact: { label: 'Doc Extract',   border: 'border-l-amber-500',   dot: 'bg-amber-400',   bg: 'bg-amber-500/[0.06]' },
  news:          { label: 'News',          border: 'border-l-violet-500',  dot: 'bg-violet-400',  bg: 'bg-violet-500/[0.06]' },
  user:          { label: 'User Edit',     border: 'border-l-rose-500',    dot: 'bg-rose-400',    bg: 'bg-rose-500/[0.06]' },
  filing:        { label: 'Filing',        border: 'border-l-indigo-500',  dot: 'bg-indigo-400',  bg: 'bg-indigo-500/[0.06]' },
  risk:          { label: 'Risk',          border: 'border-l-red-500',     dot: 'bg-red-400',     bg: 'bg-red-500/[0.06]' },
  other:         { label: 'KG Update',     border: 'border-l-zinc-500',    dot: 'bg-zinc-400',    bg: 'bg-zinc-500/[0.04]' },
}

function classifyNode(n: KgNode): ChangeCategory {
  const t = n.node_type
  if (t === 'dcf_run' || t === 'run_assumption' || t === 'run_output' || t === 'scenario_result' || t === 'valuation_result') return 'dcf'
  if (t === 'financials_hub' || t === 'structured_fundamental' || t === 'market_data' || t === 'profile' || t === 'company' || t.startsWith('market_metric')) return 'financial'
  if (t === 'capital_allocation') return 'financial'
  if (t === 'document_fact' || t === 'key_fact' || t === 'snippet_fact') return n.field === 'risk_factors' || n.field === 'competitive_moat' ? 'risk' : 'document_fact'
  if (t === 'guidance' || t === 'competitive_moat') return 'document_fact'
  if (t === 'risk_factor') return 'risk'
  if (t === 'news_item' || t === 'web_excerpt') return 'news'
  if (t === 'user_belief' || t === 'user_stated' || t === 'user_override') return 'user'
  if (t === 'filing' || t === 'filing_excerpt') return 'filing'
  if (t === 'driver' || t === 'risk' || t === 'theme') return 'risk'
  return 'other'
}

interface NotificationCard {
  cat: ChangeCategory
  count: number
  ticker: string
  sample: string
  at: number
}

function aggregateNotifications(newNodes: KgNode[], now: number): NotificationCard[] {
  const byCat = new Map<ChangeCategory, { count: number; ticker: string; sample: string }>()
  for (const n of newNodes) {
    const cat = classifyNode(n)
    const entry = byCat.get(cat) ?? { count: 0, ticker: n.ticker, sample: '' }
    entry.count++
    if (!entry.sample) {
      if (n.value && typeof n.value === 'object') {
        const obj = n.value as Record<string, unknown>
        const t = obj.text ?? obj.label ?? obj.driver ?? obj.headline ?? ''
        entry.sample = typeof t === 'string' ? t.slice(0, 55) : n.field.replace(/_/g, ' ')
      } else {
        entry.sample = n.field.replace(/_/g, ' ')
      }
    }
    byCat.set(cat, entry)
  }
  return Array.from(byCat.entries()).map(([cat, { count, ticker, sample }]) => ({
    cat, count, ticker, sample, at: now,
  }))
}

interface Props {
  nodes: KgNode[]
}

const LAST_SEEN_KEY = 'kg.notif.lastSeenTs'

function readLastSeen(): number {
  try {
    const raw = localStorage.getItem(LAST_SEEN_KEY)
    if (raw) return Number(raw)
  } catch { /* localStorage unavailable */ }
  // First ever mount: baseline at "now" so the existing graph doesn't flood.
  return Date.now() / 1000 - 2
}

function writeLastSeen(ts: number): void {
  try { localStorage.setItem(LAST_SEEN_KEY, String(ts)) } catch { /* ignore */ }
}

export function KgNotificationPanel({ nodes }: Props) {
  const [cards, setCards] = useState<NotificationCard[]>([])
  // Key = `${id}:${updated_at}` — uses updated_at (not created_at) so that
  // re-ingesting the same node (same ID, new value) also triggers a toast.
  const seenRef = useRef<Set<string>>(new Set())
  // `since` = "last time the user saw the KG", read ONCE per mount from
  // localStorage and NEVER mutated mid-render. Nodes with updated_at >= since
  // surface as toasts; on next mount/page-load `since` advances past them.
  //
  // Critical: the watermark is NOT persisted synchronously inside the diff
  // effect. Doing so makes the effect non-idempotent — under React StrictMode
  // (and any double-render) the second pass re-reads the advanced watermark and
  // the toast silently vanishes. Persistence is deferred (see below) so a
  // near-instant remount still reads the old value and re-derives the cards.
  const sinceTsRef = useRef<number>(readLastSeen())
  const maxTsRef = useRef<number>(sinceTsRef.current)
  const persistTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  // Diff: nodes with updated_at >= last-seen that haven't been surfaced yet
  useEffect(() => {
    const seen = seenRef.current
    const since = sinceTsRef.current
    const newOrUpdated: KgNode[] = []
    for (const n of nodes) {
      const ts = n.updated_at ?? 0
      const key = `${n.id}:${ts}`
      if (!seen.has(key) && ts >= since) newOrUpdated.push(n)
      seen.add(key)
      if (ts > maxTsRef.current) maxTsRef.current = ts
    }
    if (newOrUpdated.length === 0) return

    // Defer persisting the watermark. By the time this fires, any synchronous
    // StrictMode remount has already happened (and re-shown the same cards
    // harmlessly). This marks the batch "seen" for the NEXT page load without
    // eating the current toast.
    if (persistTimerRef.current) clearTimeout(persistTimerRef.current)
    persistTimerRef.current = setTimeout(() => writeLastSeen(maxTsRef.current), 4000)

    const now = Date.now()
    const aggregated = aggregateNotifications(newOrUpdated, now)
    setCards(prev => {
      const cats = new Set(aggregated.map(a => a.cat))
      const keep = prev.filter(c => !cats.has(c.cat))
      return [...aggregated, ...keep]
    })
  }, [nodes])

  // Persist the watermark on unmount too, so a clean close (well after the
  // StrictMode double-mount settles) marks the batch seen.
  useEffect(() => {
    return () => {
      if (persistTimerRef.current) clearTimeout(persistTimerRef.current)
    }
  }, [])

  // Auto-dismiss after 6 s
  useEffect(() => {
    if (cards.length === 0) return
    const timer = setInterval(() => {
      const cutoff = Date.now() - 6000
      setCards(prev => prev.filter(c => c.at > cutoff))
    }, 1000)
    return () => clearInterval(timer)
  }, [cards.length])

  const dismiss = (cat: ChangeCategory, at: number) =>
    setCards(prev => prev.filter(c => c.cat !== cat || c.at !== at))

  const dismissAll = () => setCards([])

  if (cards.length === 0) return null

  return (
    <div className="fixed top-16 right-4 z-[60] flex flex-col gap-1.5 max-w-[260px] pointer-events-none">
      {cards.length > 1 && (
        <button
          onClick={dismissAll}
          className="pointer-events-auto self-end text-[9px] text-ink-dim hover:text-ink-muted bg-surface border border-edge rounded px-2 py-0.5 transition-colors"
        >
          Clear all
        </button>
      )}
      {cards.map((c) => {
        const meta = CAT_META[c.cat]
        return (
          <div
            key={`${c.cat}-${c.at}`}
            className={`pointer-events-auto rounded border border-edge border-l-2 ${meta.border} ${meta.bg} bg-bg-overlay px-2.5 py-2 shadow-[0_4px_16px_rgba(0,0,0,0.4)] animate-step-reveal`}
          >
            <div className="flex items-start gap-2">
              <div className={`w-1.5 h-1.5 rounded-full mt-[3px] shrink-0 ${meta.dot}`} />
              <div className="flex-1 min-w-0">
                <div className="flex items-center justify-between gap-1">
                  <span className="text-[10px] font-medium text-ink-muted leading-none">
                    {c.count} {meta.label}{c.count !== 1 ? 's' : ''}
                  </span>
                  <button
                    onClick={() => dismiss(c.cat, c.at)}
                    className="text-ink-dim hover:text-ink-muted shrink-0 transition-colors"
                  >
                    <X size={10} />
                  </button>
                </div>
                {c.sample && (
                  <div className="text-[9px] text-ink-dim mt-0.5 truncate leading-snug">
                    {c.ticker} · {c.sample}
                  </div>
                )}
              </div>
            </div>
          </div>
        )
      })}
    </div>
  )
}
