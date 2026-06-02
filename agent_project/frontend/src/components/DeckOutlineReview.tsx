import { useState } from 'react'
import type { DeckReviewState, DeckOutlineSlide } from '../types'

interface Props {
  review: DeckReviewState
  threadId: string
  onApprove?: () => void
  onReject?: () => void
}

export function DeckOutlineReview({ review, threadId, onApprove, onReject }: Props) {
  const slides = review.outline?.slides ?? []
  const [editedSlides, setEditedSlides] = useState<DeckOutlineSlide[]>(() =>
    slides.map(s => ({ ...s })),
  )
  const [confirmed, setConfirmed] = useState(false)
  const [rejecting, setRejecting] = useState(false)

  const updateTitle = (index: number, title: string) => {
    setEditedSlides(prev => prev.map((s, i) => (i === index ? { ...s, title } : s)))
  }

  const titlesChanged = editedSlides.some(
    (s, i) => s.title !== (slides[i]?.title ?? ''),
  )

  const handleApprove = async () => {
    setConfirmed(true)
    const body: Record<string, unknown> = {
      approved: true,
      action: titlesChanged ? 'edit' : 'approve',
    }
    if (titlesChanged) {
      body.outline = {
        ...review.outline,
        slides: editedSlides,
      }
    }

    try {
      await fetch(`/runs/${threadId}/deck-decision`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      })
    } catch (err) {
      console.error('Deck decision submission failed:', err)
    }
    onApprove?.()
  }

  const handleReject = async () => {
    setRejecting(true)
    try {
      await fetch(`/runs/${threadId}/deck-decision`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ approved: false, action: 'reject' }),
      })
    } catch (err) {
      console.error('Deck reject submission failed:', err)
    }
    onReject?.()
  }

  if (confirmed) {
    return (
      <div className="flex items-center gap-2 px-1 py-2 text-[11px] text-emerald-400">
        <span className="w-1.5 h-1.5 rounded-full bg-emerald-500 flex-shrink-0" />
        Outline approved — generating slides…
      </div>
    )
  }

  if (rejecting) {
    return (
      <div className="flex items-center gap-2 px-1 py-2 text-[11px] text-ink-dim">
        <span className="w-1.5 h-1.5 rounded-full bg-zinc-600 flex-shrink-0" />
        Outline rejected.
      </div>
    )
  }

  return (
    <div className="rounded border border-border bg-bg overflow-hidden">
      <div className="flex items-center gap-2 px-3 py-2 border-b border-border text-[11px]">
        <span className="w-1.5 h-1.5 rounded-full bg-amber-400 animate-pulse flex-shrink-0" />
        <span className="text-ink-muted font-medium truncate">
          Review outline — {review.deck_title || 'Deck'}
        </span>
        <span className="ml-auto text-ink-dim tabular-nums shrink-0">
          {editedSlides.length} slides
        </span>
      </div>

      {review.outline?.rationale && (
        <div className="px-3 py-2 border-b border-border-subtle text-[10px] text-ink-dim leading-relaxed">
          {review.outline.rationale}
        </div>
      )}

      <div className="max-h-52 overflow-y-auto divide-y divide-border-subtle">
        {editedSlides.map((slide, idx) => (
          <div
            key={slide.slide_id || idx}
            className="grid grid-cols-[24px_72px_1fr] gap-2 items-center px-3 py-1.5 text-[11px]"
          >
            <span className="text-zinc-700 tabular-nums">{idx + 1}</span>
            <span className="text-[9px] uppercase tracking-wide text-indigo-400/80 truncate">
              {slide.layout}
            </span>
            <input
              type="text"
              value={slide.title}
              onChange={e => updateTitle(idx, e.target.value)}
              className="w-full bg-transparent border-b border-transparent hover:border-border-accent focus:border-indigo-500/50 focus:outline-none text-ink-muted truncate"
              title="Edit slide title"
            />
          </div>
        ))}
      </div>

      {review.blocks_preview.length > 0 && (
        <details className="border-t border-border-subtle px-3 py-2 text-[10px] text-ink-dim">
          <summary className="cursor-pointer hover:text-ink-dim">
            {review.blocks_preview.length} content block(s)
          </summary>
          <ul className="mt-2 space-y-1 max-h-24 overflow-y-auto">
            {review.blocks_preview.map(b => (
              <li key={b.block_id} className="truncate">
                <span className="text-zinc-700">{b.kind}</span>
                {' · '}
                {b.title || b.block_id}
              </li>
            ))}
          </ul>
        </details>
      )}

      <div className="flex gap-2 px-3 py-3 border-t border-border">
        <button
          type="button"
          onClick={handleApprove}
          className="flex-1 py-2 rounded-lg text-xs font-medium bg-indigo-600 hover:bg-indigo-500 active:bg-indigo-700 text-white transition-colors duration-150"
        >
          Approve Outline
        </button>
        <button
          type="button"
          onClick={handleReject}
          className="flex-1 py-2 rounded-lg text-xs font-medium bg-surface hover:bg-surface-3 active:bg-surface text-ink-dim border border-border-hover transition-colors duration-150"
        >
          Reject
        </button>
      </div>
    </div>
  )
}
