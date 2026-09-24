import { useMemo, useState } from 'react'
import { Check, FileText, Pencil, X } from 'lucide-react'
import type { MemoDraftState, MemoReviewState } from '../types'

interface Props {
  review: MemoReviewState
  threadId: string
  onApprove?: () => void
  onReject?: () => void
}

export function MemoDraftReview({ review, threadId, onApprove, onReject }: Props) {
  const original = review.draft
  const [draft, setDraft] = useState<MemoDraftState>(() => ({
    ...original,
    sections: { ...original.sections },
    limitations: [...(original.limitations ?? [])],
  }))
  const [submitting, setSubmitting] = useState<'approve' | 'reject' | null>(null)
  const [error, setError] = useState('')
  const changed = useMemo(() => JSON.stringify(draft) !== JSON.stringify(original), [draft, original])

  const update = (field: keyof MemoDraftState, value: string) => {
    setDraft(prev => ({ ...prev, [field]: value }))
  }

  const updateSection = (title: string, value: string) => {
    setDraft(prev => ({ ...prev, sections: { ...prev.sections, [title]: value } }))
  }

  const submit = async (approved: boolean) => {
    setSubmitting(approved ? 'approve' : 'reject')
    setError('')
    try {
      const response = await fetch(`/runs/${threadId}/memo-decision`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          approved,
          action: approved ? (changed ? 'edit' : 'approve') : 'reject',
          draft: approved && changed ? draft : undefined,
        }),
      })
      if (!response.ok) throw new Error(await response.text())
      approved ? onApprove?.() : onReject?.()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Decision failed')
      setSubmitting(null)
    }
  }

  if (submitting) {
    return (
      <div className="flex items-center gap-2 px-1 py-2 text-[11px] text-ink-dim">
        <span className={`h-1.5 w-1.5 rounded-full ${submitting === 'approve' ? 'bg-emerald-500 animate-pulse' : 'bg-zinc-600'}`} />
        {submitting === 'approve' ? 'Finalizing memo…' : 'Memo rejected.'}
      </div>
    )
  }

  return (
    <div className="overflow-hidden rounded border border-border bg-bg">
      <div className="flex items-center gap-2 border-b border-border px-3 py-2">
        <FileText size={14} className="text-amber-400" />
        <span className="min-w-0 flex-1 truncate text-[11px] font-medium text-ink-muted">Review memo draft</span>
        <span className="text-[10px] tabular-nums text-ink-dim">{Math.round((draft.confidence ?? 0) * 100)}%</span>
      </div>

      <div className="max-h-[56vh] space-y-4 overflow-y-auto px-3 py-3">
        <label className="block space-y-1">
          <span className="text-[9px] uppercase tracking-widest text-ink-dim">Title</span>
          <input
            value={draft.title}
            onChange={event => update('title', event.target.value)}
            className="w-full border-b border-border bg-transparent pb-1 text-sm font-semibold text-ink outline-none focus:border-indigo-500"
          />
        </label>

        <MemoField label="Executive summary" value={draft.executive_summary} onChange={value => update('executive_summary', value)} />

        {Object.entries(draft.sections).map(([title, body]) => (
          <MemoField key={title} label={title} value={body} onChange={value => updateSection(title, value)} />
        ))}

        <MemoField label="Recommendation" value={draft.recommendation} onChange={value => update('recommendation', value)} accent />

        <div className="border-t border-border-subtle pt-3">
          <div className="mb-2 text-[9px] uppercase tracking-widest text-ink-dim">Sources</div>
          <div className="space-y-2">
            {review.sources.map((source, index) => (
              <div key={source.version_id || source.object_id || source.doc_id || index} className="flex min-w-0 items-start gap-2 text-[10px]">
                <FileText size={12} className="mt-0.5 shrink-0 text-indigo-400" />
                <div className="min-w-0">
                  <div className="truncate text-ink-muted">{source.title}</div>
                  <div className="truncate font-mono text-[9px] text-ink-dim">{source.version_id || source.object_id || source.doc_id || source.type}</div>
                </div>
              </div>
            ))}
          </div>
        </div>

        {(draft.limitations?.length ?? 0) > 0 && (
          <div className="border-t border-border-subtle pt-3">
            <div className="mb-1 text-[9px] uppercase tracking-widest text-ink-dim">Limitations</div>
            <ul className="space-y-1 text-[10px] leading-relaxed text-ink-dim">
              {draft.limitations.map((item, index) => <li key={index}>• {item}</li>)}
            </ul>
          </div>
        )}
      </div>

      {error && <div className="border-t border-red-900/40 bg-red-950/20 px-3 py-2 text-[10px] text-red-400">{error}</div>}
      <div className="flex gap-2 border-t border-border px-3 py-3">
        <button
          type="button"
          onClick={() => void submit(true)}
          className="flex flex-1 items-center justify-center gap-1.5 rounded bg-indigo-600 py-2 text-xs font-medium text-white transition-colors hover:bg-indigo-500"
        >
          {changed ? <Pencil size={13} /> : <Check size={13} />}
          {changed ? 'Save & approve' : 'Approve'}
        </button>
        <button
          type="button"
          onClick={() => void submit(false)}
          title="Reject memo"
          aria-label="Reject memo"
          className="flex h-8 w-8 shrink-0 items-center justify-center rounded border border-border-hover bg-surface text-ink-dim transition-colors hover:bg-surface-3 hover:text-ink"
        >
          <X size={14} />
        </button>
      </div>
    </div>
  )
}

function MemoField({ label, value, onChange, accent = false }: {
  label: string
  value: string
  onChange: (value: string) => void
  accent?: boolean
}) {
  return (
    <label className="block space-y-1">
      <span className={`text-[9px] uppercase tracking-widest ${accent ? 'text-indigo-400' : 'text-ink-dim'}`}>{label}</span>
      <textarea
        value={value}
        onChange={event => onChange(event.target.value)}
        rows={Math.min(7, Math.max(3, Math.ceil(value.length / 48)))}
        className="w-full resize-y rounded border border-border-subtle bg-surface/40 px-2.5 py-2 text-[11px] leading-relaxed text-ink-muted outline-none focus:border-indigo-500/60"
      />
    </label>
  )
}

