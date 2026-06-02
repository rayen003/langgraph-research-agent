import type { AttachedDocSnapshot, DocumentInfo } from '../types'

const EXT_LABEL: Record<string, string> = {
  pdf: 'pdf',
  docx: 'docx',
  doc: 'doc',
  xlsx: 'xlsx',
  xls: 'xls',
  csv: 'csv',
  txt: 'txt',
  md: 'md',
}

function extLabel(filename: string): string {
  const ext = filename.split('.').pop()?.toLowerCase() ?? ''
  return EXT_LABEL[ext] ?? (ext.toLowerCase() || 'file')
}

// Ingest stage → short badge label (live progress while processing).
const STAGE_LABEL: Record<string, string> = {
  queued: 'QUEUED',
  uploading: 'UPLOAD',
  parsing: 'PARSE',
  chunking: 'CHUNK',
  embedding: 'EMBED',
}

interface ComposerProps {
  variant?: 'composer'
  doc: DocumentInfo
  selected?: boolean
  onSelect?: (id: string) => void
  onRemove?: (id: string) => void
  animationDelayMs?: number
  exiting?: boolean
}

interface SentProps {
  variant: 'sent'
  doc: AttachedDocSnapshot
  onSelect?: (id: string) => void
  animationDelayMs?: number
}

type Props = ComposerProps | SentProps

/** Attachment tile — composer (input) or sent (chat bubble) variant. */
export function AttachmentChip(props: Props) {
  if (props.variant === 'sent') {
    return <SentChip {...props} />
  }
  return <ComposerChip {...props} />
}

function ComposerChip({
  doc,
  selected,
  onSelect,
  onRemove,
  animationDelayMs = 0,
  exiting = false,
}: ComposerProps) {
  const label = extLabel(doc.filename).toUpperCase()
  const isProcessing = doc.status === 'processing'
  const isError = doc.status === 'error'
  const isPending = doc.doc_id.startsWith('pending_')
  const clickable = !isProcessing && !isError && !isPending && !!onSelect

  const displayName =
    doc.filename.length > 28 ? `${doc.filename.slice(0, 26)}…` : doc.filename

  return (
    <div
      className={`
        group/chip relative flex-shrink-0 w-[132px] h-[52px] rounded-lg border px-2.5 py-2
        transition-all duration-200
        ${exiting ? 'animate-attachment-exit' : 'animate-attachment-in'}
        ${isError
          ? 'border-danger/40 bg-danger-soft'
          : selected
            ? 'border-accent/50 bg-accent-soft'
            : isProcessing || isPending
              ? 'border-border-hover bg-bg-overlay animate-pulse-subtle'
              : 'border-border-hover bg-bg-raised hover:border-border-accent'
        }
      `}
      style={{ animationDelay: exiting ? '0ms' : `${animationDelayMs}ms` }}
    >
      <button
        type="button"
        onClick={() => clickable && onSelect?.(doc.doc_id)}
        disabled={!clickable}
        className={`w-full h-full text-left ${clickable ? 'cursor-pointer' : 'cursor-default'}`}
        title={doc.filename}
      >
        <p className={`text-[11px] font-medium leading-tight truncate pr-4 ${isError ? 'text-danger' : 'text-ink'}`}>
          {displayName}
        </p>
        <span className="inline-block mt-1.5 px-1.5 py-px rounded text-[9px] font-semibold uppercase tracking-wide bg-surface-3 text-ink-dim border border-border-subtle">
          {isProcessing || isPending
            ? (STAGE_LABEL[doc.stage ?? ''] ?? 'INDEXING')
            : isError ? 'ERR' : label}
        </span>
      </button>

      {onRemove && (
        <button
          type="button"
          onClick={e => {
            e.stopPropagation()
            onRemove(doc.doc_id)
          }}
          className="
            absolute top-1 right-1 w-4 h-4 rounded-full
            bg-surface-2 border border-border-hover
            flex items-center justify-center
            text-ink-dim hover:text-ink hover:bg-surface-3
            opacity-0 group-hover/chip:opacity-100 transition-opacity
          "
          title="Remove"
          aria-label={`Remove ${doc.filename}`}
        >
          <svg width="7" height="7" viewBox="0 0 7 7" fill="none">
            <path d="M1 1l5 5M6 1L1 6" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" />
          </svg>
        </button>
      )}
    </div>
  )
}

function SentChip({
  doc,
  onSelect,
  animationDelayMs = 0,
}: SentProps) {
  const label = extLabel(doc.filename)
  const meta =
    doc.page_count && doc.page_count > 0
      ? `${label} · ${doc.page_count} pg`
      : label
  const displayName =
    doc.filename.length > 34 ? `${doc.filename.slice(0, 32)}…` : doc.filename
  const clickable = doc.status === 'ready' && !!onSelect

  return (
    <button
      type="button"
      disabled={!clickable}
      onClick={() => clickable && onSelect?.(doc.doc_id)}
      title={doc.filename}
      className={`
        flex-shrink-0 flex items-center gap-2.5 min-w-[160px] max-w-[220px] h-[52px]
        px-3 py-2 rounded-xl border border-border-hover bg-bg-overlay
        text-left animate-message-send transition-colors
        ${clickable ? 'hover:border-border-accent hover:bg-bg-raised cursor-pointer' : 'cursor-default'}
      `}
      style={{ animationDelay: `${animationDelayMs}ms` }}
    >
      <div className="w-8 h-8 rounded-lg bg-surface-3 border border-border-subtle flex items-center justify-center flex-shrink-0">
        <svg width="14" height="14" viewBox="0 0 14 14" fill="none" className="text-ink-dim">
          <path d="M2.5 1.5h6L11 4.5v8H2.5v-11z" stroke="currentColor" strokeWidth="1.2" strokeLinejoin="round" />
          <path d="M8 1.5V5h2.5" stroke="currentColor" strokeWidth="1.2" strokeLinejoin="round" />
        </svg>
      </div>
      <div className="min-w-0">
        <p className="text-[11px] font-medium text-ink truncate">{displayName}</p>
        <p className="text-[10px] text-ink-dim mt-0.5">{meta}</p>
      </div>
    </button>
  )
}
