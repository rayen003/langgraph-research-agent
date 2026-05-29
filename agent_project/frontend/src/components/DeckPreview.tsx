import { useEffect, useState } from 'react'

export interface DeckColumn {
  heading?: string
  bullets?: string[]
  paragraphs?: string[]
}

export interface DeckFlowStep {
  label?: string
  detail?: string
}

export interface DeckSlidePreview {
  slide_id: string
  layout: string
  title: string
  body_bullets?: string[]
  body_paragraphs?: string[]
  table_rows?: string[][]
  chart_path?: string | null
  chart_caption?: string | null
  metric_value?: string | null
  metric_label?: string | null
  // Expectations-deck layouts — three_box, two_col_narrative use `columns`;
  // flow_diagram uses `flow_steps`. Backend always emits these arrays even
  // when empty so we can detect missing data here.
  columns?: DeckColumn[]
  flow_steps?: DeckFlowStep[]
}

export interface DeckOutputPayload {
  brief?: { title?: string }
  slides?: DeckSlidePreview[]
  pptx_filename?: string | null
  pptx_relpath?: string | null
}

interface Props {
  threadId: string
  filename: string
  title?: string
  onClose: () => void
}

function deckFilenameFromPath(path: string): string {
  return path.split('/').pop() ?? path
}

function chartUrl(threadId: string, chartPath: string | null | undefined): string | null {
  if (!chartPath) return null
  const name = chartPath.split('/').pop()
  if (!name) return null
  if (chartPath.includes('artifacts/') || /\.(png|jpg|jpeg|webp|gif)$/i.test(name)) {
    return `/artifacts/${threadId}/${name}`
  }
  return null
}

function ColumnsBlock({
  columns,
  accent,
}: {
  columns: DeckColumn[]
  accent: 'neutral' | 'binary'
}) {
  const accentColors = accent === 'binary'
    ? ['border-teal-400/60', 'border-rose-400/60']
    : ['border-[#252535]', 'border-[#252535]', 'border-[#252535]']
  return (
    <div className={`grid gap-2 mt-1`}
      style={{ gridTemplateColumns: `repeat(${columns.length}, minmax(0, 1fr))` }}>
      {columns.map((col, i) => (
        <div
          key={i}
          className={`rounded-md border ${accentColors[i % accentColors.length]} bg-[#0a0a14]/60 p-3 flex flex-col gap-1.5`}
        >
          {col.heading && (
            <div className="text-[11px] font-semibold uppercase tracking-wide text-zinc-200">
              {col.heading}
            </div>
          )}
          {col.bullets && col.bullets.length > 0 && (
            <ul className="space-y-1 text-[11px] text-zinc-300 list-disc pl-4">
              {col.bullets.slice(0, 6).map((b, j) => (
                <li key={j} className="leading-snug">{b}</li>
              ))}
            </ul>
          )}
          {col.paragraphs?.map((p, j) => (
            <p key={j} className="text-[11px] text-zinc-300 leading-relaxed">{p}</p>
          ))}
        </div>
      ))}
    </div>
  )
}

function FlowStepsBlock({ steps }: { steps: DeckFlowStep[] }) {
  return (
    <div className="flex flex-col items-center gap-1 mt-1">
      {steps.slice(0, 6).map((s, i) => (
        <div key={i} className="w-full max-w-md flex flex-col items-center">
          <div className="w-full rounded-md border border-teal-400/40 bg-[#0a0a14]/60 px-3 py-1.5 text-center">
            <div className="text-[12px] font-semibold text-zinc-200">{s.label}</div>
            {s.detail && <div className="text-[10px] text-zinc-500">{s.detail}</div>}
          </div>
          {i < steps.length - 1 && (
            <div className="text-teal-400 text-[10px] my-0.5">▼</div>
          )}
        </div>
      ))}
    </div>
  )
}

function SlideCanvas({
  slide,
  index,
  total,
  threadId,
}: {
  slide: DeckSlidePreview
  index: number
  total: number
  threadId: string
}) {
  const chartSrc = chartUrl(threadId, slide.chart_path)
  const hasColumns = (slide.columns?.length ?? 0) > 0
  const hasFlow = (slide.flow_steps?.length ?? 0) > 0
  const isBinaryLayout = slide.layout === 'two_col_narrative'
  const isVariableImpact = slide.layout === 'variable_impact_table'

  return (
    <div className="rounded-lg border border-[#1e1e2a] bg-[#0d0d14] overflow-hidden shadow-lg">
      <div className="flex items-center justify-between px-3 py-2 border-b border-[#141420] text-[10px] text-zinc-600">
        <span className="uppercase tracking-wide text-indigo-400/80">{slide.layout}</span>
        <span className="tabular-nums">{index + 1} / {total}</span>
      </div>
      <div className="aspect-[16/9] p-5 flex flex-col gap-3 overflow-hidden">
        <h4 className="text-lg font-semibold text-zinc-100 leading-snug">{slide.title || 'Untitled slide'}</h4>

        {slide.metric_value && (
          <div className="flex flex-col items-start gap-1">
            <span className="text-3xl font-semibold text-teal-300 tabular-nums">{slide.metric_value}</span>
            {slide.metric_label && (
              <span className="text-xs text-zinc-500">{slide.metric_label}</span>
            )}
          </div>
        )}

        {slide.body_paragraphs?.map((p, i) => (
          <p key={i} className="text-sm text-zinc-300 leading-relaxed line-clamp-4">{p}</p>
        ))}

        {hasColumns && (
          <ColumnsBlock columns={slide.columns!} accent={isBinaryLayout ? 'binary' : 'neutral'} />
        )}

        {hasFlow && <FlowStepsBlock steps={slide.flow_steps!} />}

        {slide.body_bullets && slide.body_bullets.length > 0 && !hasColumns && (
          <ul className="space-y-1.5 text-sm text-zinc-300 list-disc pl-4">
            {slide.body_bullets.slice(0, 8).map((b, i) => (
              <li key={i} className="leading-snug">{b}</li>
            ))}
          </ul>
        )}

        {slide.table_rows && slide.table_rows.length > 0 && (
          <div className="overflow-x-auto mt-auto">
            <table className="w-full text-[11px] text-zinc-300 border-collapse">
              <tbody>
                {slide.table_rows.slice(0, 6).map((row, ri) => (
                  <tr key={ri} className={ri === 0 ? 'text-zinc-400 font-medium' : ''}>
                    {row.map((cell, ci) => {
                      const isImpactCol = isVariableImpact && ci === row.length - 1 && ri > 0
                      return (
                        <td
                          key={ci}
                          className={`border border-[#1a1a24] px-2 py-1 align-top ${
                            isImpactCol ? 'text-teal-300 font-medium tabular-nums' : ''
                          }`}
                        >
                          {cell}
                        </td>
                      )
                    })}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        {chartSrc && (
          <div className="mt-auto space-y-2">
            <img
              src={chartSrc}
              alt={slide.chart_caption || slide.title}
              className="max-h-40 w-auto mx-auto rounded border border-[#1a1a24]"
            />
            {slide.chart_caption && (
              <p className="text-[11px] text-zinc-500 text-center">{slide.chart_caption}</p>
            )}
          </div>
        )}

        {/* Caveat footer (variable_impact_table writes caveat into chart_caption when no chart) */}
        {!chartSrc && slide.chart_caption && (
          <p className="text-[10px] text-zinc-500 italic mt-2">{slide.chart_caption}</p>
        )}
      </div>
    </div>
  )
}

export function DeckPreview({ threadId, filename, title, onClose }: Props) {
  const [payload, setPayload] = useState<DeckOutputPayload | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [activeIdx, setActiveIdx] = useState(0)

  const safeFilename = deckFilenameFromPath(filename)

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError(null)
    fetch(`/runs/${threadId}/deck-output`)
      .then(async res => {
        if (!res.ok) {
          const body = await res.json().catch(() => ({})) as { detail?: string }
          throw new Error(body.detail || `Could not load deck preview (${res.status})`)
        }
        return res.json() as Promise<DeckOutputPayload>
      })
      .then(data => {
        if (!cancelled) {
          setPayload(data)
          setActiveIdx(0)
        }
      })
      .catch(err => {
        if (!cancelled) setError(err instanceof Error ? err.message : 'Failed to load deck preview')
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => { cancelled = true }
  }, [threadId])

  const slides = payload?.slides ?? []
  const displayTitle = title || payload?.brief?.title || safeFilename.replace(/\.pptx$/i, '')

  const handleDownload = async () => {
    try {
      const res = await fetch(`/runs/${threadId}/decks/${encodeURIComponent(safeFilename)}`)
      if (!res.ok) {
        const body = await res.json().catch(() => ({})) as { detail?: string }
        throw new Error(body.detail || `Download failed (${res.status})`)
      }
      const blob = await res.blob()
      const url = URL.createObjectURL(blob)
      const anchor = document.createElement('a')
      anchor.href = url
      anchor.download = safeFilename
      anchor.click()
      URL.revokeObjectURL(url)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Download failed')
    }
  }

  return (
    <div className="w-full h-full flex flex-col bg-[#0a0a0a] border-l border-[#141414]">
      <div className="flex items-center justify-between px-4 py-3 border-b border-[#141414] flex-shrink-0">
        <div className="min-w-0">
          <h3 className="text-sm font-medium text-zinc-100 truncate">{displayTitle}</h3>
          <p className="text-[11px] text-zinc-600">
            {slides.length ? `${slides.length} slides` : 'Deck preview'}
          </p>
        </div>
        <div className="flex items-center gap-1 flex-shrink-0">
          <button
            type="button"
            onClick={handleDownload}
            title="Download PPTX"
            className="px-2.5 py-1 rounded-md border border-[#252535] text-[11px] text-zinc-300 hover:text-zinc-100 hover:border-[#33334a] transition-colors"
          >
            Download
          </button>
          <button
            type="button"
            onClick={onClose}
            title="Close"
            className="w-7 h-7 flex items-center justify-center rounded-md text-zinc-500 hover:text-zinc-200 hover:bg-[#1a1a22] transition-colors"
          >
            <svg width="11" height="11" viewBox="0 0 11 11" fill="none">
              <path d="M2 2l7 7M9 2l-7 7" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" />
            </svg>
          </button>
        </div>
      </div>

      <div className="flex-1 overflow-y-auto px-4 py-4 bg-[#080808]">
        {loading && <p className="text-xs text-zinc-500">Loading slide preview…</p>}
        {error && !loading && (
          <p className="text-xs text-red-300/90 leading-relaxed">{error}</p>
        )}
        {!loading && !error && slides.length === 0 && (
          <p className="text-xs text-zinc-500">No slide content found in deck output.</p>
        )}
        {!loading && slides.length > 0 && (
          <div className="space-y-4">
            <SlideCanvas
              slide={slides[activeIdx]}
              index={activeIdx}
              total={slides.length}
              threadId={threadId}
            />
            <div className="flex items-center justify-between gap-2">
              <button
                type="button"
                disabled={activeIdx === 0}
                onClick={() => setActiveIdx(i => Math.max(0, i - 1))}
                className="px-2 py-1 text-[11px] rounded border border-[#252535] text-zinc-400 disabled:opacity-40"
              >
                Previous
              </button>
              <span className="text-[11px] text-zinc-600 tabular-nums">
                Slide {activeIdx + 1} of {slides.length}
              </span>
              <button
                type="button"
                disabled={activeIdx >= slides.length - 1}
                onClick={() => setActiveIdx(i => Math.min(slides.length - 1, i + 1))}
                className="px-2 py-1 text-[11px] rounded border border-[#252535] text-zinc-400 disabled:opacity-40"
              >
                Next
              </button>
            </div>
            <div className="grid grid-cols-4 gap-2">
              {slides.map((slide, idx) => (
                <button
                  key={slide.slide_id || idx}
                  type="button"
                  onClick={() => setActiveIdx(idx)}
                  className={`rounded border px-2 py-1.5 text-left text-[10px] truncate transition-colors ${
                    idx === activeIdx
                      ? 'border-indigo-500/50 bg-indigo-500/10 text-indigo-200'
                      : 'border-[#1a1a24] text-zinc-500 hover:border-[#2a2a34]'
                  }`}
                  title={slide.title}
                >
                  {idx + 1}. {slide.title || slide.layout}
                </button>
              ))}
            </div>
          </div>
        )}
      </div>
    </div>
  )
}
