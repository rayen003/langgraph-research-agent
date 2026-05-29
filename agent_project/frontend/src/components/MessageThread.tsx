import { useEffect, useRef, useState } from 'react'
import { MarkdownRenderer } from './MarkdownRenderer'
import { QueryInput } from './QueryInput'
import { ActivityTrace, ResearchStepsTrace } from './ActivityTrace'
import type { ActivityEntry } from '../lib/activity'
import type { AgentRunState, DocumentInfo, DcfReviewState, EvidenceItem, Mode, Session, SessionMessage, StepState, ToolCall } from '../types'

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
    <div className="rounded-lg border border-[#1e1e2a] bg-[#07070f] px-4 py-3 space-y-2">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="text-sm font-medium text-zinc-200 truncate">{deckTitle}</p>
          <p className="text-[11px] text-zinc-600">PowerPoint deck · {filename}</p>
        </div>
        <div className="flex items-center gap-2 flex-shrink-0">
          <button
            type="button"
            onClick={() => onPreview?.(filename, deckTitle, threadId)}
            className="px-2.5 py-1 rounded-md border border-indigo-500/40 text-[11px] text-indigo-300 hover:bg-indigo-500/10 transition-colors"
          >
            Preview
          </button>
          <button
            type="button"
            onClick={handleDownload}
            disabled={busy}
            className="px-2.5 py-1 rounded-md border border-[#252535] text-[11px] text-zinc-300 hover:text-zinc-100 hover:border-[#33334a] transition-colors disabled:opacity-50"
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
          className="bg-transparent text-[11px] text-zinc-300 px-2 py-1 border-r border-zinc-700 outline-none cursor-pointer hover:text-zinc-100"
          aria-label="Report format"
        >
          <option value="pdf">PDF</option>
          <option value="md">Markdown</option>
        </select>
        <button
          type="button"
          onClick={handleDownload}
          disabled={busy}
          className="inline-flex items-center gap-1.5 px-2.5 py-1 text-[11px] font-medium text-zinc-300 hover:text-zinc-100 hover:bg-zinc-800/80 transition-colors disabled:opacity-50"
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
      <aside className="h-full w-full max-w-md border-l border-[#24242a] bg-[#08080b] shadow-2xl flex flex-col">
        <div className="flex items-start justify-between gap-3 border-b border-[#1d1d22] px-5 py-4">
          <div className="min-w-0">
            <div className="text-[11px] uppercase tracking-wide text-indigo-300">Source [{citationNumber}]</div>
            <h2 className="mt-1 text-sm font-semibold text-zinc-100 leading-snug break-words">{title}</h2>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="rounded-md px-2 py-1 text-sm text-zinc-500 hover:bg-zinc-900 hover:text-zinc-200"
            aria-label="Close source drawer"
          >
            ×
          </button>
        </div>

        <div className="flex-1 overflow-y-auto px-5 py-4 space-y-4">
          {!evidence ? (
            <p className="text-sm text-zinc-400">No source metadata was available for this citation.</p>
          ) : (
            <>
              <div className="rounded-lg border border-[#1d1d22] bg-[#0d0d11] p-3 space-y-2">
                <div className="flex flex-wrap gap-2 text-[10px]">
                  <span className="rounded bg-indigo-500/10 px-2 py-0.5 font-medium text-indigo-300">{tier.replace('_', ' ')}</span>
                  <span className="rounded bg-zinc-900 px-2 py-0.5 text-zinc-400">{evidence.kind}</span>
                  {'inferred' in evidence && evidence.inferred && (
                    <span className="rounded bg-amber-500/10 px-2 py-0.5 text-amber-300">metadata inferred</span>
                  )}
                  {evidence.as_of && <span className="rounded bg-zinc-900 px-2 py-0.5 text-zinc-400">{evidence.as_of.slice(0, 10)}</span>}
                </div>
                {value && (
                  <div>
                    <div className="text-[11px] text-zinc-500">Value used</div>
                    <div className="text-lg font-semibold text-zinc-100">{value}</div>
                  </div>
                )}
                {evidence.field && (
                  <div className="text-xs text-zinc-400">
                    Field: <span className="font-mono text-zinc-200">{evidence.field}</span>
                  </div>
                )}
                <div className="text-xs text-zinc-500 break-all">Evidence ID: {evidence.evidence_id}</div>
              </div>

              {evidence.text && !evidence.inferred && (
                <div className="rounded-lg border border-[#1d1d22] bg-[#0b0b0e] p-3">
                  <div className="mb-2 text-[11px] font-medium uppercase tracking-wide text-zinc-500">Excerpt</div>
                  <p className="whitespace-pre-wrap break-words text-sm leading-relaxed text-zinc-300">{evidence.text}</p>
                </div>
              )}

              {evidence.inferred && !evidence.url && (
                <p className="text-sm text-zinc-400">
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
                <div className="rounded-lg border border-[#1d1d22] bg-[#0b0b0e] p-3">
                  <button
                    type="button"
                    onClick={loadRawData}
                    className="text-sm font-medium text-zinc-200 hover:text-white"
                  >
                    {rawOpen ? 'Underlying FMP API data' : 'View underlying FMP API data'}
                  </button>
                  {rawOpen && (
                    <div className="mt-3">
                      {loadingRaw && <p className="text-xs text-zinc-500">Loading source data…</p>}
                      {rawError && <p className="text-xs text-red-300">{rawError}</p>}
                      {rawData && (
                        <pre className="max-h-72 overflow-auto rounded-md bg-black/40 p-3 text-[10px] leading-relaxed text-zinc-400">
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
  onEdit,
}: {
  content: string
  onEdit?: (newContent: string) => void
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
            className="w-full px-4 py-2.5 rounded-2xl bg-[#1a1a24] border border-indigo-500/40 text-sm text-zinc-100 leading-relaxed resize-none focus:outline-none focus:border-indigo-400"
          />
          <div className="flex items-center justify-end gap-2 text-[11px]">
            <span className="text-zinc-600 mr-auto">⌘/Ctrl + Enter to send · Esc to cancel</span>
            <button
              type="button"
              onClick={() => { setDraft(content); setEditing(false) }}
              className="px-2.5 py-1 rounded-md border border-[#252535] text-zinc-400 hover:text-zinc-200 hover:border-[#33334a]"
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
    <div className="group flex justify-end items-start gap-1.5 animate-fade-up">
      {onEdit && (
        <button
          type="button"
          onClick={() => { setDraft(content); setEditing(true) }}
          title="Edit message"
          className="mt-1 opacity-0 group-hover:opacity-100 transition-opacity w-7 h-7 flex items-center justify-center rounded-md text-zinc-500 hover:text-zinc-200 hover:bg-[#1a1a22]"
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
      <div className="max-w-[72%] px-4 py-2.5 rounded-2xl rounded-tr-sm bg-[#1a1a24] border border-[#252535]">
        <p className="text-sm text-zinc-200 leading-relaxed whitespace-pre-wrap">{content}</p>
      </div>
    </div>
  )
}

function AgentLabel() {
  return (
    <div className="flex items-center gap-1.5 mb-1.5">
      <div className="w-4 h-4 rounded-md bg-indigo-500/20 border border-indigo-500/30 flex items-center justify-center flex-shrink-0">
        <svg width="8" height="8" viewBox="0 0 12 12" fill="none">
          <path d="M2 9L5 3L8 7L10 4" stroke="#818cf8" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      </div>
      <span className="text-[11px] text-zinc-600 font-medium">Agent</span>
    </div>
  )
}

function DegradedBanner({ reason }: { reason?: string }) {
  return (
    <div className="rounded-md border border-red-500/40 bg-red-500/10 px-3 py-2 text-[12px] text-red-200">
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
                <MarkdownRenderer content={content} streaming={streaming} />
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
  const evidenceById = new Map((evidenceItems ?? []).map(item => [item.evidence_id, item]))
  const ticker = content.match(/^# DCF Valuation:\s*([A-Z0-9.-]+)/)?.[1] ?? ''
  const linkifiedPreChart = linkifyCitations(preChart, citationMap)
  const linkifiedPostChart = linkifyCitations(postChart, citationMap)
  const openEvidence = openCitation && citationMap ? evidenceById.get(citationMap[openCitation]) : undefined

  return (
    <div className="rounded-xl border border-[#1e1e1e] bg-[#080808] px-6 py-5">
      <MarkdownRenderer content={linkifiedPreChart} streaming={false} onCitationClick={setOpenCitation} />

      {hasMarker && sensitivityImage && threadId && (
        <figure className="my-5 space-y-2">
          <img
            src={`/artifacts/${threadId}/${sensitivityImage.split('/').pop()}`}
            alt="Sensitivity heatmap"
            className="w-full max-w-xl rounded-lg border border-[#1e1e1e]"
          />
          <figcaption className="text-[11px] text-zinc-500">
            WACC × terminal growth sensitivity
          </figcaption>
        </figure>
      )}

      {postChart && <MarkdownRenderer content={linkifiedPostChart} streaming={false} onCitationClick={setOpenCitation} />}

      {canDownload && (
        <div className="mt-6 pt-4 border-t border-[#1e1e1e] flex justify-end">
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
            rounded-xl border border-[#1e1e1e] bg-[#080808] px-6 py-5
            ${streaming ? '' : ''}
          `}
        >
          <MarkdownRenderer content={before} streaming={streaming && !markerPresent && !after} />

          {hasArtifacts && markerPresent && (
            <ArtifactImages artifactPaths={artifactPaths!} threadId={threadId!} />
          )}

          {after && <MarkdownRenderer content={after} streaming={streaming} />}

          {hasArtifacts && !markerPresent && (
            <ArtifactImages artifactPaths={artifactPaths!} threadId={threadId!} />
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
              className="rounded-xl border border-[#2a2a2a] max-w-full"
            />
            <figcaption className="text-xs text-zinc-600 text-center">{label}</figcaption>
          </figure>
        )
      })}
    </div>
  )
}

/** Status card shown during research planning/executing (before synthesis). */
function ResearchStatusCard({ run }: { run: AgentRunState }) {
  const { status, steps, completed_steps } = run
  const total = steps.length
  const running = steps.find(s => s.status === 'running')

  let label = ''
  if (status === 'classifying') label = 'Classifying intent…'
  else if (status === 'planning') label = 'Building research plan…'
  else if (status === 'awaiting_approval') label = 'Plan ready — review in the sidebar'
  else if (status === 'workflow_running') label = 'Running deterministic workflow…'
  else if (status === 'awaiting_assumptions') label = 'Assumptions ready — review in the sidebar'
  else if (status === 'awaiting_outline_review') label = 'Deck outline ready — review in the sidebar'
  else if (status === 'executing') {
    label = running
      ? `Step ${completed_steps + 1}/${total} — ${running.description.length > 55 ? running.description.slice(0, 55) + '…' : running.description}`
      : `Executing step ${completed_steps + 1}/${total}…`
  }

  return (
    <div className="flex justify-start animate-fade-up">
      <div className="max-w-[85%]">
        <AgentLabel />
        <div className="pl-1 flex items-center gap-2.5 py-2">
          <div
            className={`
              w-1.5 h-1.5 rounded-full flex-shrink-0
              ${status === 'awaiting_approval' ? 'bg-amber-500' : 'bg-indigo-500 animate-pulse'}
            `}
          />
          <span className="text-sm text-zinc-500">{label}</span>
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
          className="w-1.5 h-1.5 rounded-full bg-zinc-600 animate-pulse"
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
}: {
  msg: SessionMessage
  onOpenDeckPreview?: (filename: string, deckTitle: string | undefined, threadId: string) => void
  onEdit?: (newContent: string) => void
}) {
  if (msg.type === 'user') {
    if (msg.content.startsWith('[DCF_APPROVED]')) return null
    if (msg.content.startsWith('[DECK_COMPLETE]')) return null
    return <UserBubble content={msg.content} onEdit={onEdit} />
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

// ── Document card (attachment) ────────────────────────────────────────────────

const FILE_COLORS: Record<string, { bg: string; border: string; text: string; label: string }> = {
  pdf:  { bg: 'bg-red-500/15',    border: 'border-red-500/25',    text: 'text-red-400',    label: 'PDF'  },
  docx: { bg: 'bg-blue-500/15',   border: 'border-blue-500/25',   text: 'text-blue-400',   label: 'Word' },
  doc:  { bg: 'bg-blue-500/15',   border: 'border-blue-500/25',   text: 'text-blue-400',   label: 'Word' },
  xlsx: { bg: 'bg-emerald-500/15',border: 'border-emerald-500/25',text: 'text-emerald-400',label: 'Excel'},
  xls:  { bg: 'bg-emerald-500/15',border: 'border-emerald-500/25',text: 'text-emerald-400',label: 'Excel'},
  csv:  { bg: 'bg-teal-500/15',   border: 'border-teal-500/25',   text: 'text-teal-400',   label: 'CSV'  },
  txt:  { bg: 'bg-zinc-500/15',   border: 'border-zinc-500/25',   text: 'text-zinc-400',   label: 'Text' },
  md:   { bg: 'bg-zinc-500/15',   border: 'border-zinc-500/25',   text: 'text-zinc-400',   label: 'MD'   },
}

function DocumentCard({
  doc,
  selected,
  onSelect,
  onRemove,
}: {
  doc: DocumentInfo
  selected?: boolean
  onSelect?: (id: string) => void
  onRemove?: (id: string) => void
}) {
  const ext = doc.filename.split('.').pop()?.toLowerCase() ?? ''
  const color = FILE_COLORS[ext] ?? { bg: 'bg-zinc-500/15', border: 'border-zinc-500/25', text: 'text-zinc-400', label: ext.toUpperCase() }
  const isProcessing = doc.status === 'processing'
  const isError = doc.status === 'error'
  const name = doc.filename.length > 22 ? doc.filename.slice(0, 20) + '…' : doc.filename
  const clickable = !isProcessing && !isError && !!onSelect

  return (
    <div
      className={`
        relative flex items-center gap-2.5 px-3 py-2.5 rounded-xl border
        transition-colors duration-150 cursor-default select-none
        ${isError
          ? 'border-red-900/40 bg-red-950/20'
          : selected
            ? 'border-indigo-600/50 bg-indigo-950/20'
            : 'border-[#252535] bg-[#111118] hover:border-[#33334a]'
        }
      `}
      style={{ minWidth: 140, maxWidth: 200 }}
    >
      {/* File type icon */}
      <button
        type="button"
        onClick={() => clickable && onSelect?.(doc.doc_id)}
        disabled={!clickable}
        className={`flex items-center gap-2.5 min-w-0 flex-1 ${clickable ? 'cursor-pointer' : 'cursor-default'}`}
      >
        <div className={`w-9 h-9 rounded-lg ${color.bg} border ${color.border} flex items-center justify-center flex-shrink-0`}>
          {isProcessing ? (
            <span className="w-3 h-3 rounded-full border-2 border-current border-t-transparent animate-spin" style={{ color: color.text.replace('text-', '') }} />
          ) : (
            <svg width="16" height="16" viewBox="0 0 14 14" fill="none" className={color.text}>
              <path d="M2.5 1.5h6L11 4.5v8H2.5v-11z" stroke="currentColor" strokeWidth="1.2" strokeLinejoin="round" />
              <path d="M8 1.5V5h2.5" stroke="currentColor" strokeWidth="1.2" strokeLinejoin="round" />
            </svg>
          )}
        </div>

        {/* Name + type */}
        <div className="min-w-0 text-left">
          <p className={`text-xs font-medium truncate leading-tight ${isError ? 'text-red-400' : selected ? 'text-zinc-100' : 'text-zinc-200'}`}>
            {name}
          </p>
          <p className="text-[11px] text-zinc-500 leading-tight mt-0.5">
            {isProcessing ? 'Processing…' : isError ? 'Error' : color.label}
          </p>
        </div>
      </button>

      {/* Remove button */}
      {onRemove && (
        <button
          type="button"
          onClick={(e) => { e.stopPropagation(); onRemove(doc.doc_id) }}
          className="absolute top-1.5 right-1.5 w-4 h-4 rounded-full bg-[#1a1a22] border border-[#2a2a38] flex items-center justify-center text-zinc-500 hover:text-zinc-200 hover:bg-[#252535] transition-colors"
          title="Remove"
        >
          <svg width="7" height="7" viewBox="0 0 7 7" fill="none">
            <path d="M1 1l5 5M6 1L1 6" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" />
          </svg>
        </button>
      )}
    </div>
  )
}

// ── Main component ────────────────────────────────────────────────────────────

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
  disabled?: boolean
  onOpenDeckPreview?: (filename: string, deckTitle: string | undefined, threadId: string) => void
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
  disabled,
  onOpenDeckPreview,
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

  // Auto-scroll on new content
  useEffect(() => {
    if (runActive || isSynthesizing) {
      bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
    }
  }, [activeRun.report, liveChatMessages.length, runActive, isSynthesizing, showPendingUser])

  const isInputBusy =
    disabled ||
    activeRun.status === 'classifying' ||
    activeRun.status === 'planning' ||
    activeRun.status === 'executing' ||
    activeRun.status === 'synthesizing' ||
    activeRun.status === 'chat_responding'

  const isEmpty = messages.length === 0 && !runActive

  return (
    <div className="flex-1 flex flex-col overflow-hidden min-w-0">

      {/* Message list */}
      <div className="flex-1 overflow-y-auto">
        {isEmpty ? (
          <EmptyState mode={mode} onSubmit={onSubmit} onModeChange={onModeChange} onUpload={onUpload} />
        ) : (
          <div className="max-w-3xl mx-auto px-6 py-8 space-y-6">
            {/* Committed session messages */}
            {messages.map((msg, idx) => (
              <CommittedMessage
                key={msg.id}
                msg={msg}
                onOpenDeckPreview={onOpenDeckPreview}
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

            {/* Live: chat streaming — chat-scoped activities feed the bubble. */}
            {isChatRun && liveChatMessages.map((m, idx) => {
              if (m.role !== 'assistant') return null
              const assistantMsgs = liveChatMessages.filter(x => x.role === 'assistant')
              const isLast = idx === liveChatMessages.length - 1 || m.id === assistantMsgs[assistantMsgs.length - 1]?.id
              // Skip stale empty messages from prior sub-runs (e.g. HITL run that
              // produced no text). They would show a spurious ThinkingDots above
              // the active bubble.
              if (!m.content && !isLast) return null
              const chatActivities = isLast ? activeRun.activity.filter(a => a.scope === 'chat') : []
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
        <div className="border-t border-[#141414] px-4 pt-3 pb-4 bg-[#0a0a0a] flex-shrink-0">
          <div className="max-w-3xl mx-auto space-y-2.5">
            {/* Attachment cards — visible above input when docs uploaded */}
            {docs.length > 0 && (
              <div className="flex flex-wrap gap-2">
                {docs.map(doc => (
                  <DocumentCard
                    key={doc.doc_id}
                    doc={doc}
                    selected={doc.doc_id === selectedDocId}
                    onSelect={onSelectDoc}
                    onRemove={onRemoveDoc}
                  />
                ))}
              </div>
            )}
            <QueryInput
              onSubmit={onSubmit}
              onUpload={onUpload}
              disabled={isInputBusy}
              mode={mode}
              onModeChange={onModeChange}
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
}: {
  mode: Mode
  onSubmit: (query: string, mode: Mode) => void
  onModeChange: (mode: Mode) => void
  onUpload?: (file: File) => void
}) {
  const examples =
    mode === 'chat'
      ? ['Explain DCF valuation', 'What is EBITDA?', 'How do LBOs work?']
      : mode === 'research'
        ? ['Apple stock last 5 years', 'AI landscape 2025', 'Compare React vs Vue']
        : ['Apple vs Google financials', 'Explain quantitative easing', 'AI model releases 2025']

  return (
    <div className="flex-1 flex flex-col items-center justify-center min-h-full px-6 py-16 space-y-8">
      <div className="w-full max-w-xl space-y-2 text-center">
        <h2 className="text-xl font-medium text-zinc-100 tracking-tight">
          {mode === 'chat'
            ? 'What would you like to discuss?'
            : mode === 'research'
              ? 'What do you want to research?'
              : 'What can I help you with?'}
        </h2>
        <p className="text-sm text-zinc-600">
          {mode === 'chat'
            ? 'Quick answers, explanations, and follow-ups.'
            : mode === 'research'
              ? 'Deep research with a structured plan and full report.'
              : "I'll decide whether to research or answer directly."}
        </p>
      </div>

      <div className="w-full max-w-xl">
        <QueryInput
          onSubmit={onSubmit}
          onUpload={onUpload}
          disabled={false}
          autoFocus
          mode={mode}
          onModeChange={onModeChange}
        />
      </div>

      <div className="flex flex-wrap gap-2 justify-center">
        {examples.map(ex => (
          <button
            key={ex}
            onClick={() => onSubmit(ex, mode)}
            className="px-3 py-1.5 rounded-full border border-[#222] text-xs text-zinc-600 hover:text-zinc-300 hover:border-[#333] transition-colors duration-150"
          >
            {ex}
          </button>
        ))}
      </div>
    </div>
  )
}
