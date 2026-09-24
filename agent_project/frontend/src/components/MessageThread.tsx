import { useEffect, useRef, useState } from 'react'
import { MarkdownRenderer } from './MarkdownRenderer'
import { QueryInput } from './QueryInput'
import { ActivityTrace, ResearchStepsTrace } from './ActivityTrace'
import { DeckOutlineReview } from './DeckOutlineReview'
import { MemoDraftReview } from './MemoDraftReview'
import type { ActivityEntry } from '../lib/activity'
import type { AgentRunState, AttachedDocSnapshot, DocumentCitation, DocumentInfo, DcfReviewState, EvidenceItem, Mode, Session, SessionMessage, StepState, ToolCall, WorkflowContextReviewState, WorkspaceObject } from '../types'
import { AttachmentChip } from './AttachmentChip'

// ── Copy button ───────────────────────────────────────────────────
function CopyButton({ content }: { content: string }) {
  const [copied, setCopied] = useState(false)
  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(content)
      setCopied(true)
      setTimeout(() => setCopied(false), 1500)
    } catch {
      // fallback silently
    }
  }
  return (
    <button
      type="button"
      onClick={handleCopy}
      title={copied ? 'Copied!' : 'Copy message'}
      className="opacity-0 group-hover:opacity-100 transition-opacity w-7 h-7 flex items-center justify-center rounded-md text-ink-dim hover:text-ink hover:bg-surface flex-shrink-0"
    >
      {copied ? (
        <svg width="13" height="13" viewBox="0 0 16 16" fill="none">
          <path d="M3 8L6 11L13 4" stroke="var(--color-success)" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      ) : (
        <svg width="13" height="13" viewBox="0 0 16 16" fill="none">
          <rect x="5" y="5" width="9" height="9" rx="1.5" stroke="currentColor" strokeWidth="1.3" />
          <path d="M3 11V3.5A1.5 1.5 0 0 1 4.5 2H10" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" />
        </svg>
      )}
    </button>
  )
}

const IMAGE_RE = /\.(png|jpg|jpeg|webp|gif|svg)$/i
const ARTIFACT_MARKER_RE = /\[ARTIFACTS?\]|\[CHART\]/i
const SENSITIVITY_CHART_MARKER = '[SENSITIVITY_CHART]'

function splitOnMarker(text: string): [string, string] {
  const match = ARTIFACT_MARKER_RE.exec(text)
  if (!match) return [text, '']
  return [text.slice(0, match.index).trimEnd(), text.slice(match.index + match[0].length).trimStart()]
}

function splitOnSensitivityChart(text: string): [string, string] {
  const idx = text.indexOf(SENSITIVITY_CHART_MARKER)
  if (idx === -1) return [text, '']
  return [
    text.slice(0, idx).trimEnd(),
    text.slice(idx + SENSITIVITY_CHART_MARKER.length).trimStart(),
  ]
}

function isDcfReport(content: string): boolean {
  return content.startsWith('# DCF Valuation:')
}

function linkifyCitations(content: string, citationMap?: Record<string, string>): string {
  if (!citationMap || !Object.keys(citationMap).length) return content
  return content.replace(/\[(\d+)\](?!\()/g, (match, number: string) => {
    return citationMap[number] ? `[${number}](#source-${number})` : match
  })
}

function formatEvidenceValue(item: EvidenceItem): string | null {
  if (item.value == null) return null
  const value = Number(item.value)
  if (!Number.isFinite(value)) return String(item.value)
  const field = item.field ?? ''
  if (Math.abs(value) <= 1 && /(rate|margin|growth|tax|wacc|yield)/i.test(field)) {
    return `${(value * 100).toFixed(2)}%`
  }
  return value.toLocaleString(undefined, { maximumFractionDigits: 4 })
}

function deckArtifactFilename(artifactPaths?: string[]): string | null {
  const pptx = artifactPaths?.find(p => /\.pptx$/i.test(p))
  if (!pptx) return null
  return pptx.split('/').pop() ?? pptx
}

function DeckArtifactCard({
  threadId,
  artifactPaths,
  title,
  onPreview,
}: {
  threadId: string
  artifactPaths?: string[]
  title?: string
  // threadId is forwarded so historical / cross-session decks resolve against
  // the run that produced them (not the currently active live thread).
  onPreview?: (filename: string, deckTitle: string | undefined, threadId: string) => void
}) {
  const filename = deckArtifactFilename(artifactPaths)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  if (!filename) return null

  const deckTitle = title || filename.replace(/\.pptx$/i, '')

  const handleDownload = async () => {
    setBusy(true)
    setError(null)
    try {
      const res = await fetch(`/runs/${threadId}/decks/${encodeURIComponent(filename)}`)
      if (!res.ok) {
        const body = await res.json().catch(() => ({})) as { detail?: string }
        throw new Error(body.detail || `Download failed (${res.status})`)
      }
      const blob = await res.blob()
      const url = URL.createObjectURL(blob)
      const anchor = document.createElement('a')
      anchor.href = url
      anchor.download = filename
      anchor.click()
      URL.revokeObjectURL(url)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Download failed')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="rounded-lg border border-border bg-bg px-4 py-3 space-y-2">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="text-sm font-medium text-ink truncate">{deckTitle}</p>
          <p className="text-[11px] text-ink-dim">PowerPoint deck · {filename}</p>
        </div>
        <div className="flex items-center gap-2 flex-shrink-0">
          <button
            type="button"
            onClick={() => onPreview?.(filename, deckTitle, threadId)}
            className="px-2.5 py-1 rounded-md border border-accent text-[11px] text-indigo-300 hover:bg-indigo-500/10 transition-colors"
          >
            Preview
          </button>
          <button
            type="button"
            onClick={handleDownload}
            disabled={busy}
            className="px-2.5 py-1 rounded-md border border-border-hover text-[11px] text-ink-muted hover:text-ink hover:border-border-accent transition-colors disabled:opacity-50"
          >
            {busy ? '…' : 'Download'}
          </button>
        </div>
      </div>
      {error && <p className="text-[10px] text-red-300/90">{error}</p>}
    </div>
  )
}

function DcfReportDownloadMenu({ threadId }: { threadId: string }) {
  const [format, setFormat] = useState<'pdf' | 'md'>('pdf')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const handleDownload = async () => {
    setBusy(true)
    setError(null)
    try {
      const res = await fetch(`/runs/${threadId}/dcf-report.${format}`)
      if (!res.ok) {
        let message = `Download failed (${res.status})`
        try {
          const body = (await res.json()) as { detail?: string }
          if (body.detail) message = body.detail
        } catch {
          /* ignore */
        }
        setError(message)
        return
      }
      const blob = await res.blob()
      const disposition = res.headers.get('Content-Disposition') ?? ''
      const match = disposition.match(/filename="([^"]+)"/)
      const filename = match?.[1] ?? `dcf_report.${format}`
      const url = URL.createObjectURL(blob)
      const anchor = document.createElement('a')
      anchor.href = url
      anchor.download = filename
      anchor.click()
      URL.revokeObjectURL(url)
    } catch {
      setError('Download failed — check that the backend is running.')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="flex flex-col items-end gap-1.5">
      <div className="inline-flex items-stretch rounded-md border border-zinc-700 bg-zinc-900/80 overflow-hidden">
        <select
          value={format}
          onChange={e => setFormat(e.target.value as 'pdf' | 'md')}
          className="bg-transparent text-[11px] text-ink-muted px-2 py-1 border-r border-zinc-700 outline-none cursor-pointer hover:text-ink"
          aria-label="Report format"
        >
          <option value="pdf">PDF</option>
          <option value="md">Markdown</option>
        </select>
        <button
          type="button"
          onClick={handleDownload}
          disabled={busy}
          className="inline-flex items-center gap-1.5 px-2.5 py-1 text-[11px] font-medium text-ink-muted hover:text-ink hover:bg-zinc-800/80 transition-colors disabled:opacity-50"
        >
          <span aria-hidden>↓</span>
          {busy ? 'Preparing…' : 'Download'}
        </button>
      </div>
      {error && (
        <p className="max-w-xs text-right text-[10px] text-red-300/90 leading-snug">{error}</p>
      )}
    </div>
  )
}

function EvidenceSourceDrawer({
  citationNumber,
  evidence,
  ticker,
  onClose,
}: {
  citationNumber: string
  evidence?: EvidenceItem
  ticker: string
  onClose: () => void
}) {
  const [rawOpen, setRawOpen] = useState(false)
  const [rawData, setRawData] = useState<string | null>(null)
  const [loadingRaw, setLoadingRaw] = useState(false)
  const [rawError, setRawError] = useState<string | null>(null)

  const title = evidence?.title || evidence?.field || evidence?.source || evidence?.evidence_id || `Citation [${citationNumber}]`
  const tier = evidence?.source_tier ?? 'unknown'
  const value = evidence ? formatEvidenceValue(evidence) : null
  const isApiBacked = !!evidence && (tier === 'structured_api' || evidence.kind === 'structured_fundamental' || evidence.kind === 'market_data' || evidence.kind === 'profile')

  const loadRawData = async () => {
    if (!evidence || !isApiBacked) return
    setRawOpen(true)
    if (rawData || loadingRaw) return
    setLoadingRaw(true)
    setRawError(null)
    try {
      const params = evidence.field ? `?field=${encodeURIComponent(evidence.field)}` : ''
      const res = await fetch(`/sources/fmp/${encodeURIComponent(ticker)}${params}`)
      if (!res.ok) throw new Error(`Source fetch failed (${res.status})`)
      const data = await res.json()
      setRawData(JSON.stringify(data, null, 2))
    } catch (err) {
      setRawError(err instanceof Error ? err.message : 'Source fetch failed')
    } finally {
      setLoadingRaw(false)
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex justify-end bg-black/45" role="dialog" aria-modal="true" aria-label={`Source for citation ${citationNumber}`}>
      <button className="flex-1 cursor-default" aria-label="Close source drawer" onClick={onClose} />
      <aside className="h-full w-full max-w-md border-l border-border bg-bg shadow-2xl flex flex-col">
        <div className="flex items-start justify-between gap-3 border-b border-border px-5 py-4">
          <div className="min-w-0">
            <div className="text-[11px] uppercase tracking-wide text-indigo-300">Source [{citationNumber}]</div>
            <h2 className="mt-1 text-sm font-semibold text-ink leading-snug break-words">{title}</h2>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="rounded-md px-2 py-1 text-sm text-ink-dim hover:bg-zinc-900 hover:text-ink"
            aria-label="Close source drawer"
          >
            ×
          </button>
        </div>

        <div className="flex-1 overflow-y-auto px-5 py-4 space-y-4">
          {!evidence ? (
            <p className="text-sm text-ink-muted">No source metadata was available for this citation.</p>
          ) : (
            <>
              <div className="rounded-lg border border-border bg-bg p-3 space-y-2">
                <div className="flex flex-wrap gap-2 text-[10px]">
                  <span className="rounded bg-indigo-500/10 px-2 py-0.5 font-medium text-indigo-300">{tier.replace('_', ' ')}</span>
                  <span className="rounded bg-zinc-900 px-2 py-0.5 text-ink-muted">{evidence.kind}</span>
                  {'inferred' in evidence && evidence.inferred && (
                    <span className="rounded bg-amber-500/10 px-2 py-0.5 text-amber-300">metadata inferred</span>
                  )}
                  {evidence.as_of && <span className="rounded bg-zinc-900 px-2 py-0.5 text-ink-muted">{evidence.as_of.slice(0, 10)}</span>}
                </div>
                {value && (
                  <div>
                    <div className="text-[11px] text-ink-dim">Value used</div>
                    <div className="text-lg font-semibold text-ink">{value}</div>
                  </div>
                )}
                {evidence.field && (
                  <div className="text-xs text-ink-muted">
                    Field: <span className="font-mono text-ink">{evidence.field}</span>
                  </div>
                )}
                <div className="text-xs text-ink-dim break-all">Evidence ID: {evidence.evidence_id}</div>
              </div>

              {evidence.text && !evidence.inferred && (
                <div className="rounded-lg border border-border bg-bg p-3">
                  <div className="mb-2 text-[11px] font-medium uppercase tracking-wide text-ink-dim">Excerpt</div>
                  <p className="whitespace-pre-wrap break-words text-sm leading-relaxed text-ink-muted">{evidence.text}</p>
                </div>
              )}

              {evidence.inferred && !evidence.url && (
                <p className="text-sm text-ink-muted">
                  Source metadata was not archived with this completed run. The numbered citation still marks where this claim was anchored in the report.
                </p>
              )}

              {evidence.url && (
                <a
                  href={evidence.url}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="inline-flex text-sm text-indigo-300 hover:text-indigo-200 underline underline-offset-2"
                >
                  Open original source
                </a>
              )}

              {isApiBacked && (
                <div className="rounded-lg border border-border bg-bg p-3">
                  <button
                    type="button"
                    onClick={loadRawData}
                    className="text-sm font-medium text-ink hover:text-white"
                  >
                    {rawOpen ? 'Underlying FMP API data' : 'View underlying FMP API data'}
                  </button>
                  {rawOpen && (
                    <div className="mt-3">
                      {loadingRaw && <p className="text-xs text-ink-dim">Loading source data…</p>}
                      {rawError && <p className="text-xs text-red-300">{rawError}</p>}
                      {rawData && (
                        <pre className="max-h-72 overflow-auto rounded-md bg-black/40 p-3 text-[10px] leading-relaxed text-ink-muted">
                          {rawData}
                        </pre>
                      )}
                    </div>
                  )}
                </div>
              )}
            </>
          )}
        </div>
      </aside>
    </div>
  )
}

// ── Individual message renderers ─────────────────────────────────────────────

function UserBubble({
  content,
  attachedDocs,
  onEdit,
  onSelectDoc,
}: {
  content: string
  attachedDocs?: AttachedDocSnapshot[]
  onEdit?: (newContent: string) => void
  onSelectDoc?: (docId: string) => void
}) {
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState(content)

  // Rerun diff messages are pre-formatted in monospace columns; render with
  // a mono font + indigo accent so they look like a system event card rather
  // than a typed user message.
  const isRerunDiff = content.startsWith('🔄')
  if (isRerunDiff) {
    return (
      <div className="flex justify-end animate-fade-up">
        <div className="max-w-[80%] px-4 py-3 rounded-2xl rounded-tr-sm bg-indigo-500/10 border border-indigo-500/30">
          <pre className="text-[12px] text-indigo-100 leading-relaxed font-mono whitespace-pre-wrap m-0">{content}</pre>
        </div>
      </div>
    )
  }

  if (editing && onEdit) {
    const trimmed = draft.trim()
    const unchanged = trimmed === content.trim()
    const submit = () => {
      if (!trimmed || unchanged) return
      onEdit(trimmed)
      setEditing(false)
    }
    return (
      <div className="flex justify-end animate-fade-up">
        <div className="max-w-[72%] w-[72%] flex flex-col gap-2">
          <textarea
            autoFocus
            value={draft}
            onChange={e => setDraft(e.target.value)}
            onKeyDown={e => {
              if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
                e.preventDefault()
                submit()
              } else if (e.key === 'Escape') {
                e.preventDefault()
                setDraft(content)
                setEditing(false)
              }
            }}
            rows={Math.min(8, Math.max(2, draft.split('\n').length))}
            className="w-full px-4 py-2.5 rounded-2xl bg-surface border border-accent text-sm text-ink leading-relaxed resize-none focus:outline-none focus:border-indigo-400"
          />
          <div className="flex items-center justify-end gap-2 text-[11px]">
            <span className="text-ink-dim mr-auto">⌘/Ctrl + Enter to send · Esc to cancel</span>
            <button
              type="button"
              onClick={() => { setDraft(content); setEditing(false) }}
              className="px-2.5 py-1 rounded-md border border-border-hover text-ink-muted hover:text-ink hover:border-border-accent"
            >
              Cancel
            </button>
            <button
              type="button"
              disabled={!trimmed || unchanged}
              onClick={submit}
              className="px-2.5 py-1 rounded-md bg-indigo-500/80 text-white disabled:opacity-40 disabled:cursor-not-allowed hover:bg-indigo-500"
            >
              Send
            </button>
          </div>
        </div>
      </div>
    )
  }

  return (
    <div className="group flex flex-col items-end gap-2">
      {attachedDocs && attachedDocs.length > 0 && (
        <div className="flex gap-2 justify-end overflow-x-auto max-w-full pb-0.5">
          {attachedDocs.map((doc, i) => (
            <AttachmentChip
              key={doc.doc_id}
              variant="sent"
              doc={doc}
              onSelect={onSelectDoc}
              animationDelayMs={i * 50}
            />
          ))}
        </div>
      )}
      <div className="flex justify-end items-start gap-1.5 animate-message-send">
      <CopyButton content={content} />
      {onEdit && (
        <button
          type="button"
          onClick={() => { setDraft(content); setEditing(true) }}
          title="Edit message"
          className="mt-1 opacity-0 group-hover:opacity-100 transition-opacity w-7 h-7 flex items-center justify-center rounded-md text-ink-dim hover:text-ink hover:bg-surface"
        >
          <svg width="13" height="13" viewBox="0 0 16 16" fill="none">
            <path
              d="M11.013 1.427a1.75 1.75 0 0 1 2.474 0l1.086 1.086a1.75 1.75 0 0 1 0 2.474l-8.61 8.61c-.21.21-.47.364-.756.445l-3.251.93a.75.75 0 0 1-.927-.928l.929-3.25c.081-.286.235-.547.445-.758l8.61-8.61Z"
              stroke="currentColor"
              strokeWidth="1.4"
              strokeLinejoin="round"
            />
          </svg>
        </button>
      )}
      <div className="max-w-[72%] px-4 py-2.5 rounded-2xl rounded-tr-sm bg-surface-3 border border-border-hover">
        <p className="text-sm text-ink leading-relaxed whitespace-pre-wrap">{content}</p>
      </div>
      </div>
    </div>
  )
}

function AgentLabel() {
  return (
    <div className="flex items-center gap-1.5 mb-1.5">
      <div className="w-4 h-4 rounded-md bg-accent-soft border border-accent-ring/30 flex items-center justify-center flex-shrink-0">
        <svg width="8" height="8" viewBox="0 0 12 12" fill="none">
          <path d="M2 9L5 3L8 7L10 4" stroke="var(--color-accent-muted)" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      </div>
      <span className="text-[11px] text-ink-dim font-medium">Agent</span>
    </div>
  )
}

function DegradedBanner({ reason }: { reason?: string }) {
  return (
    <div className="rounded-md border border-danger/40 bg-danger-soft px-3 py-2 text-[12px] text-red-200">
      <div className="font-semibold flex items-center gap-1.5">
        <span>⚠</span>
        <span>Degraded result — model marked invalid</span>
      </div>
      {reason && (
        <div className="mt-1 text-[11px] text-red-300/90 leading-snug">{reason}</div>
      )}
      <div className="mt-1 text-[10px] text-red-300/70">
        Treat the figures below as illustrative. Do not act on them as a valuation.
      </div>
    </div>
  )
}

function DocumentCitationDrawer({
  citationId,
  onClose,
}: {
  citationId: string
  onClose: () => void
}) {
  const [citation, setCitation] = useState<DocumentCitation | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [showContext, setShowContext] = useState(false)

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError(null)
    setCitation(null)
    setShowContext(false)
    ;(async () => {
      try {
        const res = await fetch(`/documents/citations/${encodeURIComponent(citationId)}`)
        if (!res.ok) throw new Error(`Citation fetch failed (${res.status})`)
        const data = (await res.json()) as DocumentCitation
        if (!cancelled) setCitation(data)
      } catch (err) {
        if (!cancelled) setError(err instanceof Error ? err.message : 'Citation fetch failed')
      } finally {
        if (!cancelled) setLoading(false)
      }
    })()
    return () => { cancelled = true }
  }, [citationId])

  const title = citation?.filename ?? citationId
  const meta = [
    citation?.company,
    citation?.ticker,
    citation?.doc_type?.replace('_', ' '),
    citation?.fiscal_period,
  ].filter(Boolean).join(' · ')
  const structuredTableBlocks = citation ? structuredTablesToBlocks(citation.tables) : []
  const citedBlocks = citation
    ? [...structuredTableBlocks, ...formatCitationBlocks(citation.text, structuredTableBlocks.length > 0)]
    : []
  const previousBlocks = citation?.previous_text ? formatCitationBlocks(citation.previous_text) : []
  const nextBlocks = citation?.next_text ? formatCitationBlocks(citation.next_text) : []
  const hasContext = Boolean(citation?.previous_text || citation?.next_text)

  return (
    <div className="fixed inset-0 z-50 flex justify-end bg-black/45" role="dialog" aria-modal="true" aria-label="Document citation source">
      <button className="flex-1 cursor-default" aria-label="Close document citation drawer" onClick={onClose} />
      <aside className="h-full w-full max-w-lg border-l border-border bg-bg shadow-2xl flex flex-col">
        <div className="border-b border-border px-5 py-4">
          <div className="flex items-start justify-between gap-3">
            <div className="min-w-0">
              <div className="text-[11px] uppercase tracking-wide text-indigo-300">Document source</div>
              <h2 className="mt-1 text-sm font-semibold text-ink leading-snug break-words">{title}</h2>
              {citation?.page != null && (
                <div className="mt-1 text-xs text-ink-dim">Page {citation.page}</div>
              )}
            </div>
            <button
              type="button"
              onClick={onClose}
              className="rounded-md px-2 py-1 text-sm text-ink-dim hover:bg-zinc-900 hover:text-ink"
              aria-label="Close document citation drawer"
            >
              ×
            </button>
          </div>
          {citation && (
            <div className="mt-4 flex flex-wrap items-center gap-2">
              <span className="rounded bg-indigo-500/10 px-2 py-1 text-[11px] font-medium text-indigo-300">uploaded document</span>
              {citation.page != null && <span className="rounded bg-zinc-900 px-2 py-1 text-[11px] text-ink-muted">p.{citation.page}</span>}
              {citation.ticker && <span className="rounded bg-zinc-900 px-2 py-1 text-[11px] text-ink-muted">{citation.ticker}</span>}
              <a
                href={`/documents/${encodeURIComponent(citation.doc_id)}/file`}
                target="_blank"
                rel="noopener noreferrer"
                className="ml-auto rounded-md border border-border px-2.5 py-1 text-xs text-ink-muted hover:border-border-hover hover:text-ink"
              >
                Open PDF
              </a>
            </div>
          )}
        </div>

        <div className="flex-1 overflow-y-auto px-5 py-4 space-y-4">
          {loading && <p className="text-sm text-ink-muted">Loading source…</p>}
          {error && <p className="text-sm text-red-300">{error}</p>}
          {citation && (
            <>
              <section className="rounded-lg border border-indigo-500/30 bg-indigo-500/5 p-3">
                <div className="mb-3 flex items-center justify-between gap-3">
                  <div>
                    <div className="text-[11px] font-medium uppercase tracking-wide text-indigo-300">Evidence from page {citation.page}</div>
                    {meta && <div className="mt-1 text-xs text-ink-dim">{meta}</div>}
                  </div>
                </div>
                <FormattedCitationBlocks blocks={citedBlocks} />
              </section>

              {hasContext && (
                <section className="rounded-lg border border-border bg-bg/60">
                  <button
                    type="button"
                    onClick={() => setShowContext(value => !value)}
                    className="flex w-full items-center justify-between px-3 py-3 text-left"
                    aria-expanded={showContext}
                  >
                    <span className="text-[11px] font-medium uppercase tracking-wide text-ink-dim">Surrounding page text</span>
                    <span className="text-xs text-ink-dim">{showContext ? 'Hide' : 'Show'}</span>
                  </button>
                  {showContext && (
                    <div className="space-y-3 border-t border-border px-3 py-3">
                      {citation.previous_text && (
                        <div>
                          <div className="mb-2 text-[11px] font-medium uppercase tracking-wide text-ink-dim">Before citation</div>
                          <FormattedCitationBlocks blocks={previousBlocks} compact />
                        </div>
                      )}
                      {citation.next_text && (
                        <div>
                          <div className="mb-2 text-[11px] font-medium uppercase tracking-wide text-ink-dim">After citation</div>
                          <FormattedCitationBlocks blocks={nextBlocks} compact />
                        </div>
                      )}
                    </div>
                  )}
                </section>
              )}
            </>
          )}
        </div>
      </aside>
    </div>
  )
}

type CitationBlock =
  | { kind: 'paragraph'; text: string }
  | { kind: 'table'; title?: string; headers: string[]; rows: string[][] }

function looksNumericToken(token: string): boolean {
  return /^[-+]?(\d[\d,.]*)(%|bn|mn|m|x)?$/i.test(token.trim())
}

function structuredTablesToBlocks(tables?: DocumentCitation['tables']): CitationBlock[] {
  if (!Array.isArray(tables) || !tables.length) return []
  return tables
    .filter(table => Array.isArray(table.headers) && Array.isArray(table.rows) && table.rows.length && !looksLikeSlideCardGrid(table.headers, table.rows))
    .map(table => ({
      kind: 'table' as const,
      title: table.caption || undefined,
      headers: table.headers,
      rows: table.rows,
    }))
}

function formatCitationBlocks(text: string, skipHeuristicTables = false): CitationBlock[] {
  const normalized = (text || '').replace(/\r/g, '').trim()
  if (!normalized) return []
  if (skipHeuristicTables) {
    const withoutTables = normalized.replace(/\[TABLE[^\]]*\]\n?[\s\S]*?\n?\[\/TABLE\]/g, '').trim()
    return withoutTables ? [{ kind: 'paragraph', text: withoutTables }] : []
  }
  const tableMarkerText = normalized.replace(/\[TABLE[^\]]*\]/g, '\n').replace(/\[\/TABLE\]/g, '\n')
  const financial = parseFinancialSlideText(tableMarkerText)
  if (financial) return financial

  const explicitTables = [...normalized.matchAll(/\[TABLE[^\]]*\]\n?([\s\S]*?)\n?\[\/TABLE\]/g)]
  if (explicitTables.length) {
    const blocks: CitationBlock[] = []
    let cursor = 0
    for (const match of explicitTables) {
      const before = normalized.slice(cursor, match.index).trim()
      if (before) blocks.push({ kind: 'paragraph', text: before.replace(/\[TABLE[^\]]*\]/g, '').trim() })
      const table = parsePipeTable(match[1])
      if (table) blocks.push(table)
      cursor = (match.index ?? 0) + match[0].length
    }
    const after = normalized.slice(cursor).trim()
    if (after) blocks.push({ kind: 'paragraph', text: after })
    return blocks.length ? blocks : [{ kind: 'paragraph', text: normalized }]
  }

  return [{ kind: 'paragraph', text: normalized }]
}

function startsWithNumericSignal(cell: string): boolean {
  return /^\s*[-+]?\d[\d,.]*(?:%|bn|mn|m|x)?\b/i.test(cell)
}

function wordCount(cell: string): number {
  return (cell.match(/[A-Za-z][A-Za-z&-]*/g) ?? []).length
}

function looksLikeSlideCardGrid(headers: string[], rows: string[][]): boolean {
  if (headers.length < 3 || rows.length < 2) return false
  const numericHeaderSignals = headers.filter(startsWithNumericSignal).length
  const avgHeaderWords = headers.reduce((sum, cell) => sum + wordCount(cell), 0) / Math.max(1, headers.length)
  const cells = rows.flat().filter(Boolean)
  const longBodyCells = cells.filter(cell => wordCount(cell) >= 5).length
  return (
    numericHeaderSignals >= Math.max(2, Math.floor(headers.length / 2)) &&
    avgHeaderWords >= 3 &&
    longBodyCells >= Math.max(2, Math.floor(cells.length / 3))
  )
}

function parsePipeTable(raw: string): CitationBlock | null {
  const rows = raw
    .split('\n')
    .map(line => line.split('|').map(cell => cell.trim()).filter(Boolean))
    .filter(row => row.length > 1)
  if (!rows.length) return null
  const maxCols = Math.max(...rows.map(row => row.length))
  const padded = rows.map(row => [...row, ...Array(Math.max(0, maxCols - row.length)).fill('')])
  return {
    kind: 'table',
    headers: padded[0],
    rows: padded.slice(1),
  }
}

function parseFinancialSlideText(text: string): CitationBlock[] | null {
  const lines = text.split('\n').map(line => line.trim()).filter(Boolean)
  if (lines.length < 4) return null
  const joined = lines.join(' ')
  const known = parseKnownFinancialMetrics(joined)
  if (known) {
    const lead = lines.slice(0, 2).join(' ')
    return [
      ...(lead ? [{ kind: 'paragraph' as const, text: lead }] : []),
      known,
    ]
  }
  const valueTokens = joined.match(/[-+]?\d[\d,.]*(?:%|bn|mn|m|x)?/gi) ?? []
  if (valueTokens.length < 6) return null

  const titleLines = lines.filter(line => !/[0-9]/.test(line)).slice(0, 2)
  const title = titleLines.join(' · ') || undefined
  const metricLine = [...lines].reverse().find(line => {
    const tokens = line.split(/\s+/)
    const numeric = tokens.filter(looksNumericToken).length
    return tokens.length >= 3 && numeric < Math.max(1, tokens.length / 3)
  })
  const metrics = metricLine
    ? metricLine.split(/\s{2,}|\s(?=[A-Z][a-z]+(?:\s|$))/).map(part => part.trim()).filter(part => part && !looksNumericToken(part))
    : []

  if (metrics.length >= 2) {
    const cols = Math.min(metrics.length, 4)
    const values = valueTokens.slice(-cols * 2)
    const rows: string[][] = []
    for (let i = 0; i < cols; i += 1) {
      rows.push([
        metrics[i],
        values[i * 2] ?? '',
        values[i * 2 + 1] ?? '',
      ])
    }
    const lead = lines.slice(0, 2).join(' ')
    return [
      ...(lead ? [{ kind: 'paragraph' as const, text: lead }] : []),
      { kind: 'table', title, headers: ['Metric', 'FY24', 'FY25'], rows },
    ]
  }

  const pairs: string[][] = []
  for (let i = 0; i < valueTokens.length - 1; i += 2) {
    pairs.push([`Item ${pairs.length + 1}`, valueTokens[i], valueTokens[i + 1]])
  }
  if (!pairs.length) return null
  return [
    { kind: 'paragraph', text: lines.slice(0, 2).join(' ') },
    { kind: 'table', title, headers: ['Metric', 'Prior', 'Current'], rows: pairs.slice(0, 8) },
  ]
}

function parseKnownFinancialMetrics(text: string): Extract<CitationBlock, { kind: 'table' }> | null {
  const metrics = [
    {
      label: 'Financing portfolio',
      growth: text.match(/([\d.]+)%\s+YoY\s*Growth\s+in\s+financing\s+portfolio/i)?.[1],
      values: [...text.matchAll(/([\d,.]+bn)\s*\+?[\d.]+%\s+([\d,.]+bn)/gi)][0],
    },
    {
      label: 'Liabilities',
      growth: text.match(/([\d.]+)%\s+Growth\s+in\s+liabilities/i)?.[1],
      values: [...text.matchAll(/([\d,.]+bn)\s*\+?[\d.]+%\s+([\d,.]+bn)/gi)][1],
    },
    {
      label: 'Loan-to-deposit ratio',
      growth: '',
      values: text.match(/Loan to Deposit Ratio[\s\S]*?([\d.]+%)\s+([\d.]+%)/i),
    },
    {
      label: 'Net yield income',
      growth: text.match(/([\d.]+)%\s+growth\s+in\s+net\s+yield\s+income/i)?.[1],
      values: text.match(/Net Yield income[\s\S]*?([\d,.]+mn)\s+([\d,.]+mn)/i),
    },
    {
      label: 'Non-yield income',
      growth: text.match(/([\d.]+)%\s+higher\s+Non\s+yield\s+income/i)?.[1],
      values: text.match(/Non Yield Income[\s\S]*?([\d,.]+mn)\s+([\d,.]+mn)/i),
    },
    {
      label: 'Operating income',
      growth: text.match(/([\d.]+)%\s+higher\s+operating\s+income/i)?.[1],
      values: text.match(/Operating Income[\s\S]*?([\d,.]+mn)\s+([\d,.]+mn)/i),
    },
  ]

  const rows = metrics.flatMap(metric => {
    const values = metric.values
    if (!values) return []
    return [[
      metric.label,
      values[1] ?? '',
      values[2] ?? '',
      metric.growth ? `${metric.growth}%` : '',
    ]]
  })
  if (rows.length < 3) return null
  return {
    kind: 'table',
    title: 'Financial metrics extracted from cited slide',
    headers: ['Metric', 'Prior period', 'Current period', 'Growth'],
    rows,
  }
}

function FormattedCitationBlocks({ blocks, compact = false }: { blocks: CitationBlock[]; compact?: boolean }) {
  if (!blocks.length) return null
  return (
    <div className={compact ? 'space-y-2' : 'space-y-3'}>
      {blocks.map((block, idx) => {
        if (block.kind === 'paragraph') {
          return (
            <p key={idx} className={`${compact ? 'text-xs text-ink-muted' : 'text-sm text-ink'} whitespace-pre-wrap break-words leading-relaxed`}>
              {block.text}
            </p>
          )
        }
        return (
          <div key={idx} className="overflow-x-auto rounded-lg border border-border-hover bg-bg/80">
            {block.title && (
              <div className="border-b border-border px-3 py-2 text-xs font-medium text-ink-muted">{block.title}</div>
            )}
            <table className="w-full min-w-[360px] border-collapse text-left text-xs">
              <thead className="bg-bg-overlay">
                <tr>
                  {block.headers.map((header, i) => (
                    <th key={i} className="border-b border-border px-3 py-2 font-semibold uppercase tracking-wide text-ink-dim">
                      {header || `Column ${i + 1}`}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {block.rows.map((row, rowIdx) => (
                  <tr key={rowIdx} className="border-b border-border last:border-b-0">
                    {row.map((cell, cellIdx) => (
                      <td key={cellIdx} className={`px-3 py-2 align-top ${cellIdx === 0 ? 'text-ink-muted' : 'font-medium text-ink'}`}>
                        {cell}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )
      })}
    </div>
  )
}

function ChatBubble({
  content,
  streaming,
  toolCalls,
  activities,
  persisted,
  dcfReview,
  onDcfApprove,
  onDcfReject,
  threadId,
  artifactPaths,
  dcfEvidenceItems,
  dcfCitationMap,
  hideLabel,
  validity,
  invalidationReason,
  onOpenDeckPreview,
}: {
  content: string
  streaming?: boolean
  /** Legacy ToolCall list (committed messages, pre-activity contract). */
  toolCalls?: ToolCall[]
  /** Unified activity log scoped to chat — preferred when provided. */
  activities?: ActivityEntry[]
  /** True when rendering a committed message (read-only). */
  persisted?: boolean
  dcfReview?: DcfReviewState
  onDcfApprove?: (overrides?: Record<string, number>) => void
  onDcfReject?: () => void
  threadId?: string
  artifactPaths?: string[]
  dcfEvidenceItems?: EvidenceItem[]
  dcfCitationMap?: Record<string, string>
  hideLabel?: boolean
  validity?: 'valid' | 'invalid' | 'adjusting'
  invalidationReason?: string
  onOpenDeckPreview?: (filename: string, deckTitle: string | undefined, threadId: string) => void
}) {
  const useUnified = !!(activities && activities.length)
  const calls = toolCalls ?? []
  const hasContent = !!content
  const hasRunning = useUnified
    ? activities!.some(a => a.status === 'started' || a.status === 'running')
    : calls.some(t => t.status === 'running')
  const hasAnyActivity = useUnified ? activities!.length > 0 : calls.length > 0
  const deckFilename = deckArtifactFilename(artifactPaths)
  const [openDocCitation, setOpenDocCitation] = useState<string | null>(null)

  // Activity defaults: open while we're still working (no content yet),
  // collapsed once the assistant message is present so the response stays
  // front-and-center but the audit trail remains one click away.
  const defaultOpen = !hasContent && !persisted

  return (
    <div className="flex justify-start animate-fade-up">
      <div className="max-w-[85%] min-w-0 w-full">
        {!hideLabel && <AgentLabel />}
        <div className="pl-1 space-y-2">
          {validity === 'invalid' && (
            <DegradedBanner reason={invalidationReason} />
          )}
          {hasAnyActivity && (
            <ActivityTrace
              toolCalls={useUnified ? undefined : calls}
              activities={useUnified ? activities : undefined}
              scope={useUnified ? 'chat' : undefined}
              variant="inline"
              defaultOpen={defaultOpen || !!dcfReview}
              dcfReview={dcfReview}
              onDcfApprove={onDcfApprove}
              onDcfReject={onDcfReject}
              threadId={threadId}
            />
          )}

          {hasContent ? (
            isDcfReport(content) && !streaming ? (
              <DcfReportCard
                content={content}
                threadId={threadId}
                artifactPaths={artifactPaths}
                evidenceItems={dcfEvidenceItems}
                citationMap={dcfCitationMap}
              />
            ) : (
              <>
                <MarkdownRenderer
                  content={content}
                  streaming={streaming}
                  onDocCitationClick={setOpenDocCitation}
                />
                {!streaming && threadId && artifactPaths?.length ? (
                  <ArtifactImages artifactPaths={artifactPaths} threadId={threadId} />
                ) : null}
                {!streaming && <CopyButton content={content} />}
                {!streaming && deckFilename && threadId && (
                  <DeckArtifactCard
                    threadId={threadId}
                    artifactPaths={artifactPaths}
                    onPreview={onOpenDeckPreview}
                  />
                )}
              </>
            )
          ) : !persisted ? (
            <ThinkingDots />
          ) : null}
          {openDocCitation && (
            <DocumentCitationDrawer
              citationId={openDocCitation}
              onClose={() => setOpenDocCitation(null)}
            />
          )}
        </div>
      </div>
    </div>
  )
}

function DcfReportCard({
  content,
  threadId,
  artifactPaths,
  evidenceItems,
  citationMap,
}: {
  content: string
  threadId?: string
  artifactPaths?: string[]
  evidenceItems?: EvidenceItem[]
  citationMap?: Record<string, string>
}) {
  const canDownload = !!threadId
  const hasMarker = content.includes(SENSITIVITY_CHART_MARKER)
  const [preChart, postChart] = hasMarker ? splitOnSensitivityChart(content) : [content, '']
  const sensitivityImage = artifactPaths?.find(p => p.includes('sensitivity') && IMAGE_RE.test(p))
  const [openCitation, setOpenCitation] = useState<string | null>(null)
  const [openDocCitation, setOpenDocCitation] = useState<string | null>(null)
  const evidenceById = new Map((evidenceItems ?? []).map(item => [item.evidence_id, item]))
  const ticker = content.match(/^# DCF Valuation:\s*([A-Z0-9.-]+)/)?.[1] ?? ''
  const linkifiedPreChart = linkifyCitations(preChart, citationMap)
  const linkifiedPostChart = linkifyCitations(postChart, citationMap)
  const openEvidence = openCitation && citationMap ? evidenceById.get(citationMap[openCitation]) : undefined

  return (
    <div className="rounded-xl border border-border bg-bg px-6 py-5">
      <MarkdownRenderer
        content={linkifiedPreChart}
        streaming={false}
        onCitationClick={setOpenCitation}
        onDocCitationClick={setOpenDocCitation}
      />

      {hasMarker && sensitivityImage && threadId && (
        <figure className="my-5 space-y-2">
          <img
            src={`/artifacts/${threadId}/${sensitivityImage.split('/').pop()}`}
            alt="Sensitivity heatmap"
            className="w-full max-w-xl rounded-lg border border-border"
          />
          <figcaption className="text-[11px] text-ink-dim">
            WACC × terminal growth sensitivity
          </figcaption>
        </figure>
      )}

      {postChart && (
        <MarkdownRenderer
          content={linkifiedPostChart}
          streaming={false}
          onCitationClick={setOpenCitation}
          onDocCitationClick={setOpenDocCitation}
        />
      )}

      {canDownload && (
        <div className="mt-6 pt-4 border-t border-border flex justify-end">
          <DcfReportDownloadMenu threadId={threadId!} />
        </div>
      )}

      {openCitation && (
        <EvidenceSourceDrawer
          citationNumber={openCitation}
          evidence={openEvidence}
          ticker={ticker}
          onClose={() => setOpenCitation(null)}
        />
      )}
      {openDocCitation && (
        <DocumentCitationDrawer
          citationId={openDocCitation}
          onClose={() => setOpenDocCitation(null)}
        />
      )}
    </div>
  )
}

function ResearchReportCard({
  content,
  threadId,
  artifactPaths,
  streaming,
  steps,
  activity,
}: {
  content: string
  threadId?: string
  artifactPaths?: string[]
  streaming?: boolean
  /** Persisted research-step snapshot. When provided, an Activity bar is
   *  rendered above the report so the audit trail survives after commit. */
  steps?: StepState[]
  /** Unified activity snapshot (preferred when present). */
  activity?: ActivityEntry[]
}) {
  const hasArtifacts = !streaming && (artifactPaths?.length ?? 0) > 0 && !!threadId
  const markerPresent = ARTIFACT_MARKER_RE.test(content)
  const [before, after] = hasArtifacts && markerPresent ? splitOnMarker(content) : [content, '']

  // Keep the research plan timeline as the primary audit surface. Activity is
  // useful detail, but it should not replace the step descriptions/messages.
  const safeActivity = Array.isArray(activity) ? activity : []
  const safeSteps = Array.isArray(steps) ? steps : []
  const [openDocCitation, setOpenDocCitation] = useState<string | null>(null)
  const researchActivity = safeActivity.filter(
    a => a.scope === 'research' || a.scope === 'workflow',
  )

  return (
    <div className="flex justify-start animate-fade-up w-full">
      <div className="w-full min-w-0 space-y-2">
        <AgentLabel />
        {safeSteps.length > 0 ? (
          <ResearchStepsTrace steps={safeSteps} defaultOpen={!!streaming} />
        ) : researchActivity.length > 0 ? (
          <ActivityTrace
            activities={researchActivity}
            label="Research activity"
            defaultOpen={false}
          />
        ) : null}
        <div
          className={`
            rounded-xl border border-border bg-bg px-6 py-5
            ${streaming ? '' : ''}
          `}
        >
          <MarkdownRenderer
            content={before}
            streaming={streaming && !markerPresent && !after}
            onDocCitationClick={setOpenDocCitation}
          />

          {hasArtifacts && markerPresent && (
            <ArtifactImages artifactPaths={artifactPaths!} threadId={threadId!} />
          )}

          {after && (
            <MarkdownRenderer
              content={after}
              streaming={streaming}
              onDocCitationClick={setOpenDocCitation}
            />
          )}

          {hasArtifacts && !markerPresent && (
            <ArtifactImages artifactPaths={artifactPaths!} threadId={threadId!} />
          )}
          {openDocCitation && (
            <DocumentCitationDrawer
              citationId={openDocCitation}
              onClose={() => setOpenDocCitation(null)}
            />
          )}
        </div>
      </div>
    </div>
  )
}

function ArtifactImages({ artifactPaths, threadId }: { artifactPaths: string[]; threadId: string }) {
  const images = artifactPaths.filter(p => IMAGE_RE.test(p))
  if (!images.length) return null
  return (
    <div className="my-5 space-y-4">
      {images.map(p => {
        const filename = p.split('/').pop() ?? p
        const label = filename.replace(/\.[^.]+$/, '').replace(/[_-]/g, ' ')
        return (
          <figure key={p} className="space-y-2">
            <img
              src={`/artifacts/${threadId}/${filename}`}
              alt={label}
              className="rounded-xl border border-border-hover max-w-full"
            />
            <figcaption className="text-xs text-ink-dim text-center">{label}</figcaption>
          </figure>
        )
      })}
    </div>
  )
}

/** Status card shown during research planning/executing (before synthesis). */
function ResearchStatusCard({ run }: { run: AgentRunState }) {
  const { status, steps } = run
  const total = steps.length
  const completed = steps.filter(s => s.status === 'completed').length
  const running = steps.find(s => s.status === 'running')
  const isExecuting = status === 'executing' || status === 'workflow_running'

  let label = ''
  let sublabel = ''
  if (status === 'classifying') { label = 'Classifying intent'; sublabel = 'Determining research approach' }
  else if (status === 'planning') { label = 'Building research plan'; sublabel = 'Creating step-by-step strategy' }
  else if (status === 'awaiting_approval') { label = 'Plan ready'; sublabel = 'Review in the sidebar' }
  else if (status === 'workflow_running') { label = 'Running DCF workflow'; sublabel = `${completed}/${total} steps complete` }
  else if (status === 'awaiting_assumptions') { label = 'Assumptions ready'; sublabel = 'Review in the sidebar' }
  else if (status === 'awaiting_outline_review') { label = 'Deck outline ready'; sublabel = 'Review in the sidebar' }
  else if (status === 'executing') {
    label = running
      ? (running.description.length > 55 ? running.description.slice(0, 55) + '…' : running.description)
      : `Executing step ${completed + 1}/${total}`
    sublabel = `${completed}/${total} steps complete`
  }

  return (
    <div className="flex justify-start animate-fade-up">
      <div className="max-w-[85%]">
        <AgentLabel />
        <div className="flex items-start gap-2.5 py-2">
          {/* Manus-style 19px bordered circle with checkmark or spinner */}
          <div className={`w-[19px] h-[19px] rounded-full border flex items-center justify-center flex-shrink-0 mt-0.5 ${
            isExecuting
              ? 'border-indigo-500/40 bg-indigo-500/10'
              : 'border-amber-500/40 bg-amber-500/10'
          }`}>
            {isExecuting ? (
              <svg className="animate-spin" width="10" height="10" viewBox="0 0 12 12" fill="none">
                <path d="M6 1a5 5 0 0 1 5 5" stroke="#818cf8" strokeWidth="1.5" strokeLinecap="round"/>
              </svg>
            ) : (
              <svg width="10" height="10" viewBox="0 0 12 12" fill="none">
                <path d="M2.5 6L5 8.5L9.5 3.5" stroke="#f59e0b" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"/>
              </svg>
            )}
          </div>
          <div className="flex flex-col gap-0.5">
            <span className="text-[13px] font-medium text-ink-primary leading-snug">{label}</span>
            <span className="text-[11px] text-ink-muted leading-tight">{sublabel}</span>
          </div>
        </div>
      </div>
    </div>
  )
}

function ThinkingDots() {
  return (
    <div className="flex items-center gap-1 h-6 pl-1">
      {[0, 1, 2].map(i => (
        <div
          key={i}
          className="w-1.5 h-1.5 rounded-full bg-ink-dim animate-pulse"
          style={{ animationDelay: `${i * 150}ms` }}
        />
      ))}
    </div>
  )
}

// ── Committed message renderer ────────────────────────────────────────────────

function CommittedMessage({
  msg,
  onOpenDeckPreview,
  onEdit,
  onSelectDoc,
}: {
  msg: SessionMessage
  onOpenDeckPreview?: (filename: string, deckTitle: string | undefined, threadId: string) => void
  onEdit?: (newContent: string) => void
  onSelectDoc?: (docId: string) => void
}) {
  if (msg.type === 'user') {
    if (msg.content.startsWith('[DCF_APPROVED]')) return null
    if (msg.content.startsWith('[DECK_COMPLETE]')) return null
    return (
      <UserBubble
        content={msg.content}
        attachedDocs={msg.attachedDocs}
        onEdit={onEdit}
        onSelectDoc={onSelectDoc}
      />
    )
  }
  if (msg.type === 'chat_response') {
    return (
      <ChatBubble
        content={msg.content}
        toolCalls={msg.toolTrace}
        activities={msg.activity}
        threadId={msg.threadId}
        artifactPaths={msg.artifactPaths}
        dcfEvidenceItems={msg.dcfEvidenceItems}
        dcfCitationMap={msg.dcfCitationMap}
        persisted
        validity={msg.validity}
        invalidationReason={msg.invalidationReason}
        onOpenDeckPreview={onOpenDeckPreview}
      />
    )
  }
  if (msg.type === 'research_report') {
    return (
      <ResearchReportCard
        content={msg.content}
        threadId={msg.threadId}
        artifactPaths={msg.artifactPaths}
        steps={msg.researchSteps}
        activity={msg.activity}
      />
    )
  }
  return null
}

// ── Main component ────────────────────────────────────────────────────────────

export function WorkflowSetupCard({
  review,
  onSubmit,
}: {
  review: WorkflowContextReviewState
  onSubmit?: (context: Record<string, any>, approved?: boolean) => void
}) {
  if (review.workflow_id === 'tracked_task') return <TrackedTaskProposal review={review} onSubmit={onSubmit} />
  return <WorkflowSetupFields review={review} onSubmit={onSubmit} />
}

function TrackedTaskProposal({ review, onSubmit }: {
  review: WorkflowContextReviewState
  onSubmit?: (context: Record<string, any>, approved?: boolean) => void
}) {
  const [submitted, setSubmitted] = useState(false)
  const [error, setError] = useState('')
  const choose = async (tracked: boolean) => {
    setSubmitted(true)
    setError('')
    try { await onSubmit?.(review.context, tracked) }
    catch { setError('Could not submit decision. Please retry.'); setSubmitted(false) }
  }
  return <section className="my-4 rounded-md border border-border p-4">
    <h3 className="text-sm font-semibold text-ink">Track this work?</h3>
    <p className="mt-2 whitespace-pre-wrap text-sm text-ink-muted">{String(review.context.goal || '')}</p>
    <p className="mt-2 text-xs text-ink-dim">Outputs: {(review.context.requested_outputs || []).join(', ')}</p>
    {error && <p role="alert" className="mt-2 text-sm text-red-400">{error}</p>}
    <div className="mt-4 flex flex-wrap gap-3">
      <button type="button" disabled={submitted} onClick={() => choose(true)} className="rounded bg-blue-600 px-3 py-2 text-sm text-white disabled:opacity-50">Create task</button>
      <button type="button" disabled={submitted} onClick={() => choose(false)} className="rounded border border-border px-3 py-2 text-sm text-ink-muted disabled:opacity-50">Continue in chat</button>
    </div>
  </section>
}

function WorkflowSetupFields({ review, onSubmit }: {
  review: WorkflowContextReviewState
  onSubmit?: (context: Record<string, any>, approved?: boolean) => void
}) {
  const [context, setContext] = useState<Record<string, any>>(() => ({ ...(review.context ?? {}) }))
  const output = (context.output_requirements ?? {}) as Record<string, any>
  const workflowId = String(review.workflow_id || context.workflow_id || 'workflow')
  const workflowLabel = workflowId.replace(/_/g, ' ')
  const docs = Array.isArray(context.available_documents) ? context.available_documents : []
  const artifacts = Array.isArray(context.available_artifacts) ? context.available_artifacts : []
  const selectedDocs = new Set(
    Array.isArray(context.selected_doc_ids) ? context.selected_doc_ids.map(String) : [],
  )
  const selectedArtifacts = new Set(
    Array.isArray(context.selected_artifact_ids) ? context.selected_artifact_ids.map(String) : [],
  )
  const focusAreas = Array.isArray(context.focus_areas) ? context.focus_areas.map(String) : []
  const selectedFocus = new Set(focusAreas)
  const focusControl = review.controls.find(c => c.field === 'focus_areas')
  const focusOptions = (focusControl?.options?.length ? focusControl.options : ['growth', 'margins', 'risk'])
    .map(option => typeof option === 'string' ? option : option.label)

  const updateContext = (patch: Record<string, any>) => {
    setContext(prev => ({ ...prev, ...patch }))
  }

  const updateOutput = (patch: Record<string, any>) => {
    setContext(prev => ({
      ...prev,
      output_requirements: {
        ...((prev.output_requirements ?? {}) as Record<string, any>),
        ...patch,
      },
    }))
  }

  const toggleDoc = (docId: string) => {
    const next = new Set(selectedDocs)
    if (next.has(docId)) next.delete(docId)
    else next.add(docId)
    updateContext({ selected_doc_ids: [...next] })
  }

  const toggleArtifact = (objectId: string) => {
    const next = new Set(selectedArtifacts)
    if (next.has(objectId)) next.delete(objectId)
    else next.add(objectId)
    updateContext({ selected_artifact_ids: [...next] })
  }

  const toggleFocus = (area: string) => {
    const next = new Set(selectedFocus)
    if (next.has(area)) next.delete(area)
    else next.add(area)
    updateContext({ focus_areas: [...next] })
  }

  const companyName = String(context.company_name ?? context.primary_entity?.name ?? context.ticker ?? '')
  const horizon = Number(context.horizon_years ?? 5)
  const showDcfFields = workflowId === 'dcf'

  return (
    <div className="flex justify-start animate-fade-up">
      <div className="w-full max-w-[760px] min-w-0">
        <AgentLabel />
        <div className="mt-2 rounded-lg border border-border bg-surface/35 p-4 space-y-4">
          <div className="flex items-start justify-between gap-4">
            <div>
              <div className="text-sm font-semibold text-ink">{review.title || 'Setup DCF'}</div>
              <div className="mt-1 text-xs text-ink-muted">
                Confirm scope before this workflow starts.
              </div>
            </div>
            <div className="flex items-center gap-2 text-xs">
              <span className="rounded-md border border-border-subtle bg-bg px-2 py-1 text-ink-muted">
                {showDcfFields ? (companyName || 'Company needed') : workflowLabel}
              </span>
              <span className="rounded-md border border-border-subtle bg-bg px-2 py-1 text-ink-muted">
                {showDcfFields && Number.isFinite(horizon) ? `${horizon}y` : String(output.depth ?? 'full')}
              </span>
            </div>
          </div>

          {showDcfFields && (
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
            <label className="space-y-1">
              <span className="text-[11px] uppercase tracking-[0.12em] text-ink-dim">Company</span>
              <input
                value={companyName}
                onChange={e => updateContext({
                  company_name: e.target.value,
                  primary_entity: { kind: 'company', name: e.target.value },
                })}
                className="w-full rounded-md border border-border-subtle bg-bg px-3 py-2 text-sm text-ink outline-none focus:border-accent"
              />
            </label>
            <label className="space-y-1">
              <span className="text-[11px] uppercase tracking-[0.12em] text-ink-dim">Horizon</span>
              <select
                value={String(horizon)}
                onChange={e => updateContext({ horizon_years: Number(e.target.value) })}
                className="w-full rounded-md border border-border-subtle bg-bg px-3 py-2 text-sm text-ink outline-none focus:border-accent"
              >
                {[3, 5, 7, 10].map(years => (
                  <option key={years} value={years}>{years} years</option>
                ))}
              </select>
            </label>
          </div>
          )}

          {docs.length > 0 && (
            <div className="space-y-2">
              <div className="text-[11px] uppercase tracking-[0.12em] text-ink-dim">Sources</div>
              <div className="space-y-1.5">
                {docs.map((doc: any) => {
                  const docId = String(doc.id ?? doc.doc_id ?? '')
                  const selected = selectedDocs.has(docId)
                  return (
                    <button
                      key={docId}
                      type="button"
                      onClick={() => toggleDoc(docId)}
                      className={`w-full rounded-md border px-3 py-2 text-left transition-colors ${
                        selected
                          ? 'border-accent/60 bg-accent/10 text-ink'
                          : 'border-border-subtle bg-bg/80 text-ink-muted hover:text-ink'
                      }`}
                    >
                      <div className="flex items-center gap-2">
                        <span className={`h-2 w-2 rounded-full ${selected ? 'bg-accent' : 'bg-border'}`} />
                        <span className="truncate text-sm font-medium">{doc.filename ?? doc.name ?? doc.title ?? 'Document'}</span>
                      </div>
                    </button>
                  )
                })}
              </div>
            </div>
          )}

          {artifacts.length > 0 && (
            <div className="space-y-2">
              <div className="text-[11px] uppercase tracking-[0.12em] text-ink-dim">Workspace Objects</div>
              <div className="space-y-1.5">
                {artifacts.map((artifact: any) => {
                  const objectId = String(artifact.object_id ?? artifact.id ?? '')
                  const selected = selectedArtifacts.has(objectId)
                  return (
                    <button
                      key={objectId}
                      type="button"
                      onClick={() => toggleArtifact(objectId)}
                      className={`w-full rounded-md border px-3 py-2 text-left transition-colors ${
                        selected
                          ? 'border-accent/60 bg-accent/10 text-ink'
                          : 'border-border-subtle bg-bg/80 text-ink-muted hover:text-ink'
                      }`}
                    >
                      <div className="flex items-center gap-2">
                        <span className={`h-2 w-2 rounded-full ${selected ? 'bg-accent' : 'bg-border'}`} />
                        <span className="truncate text-sm font-medium">{artifact.title ?? objectId}</span>
                        {artifact.object_type && (
                          <span className="ml-auto text-[10px] uppercase tracking-[0.12em] text-ink-dim">
                            {String(artifact.object_type).replace(/_/g, ' ')}
                          </span>
                        )}
                      </div>
                    </button>
                  )
                })}
              </div>
            </div>
          )}

          <div className="space-y-2">
            <div className="text-[11px] uppercase tracking-[0.12em] text-ink-dim">Focus</div>
            <div className="flex flex-wrap gap-2">
              {focusOptions.map(area => {
                const selected = selectedFocus.has(area)
                return (
                  <button
                    key={area}
                    type="button"
                    onClick={() => toggleFocus(area)}
                    className={`rounded-md border px-2.5 py-1.5 text-xs transition-colors ${
                      selected
                        ? 'border-accent/60 bg-accent/10 text-ink'
                        : 'border-border-subtle bg-bg text-ink-muted hover:text-ink'
                    }`}
                  >
                    {area.replace(/_/g, ' ')}
                  </button>
                )
              })}
            </div>
          </div>

          <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
            <label className="space-y-1">
              <span className="text-[11px] uppercase tracking-[0.12em] text-ink-dim">Audience</span>
              <select
                value={String(output.audience ?? 'analyst')}
                onChange={e => updateOutput({ audience: e.target.value })}
                className="w-full rounded-md border border-border-subtle bg-bg px-3 py-2 text-sm text-ink outline-none focus:border-accent"
              >
                <option value="analyst">Analyst</option>
                <option value="investment_committee">Investment committee</option>
                <option value="client">Client</option>
              </select>
            </label>
            <label className="space-y-1">
              <span className="text-[11px] uppercase tracking-[0.12em] text-ink-dim">Depth</span>
              <select
                value={String(output.depth ?? 'full')}
                onChange={e => updateOutput({ depth: e.target.value })}
                className="w-full rounded-md border border-border-subtle bg-bg px-3 py-2 text-sm text-ink outline-none focus:border-accent"
              >
                <option value="quick">Quick</option>
                <option value="full">Full</option>
              </select>
            </label>
            <label className="space-y-1">
              <span className="text-[11px] uppercase tracking-[0.12em] text-ink-dim">Format</span>
              <select
                value={String(output.format ?? 'report')}
                onChange={e => updateOutput({ format: e.target.value })}
                className="w-full rounded-md border border-border-subtle bg-bg px-3 py-2 text-sm text-ink outline-none focus:border-accent"
              >
                <option value="report">Report</option>
                <option value="memo">Memo</option>
                <option value="deck_input">Deck input</option>
              </select>
            </label>
          </div>

          <div className="flex items-center justify-end gap-2 pt-1">
            <button
              type="button"
              onClick={() => onSubmit?.(context, false)}
              className="rounded-md border border-border-subtle px-3 py-2 text-sm text-ink-muted hover:text-ink"
            >
              Cancel
            </button>
            <button
              type="button"
              onClick={() => onSubmit?.(context, true)}
              className="rounded-md border border-accent/60 bg-accent/15 px-3 py-2 text-sm font-medium text-ink hover:bg-accent/20"
            >
              Run {workflowLabel}
            </button>
          </div>
        </div>
      </div>
    </div>
  )
}

function PlanReviewCard({
  steps,
  onApprove,
  onReject,
}: {
  steps: StepState[]
  onApprove?: () => void
  onReject?: () => void
}) {
  return (
    <div className="flex justify-start animate-fade-up">
      <div className="w-full max-w-[760px] min-w-0">
        <AgentLabel />
        <div className="mt-2 overflow-hidden rounded-lg border border-border bg-surface/35">
          <div className="border-b border-border px-4 py-3">
            <div className="text-sm font-semibold text-ink">Research plan ready</div>
            <div className="mt-1 text-xs text-ink-muted">Review scope before execution starts.</div>
          </div>
          <div className="px-4 py-3">
            <ResearchStepsTrace steps={steps} defaultOpen />
          </div>
          <div className="flex items-center justify-end gap-2 border-t border-border px-4 py-3">
            <button
              type="button"
              onClick={onReject}
              className="rounded-md border border-border-subtle px-3 py-2 text-sm text-ink-muted hover:text-ink"
            >
              Reject
            </button>
            <button
              type="button"
              onClick={onApprove}
              className="rounded-md border border-accent/60 bg-accent/15 px-3 py-2 text-sm font-medium text-ink hover:bg-accent/20"
            >
              Approve and run
            </button>
          </div>
        </div>
      </div>
    </div>
  )
}

interface Props {
  session: Session | undefined
  activeRun: AgentRunState
  mode: Mode
  onModeChange: (mode: Mode) => void
  onSubmit: (query: string, mode: Mode) => void
  onUpload?: (file: File) => void
  docs?: DocumentInfo[]
  selectedDocId?: string | null
  onSelectDoc?: (docId: string) => void
  onRemoveDoc?: (docId: string) => void
  workspaceObjects?: WorkspaceObject[]
  disabled?: boolean
  onOpenDeckPreview?: (filename: string, deckTitle: string | undefined, threadId: string) => void
  onWorkflowContextSubmit?: (context: Record<string, any>, approved?: boolean) => void
  onApprovePlan?: () => void
  onRejectPlan?: () => void
  /**
   * Optional handler for amending a previously-sent user message. When omitted,
   * the edit affordance on user bubbles is hidden. Receives the index of the
   * message in `session.messages` and the new content typed by the user.
   */
  onAmendMessage?: (messageIndex: number, originalContent: string, newContent: string) => void
}

export function MessageThread({
  session,
  activeRun,
  mode,
  onModeChange,
  onSubmit,
  onUpload,
  docs = [],
  selectedDocId,
  onSelectDoc,
  onRemoveDoc,
  workspaceObjects = [],
  disabled,
  onOpenDeckPreview,
  onWorkflowContextSubmit,
  onApprovePlan,
  onRejectPlan,
  onAmendMessage,
}: Props) {
  const bottomRef = useRef<HTMLDivElement>(null)
  const messages = session?.messages ?? []

  const runActive = !['idle', 'complete', 'error', 'rejected'].includes(activeRun.status)
  const isResearchRun = activeRun.resolved_intent === 'research' || (
    runActive && !['chat_responding', 'classifying'].includes(activeRun.status)
      && activeRun.resolved_intent !== 'chat'
  )
  const isChatRun = activeRun.resolved_intent === 'chat' || activeRun.status === 'chat_responding'
  const isSynthesizing = activeRun.status === 'synthesizing'

  const showResearchStatus =
    runActive && isResearchRun && !isSynthesizing &&
    ['classifying', 'planning', 'workflow_running'].includes(activeRun.status)

  // Live chat messages (streaming)
  const liveChatMessages = isChatRun ? activeRun.chat_messages : []
  const lastCommittedUser = [...messages].reverse().find(m => m.type === 'user')
  const showPendingUser =
    runActive &&
    !!activeRun.query &&
    lastCommittedUser?.content !== activeRun.query

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages.length, runActive, isSynthesizing, showPendingUser])

  const isInputBusy =
    disabled ||
    activeRun.status === 'classifying' ||
    activeRun.status === 'planning' ||
    activeRun.status === 'awaiting_approval' ||
    activeRun.status === 'executing' ||
    activeRun.status === 'synthesizing' ||
    activeRun.status === 'awaiting_workflow_context' ||
    activeRun.status === 'awaiting_assumptions' ||
    activeRun.status === 'awaiting_outline_review' ||
    activeRun.status === 'awaiting_memo_review' ||
    activeRun.status === 'chat_responding'

  const isEmpty = messages.length === 0 && !runActive

  return (
    <div className="flex-1 flex flex-col overflow-hidden min-w-0">

      {/* Message list */}
      <div className="flex-1 overflow-y-auto">
        {isEmpty ? (
          <EmptyState
            mode={mode}
            onSubmit={onSubmit}
            onModeChange={onModeChange}
            onUpload={onUpload}
            docs={docs}
            selectedDocId={selectedDocId}
            onSelectDoc={onSelectDoc}
            onRemoveDoc={onRemoveDoc}
            workspaceObjects={workspaceObjects}
          />
        ) : (
          <div className="max-w-3xl mx-auto px-6 py-8 space-y-6">
            {/* Committed session messages */}
            {messages.map((msg, idx) => (
              <CommittedMessage
                key={msg.id}
                msg={msg}
                onOpenDeckPreview={onOpenDeckPreview}
                onSelectDoc={onSelectDoc}
                onEdit={
                  onAmendMessage && !runActive && msg.type === 'user'
                    ? (newContent) => onAmendMessage(idx, msg.content, newContent)
                    : undefined
                }
              />
            ))}

            {/* Live: user message before session commit is visible.
                Hide [DCF_APPROVED] approval triggers — the confirmed card and
                activity trace make the action visible without a text bubble. */}
            {showPendingUser && !activeRun.query.startsWith('[DCF_APPROVED]') && (
              <UserBubble content={activeRun.query} />
            )}

            {activeRun.workflow_context_review && (
              <WorkflowSetupCard
                review={activeRun.workflow_context_review}
                onSubmit={onWorkflowContextSubmit}
              />
            )}

            {activeRun.status === 'awaiting_approval' && activeRun.steps.length > 0 && (
              <PlanReviewCard
                steps={activeRun.steps}
                onApprove={onApprovePlan}
                onReject={onRejectPlan}
              />
            )}

            {/* Live: chat streaming — chat-scoped activities feed the bubble. */}
            {isChatRun && liveChatMessages.map((m, idx) => {
              if (m.role !== 'assistant') return null
              const assistantMsgs = liveChatMessages.filter(x => x.role === 'assistant')
              const isLast = idx === liveChatMessages.length - 1 || m.id === assistantMsgs[assistantMsgs.length - 1]?.id
              // Skip stale empty messages from prior sub-runs (e.g. HITL run that
              // produced no text). They would show a spurious ThinkingDots above
              // the active bubble.
              if (!m.content && !isLast) return null
              const chatActivities = isLast
                ? activeRun.activity.filter(a => a.scope === 'chat' || a.scope === 'workflow')
                : []
              const hasDcf = isLast && !!activeRun.dcf_review
              const prevMsg = liveChatMessages[idx - 1]
              const hideLabel = !!prevMsg && prevMsg.role === 'assistant'
              // Surface DCF validity from the live workflow activity so the
              // degraded banner shows up before the message is committed.
              let liveValidity: 'valid' | 'invalid' | 'adjusting' | undefined
              let liveInvalidationReason: string | undefined
              if (isLast) {
                const wf = activeRun.activity.find(
                  a => a.kind === 'workflow' && a.meta && typeof a.meta === 'object',
                )
                const meta = (wf?.meta ?? {}) as Record<string, unknown>
                if (typeof meta.model_validity === 'string') {
                  liveValidity = meta.model_validity as 'valid' | 'invalid' | 'adjusting'
                }
                if (typeof meta.invalidation_reason === 'string') {
                  liveInvalidationReason = meta.invalidation_reason
                }
              }
              return (
                <ChatBubble
                  key={m.id}
                  content={m.content}
                  streaming={m.streaming}
                  activities={chatActivities.length > 0 ? chatActivities : undefined}
                  dcfReview={hasDcf ? activeRun.dcf_review! : undefined}
                  onDcfApprove={hasDcf ? () => {
                    // DcfHitlSection calls /dcf-decision endpoint directly when threadId set.
                    // This callback is a no-op fallback for missing threadId.
                  } : undefined}
                  onDcfReject={hasDcf ? () => {
                    // DcfHitlSection calls /dcf-decision endpoint directly when threadId set.
                    // No-op fallback.
                  } : undefined}
                  threadId={activeRun.thread_id || undefined}
                  artifactPaths={activeRun.artifact_paths.length ? activeRun.artifact_paths : undefined}
                  dcfEvidenceItems={activeRun.dcf_evidence_items}
                  dcfCitationMap={activeRun.dcf_citation_map}
                  hideLabel={hideLabel}
                  validity={liveValidity}
                  invalidationReason={liveInvalidationReason}
                  onOpenDeckPreview={onOpenDeckPreview}
                />
              )
            })}

            {activeRun.deck_review && activeRun.thread_id && (
              <div className="flex justify-start animate-fade-up">
                <div className="w-full max-w-[760px] pl-1">
                  <DeckOutlineReview review={activeRun.deck_review} threadId={activeRun.thread_id} />
                </div>
              </div>
            )}

            {activeRun.memo_review && activeRun.thread_id && (
              <div className="flex justify-start animate-fade-up">
                <div className="w-full max-w-[760px] pl-1">
                  <MemoDraftReview review={activeRun.memo_review} threadId={activeRun.thread_id} />
                </div>
              </div>
            )}

            {/* Live: research status (planning / executing) */}
            {showResearchStatus && <ResearchStatusCard run={activeRun} />}

            {/* Live: synthesis streaming — report builds in-thread */}
            {isSynthesizing && (
              <ResearchReportCard
                content={activeRun.report}
                streaming={true}
                steps={activeRun.steps}
                activity={activeRun.activity}
              />
            )}

            <div ref={bottomRef} />
          </div>
        )}
      </div>

      {/* Input bar */}
      {!isEmpty && (
        <div className="border-t border-border-subtle px-4 pt-3 pb-4 bg-bg flex-shrink-0 overflow-visible">
          <div className="max-w-3xl mx-auto">
            <QueryInput
              onSubmit={onSubmit}
              onUpload={onUpload}
              disabled={isInputBusy}
              mode={mode}
              onModeChange={onModeChange}
              docs={docs}
              selectedDocId={selectedDocId}
              onSelectDoc={onSelectDoc}
              onRemoveDoc={onRemoveDoc}
            />
          </div>
        </div>
      )}
    </div>
  )
}

// ── Empty / hero state ────────────────────────────────────────────────────────

function EmptyState({
  mode,
  onSubmit,
  onModeChange,
  onUpload,
  docs = [],
  selectedDocId,
  onSelectDoc,
  onRemoveDoc,
  workspaceObjects = [],
}: {
  mode: Mode
  onSubmit: (query: string, mode: Mode) => void
  onModeChange: (mode: Mode) => void
  onUpload?: (file: File) => void
  docs?: DocumentInfo[]
  selectedDocId?: string | null
  onSelectDoc?: (docId: string) => void
  onRemoveDoc?: (docId: string) => void
  workspaceObjects?: WorkspaceObject[]
}) {
  const launcherFileRef = useRef<HTMLInputElement>(null)
  const docsReady = docs.length > 0 && docs.every(doc => doc.status === 'ready')
  const hasWorkspaceContext = workspaceObjects.length > 0 || docsReady
  type LauncherTask = {
    title: string
    label: string
    description: string
    prompt: string
    mode: Mode
    action?: 'upload_or_prompt'
    disabled?: boolean
    requiresContext?: boolean
  }
  const tasks: LauncherTask[] = [
    {
      title: 'Analyze document',
      label: 'RAG',
      description: 'Upload or use selected documents for cited analysis.',
      prompt: 'Analyze the selected document and summarize key financial signals with citations.',
      mode: 'chat' as Mode,
      action: 'upload_or_prompt',
    },
    {
      title: 'Run DCF',
      label: 'Valuation',
      description: 'Launch setup gate, review assumptions, then save DCF run object.',
      prompt: 'Run a DCF analysis for ',
      mode: 'chat' as Mode,
    },
    {
      title: 'Draft memo',
      label: 'Writing',
      description: 'Select workspace sources, audience, and memo depth before drafting.',
      prompt: 'Draft an investment memo using the current workspace context.',
      mode: 'chat' as Mode,
      requiresContext: true,
    },
    {
      title: 'Create deck',
      label: 'Slides',
      description: 'Select source objects and target audience before building slides.',
      prompt: 'Create a presentation deck from the current workspace context.',
      mode: 'chat' as Mode,
      requiresContext: true,
    },
    {
      title: 'Compare companies',
      label: 'Comparison',
      description: 'Select saved analyses or documents, then choose comparison focus.',
      prompt: 'Compare selected companies or saved analyses using financial metrics, drivers, and valuation context.',
      mode: 'chat' as Mode,
      requiresContext: true,
    },
  ]

  return (
    <div className="flex-1 flex flex-col items-center justify-center min-h-full px-6 py-12 space-y-7">
      <div className="w-full max-w-3xl space-y-2">
        <div className="text-[11px] uppercase tracking-[0.16em] text-ink-dim">Task launcher</div>
        <h2 className="text-xl font-medium text-ink tracking-tight">Start from a workflow</h2>
        <p className="max-w-xl text-sm text-ink-muted">
          Pick a task or ask directly. Workflow setup uses current chat, selected documents, and saved objects.
        </p>
      </div>

      <div className="grid w-full max-w-3xl grid-cols-1 gap-2 sm:grid-cols-2">
        {tasks.map(task => {
          const blockedForIndexing = task.action === 'upload_or_prompt' && docs.length > 0 && !docsReady
          const missingContext = Boolean(task.requiresContext && !hasWorkspaceContext)
          const taskDisabled = Boolean(task.disabled || blockedForIndexing || missingContext)
          return (
          <button
            key={task.title}
            type="button"
            disabled={taskDisabled}
            onClick={() => {
              if (taskDisabled) return
              if (task.action === 'upload_or_prompt' && docs.length === 0) {
                launcherFileRef.current?.click()
                return
              }
              onModeChange(task.mode)
              onSubmit(task.prompt, task.mode)
            }}
            className={`
              rounded-lg border border-border-subtle bg-surface/30 p-3 text-left transition-colors
              focus:outline-none focus:ring-1 focus:ring-border-hover
              ${task.disabled || missingContext
                ? 'cursor-not-allowed opacity-45'
                : blockedForIndexing
                  ? 'cursor-wait opacity-60'
                  : 'hover:border-border-hover hover:bg-surface/50'}
            `}
          >
            <div className="flex items-center gap-2">
              <span className="text-sm font-medium text-ink">{task.title}</span>
              <span className="rounded bg-bg px-1.5 py-0.5 text-[10px] uppercase tracking-[0.12em] text-ink-dim">
                {task.label}
              </span>
              {(task.disabled || blockedForIndexing || missingContext) && (
                <span className="ml-auto rounded bg-bg px-1.5 py-0.5 text-[10px] uppercase tracking-[0.12em] text-ink-disabled">
                  {blockedForIndexing ? 'Indexing' : missingContext ? 'Needs source' : 'Setup needed'}
                </span>
              )}
            </div>
            <p className="mt-1 text-xs leading-5 text-ink-muted">
              {blockedForIndexing
                ? 'Document uploaded. Waiting for parsing and embeddings to finish.'
                : missingContext
                ? 'Upload a document or create a workspace object first.'
                : task.action === 'upload_or_prompt' && docs.length === 0
                ? 'Upload a PDF, spreadsheet, or transcript to start document analysis.'
                : task.description}
            </p>
          </button>
          )
        })}
      </div>

      <input
        ref={launcherFileRef}
        type="file"
        className="hidden"
        accept=".pdf,.docx,.doc,.xlsx,.xls,.csv,.txt,.md"
        onChange={e => {
          const file = e.target.files?.[0]
          if (file && onUpload) onUpload(file)
          e.currentTarget.value = ''
        }}
      />

      <div className="w-full max-w-3xl">
        <QueryInput
          onSubmit={onSubmit}
          onUpload={onUpload}
          disabled={false}
          autoFocus
          mode={mode}
          onModeChange={onModeChange}
          docs={docs}
          selectedDocId={selectedDocId}
          onSelectDoc={onSelectDoc}
          onRemoveDoc={onRemoveDoc}
        />
      </div>
    </div>
  )
}
