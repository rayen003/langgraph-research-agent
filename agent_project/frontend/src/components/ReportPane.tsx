import { useEffect, useRef } from 'react'
import { MarkdownRenderer } from './MarkdownRenderer'
import type { RunStatus } from '../types'

interface Props {
  query: string
  report: string
  status: RunStatus
  artifactPaths: string[]
  threadId: string | null
  onReset: () => void
}

const STATUS_LABEL: Partial<Record<RunStatus, string>> = {
  planning: 'Planning…',
  awaiting_approval: 'Awaiting approval',
  executing: 'Executing…',
  synthesizing: 'Writing report…',
  complete: 'Complete',
  error: 'Error',
  rejected: 'Rejected',
}

const IMAGE_RE = /\.(png|jpg|jpeg|webp|gif|svg)$/i
const ARTIFACT_MARKER_RE = /\[ARTIFACTS?\]|\[CHART\]/i

/** Split report markdown on the first artifact marker. */
function splitOnMarker(text: string): [string, string] {
  const match = ARTIFACT_MARKER_RE.exec(text)
  if (!match) return [text, '']
  return [text.slice(0, match.index).trimEnd(), text.slice(match.index + match[0].length).trimStart()]
}

function ArtifactImages({
  artifactPaths,
  threadId,
}: {
  artifactPaths: string[]
  threadId: string
}) {
  const images = artifactPaths.filter(p => IMAGE_RE.test(p))
  if (!images.length) return null
  return (
    <div className="my-6 space-y-4">
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

export function ReportPane({
  query,
  report,
  status,
  artifactPaths,
  threadId,
  onReset,
}: Props) {
  const bottomRef = useRef<HTMLDivElement>(null)
  const isStreaming = status === 'synthesizing'
  const isComplete = status === 'complete'
  const hasReport = report.length > 0

  const hasArtifacts = isComplete && artifactPaths.length > 0 && !!threadId
  const markerPresent = ARTIFACT_MARKER_RE.test(report)

  // Split report around [ARTIFACTS] marker so images land inline
  const [before, after] = hasArtifacts && markerPresent
    ? splitOnMarker(report)
    : [report, '']

  // Auto-scroll while streaming
  useEffect(() => {
    if (isStreaming) bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [report, isStreaming])

  return (
    <div className="flex-1 flex flex-col overflow-hidden min-w-0">
      {/* Top bar */}
      <div className="flex items-center gap-3 px-6 py-3.5 border-b border-border flex-shrink-0">
        <button
          onClick={onReset}
          className="text-zinc-700 hover:text-ink-muted transition-colors duration-150 flex-shrink-0"
          title="New research"
        >
          <svg width="14" height="14" viewBox="0 0 14 14" fill="none">
            <path
              d="M2 7H12M2 7L6 3M2 7L6 11"
              stroke="currentColor"
              strokeWidth="1.3"
              strokeLinecap="round"
              strokeLinejoin="round"
            />
          </svg>
        </button>

        <h2 className="flex-1 text-sm text-ink-muted font-medium truncate">{query}</h2>

        <div className="flex items-center gap-1.5 flex-shrink-0">
          {(status === 'executing' || status === 'synthesizing' || status === 'planning') ? (
            <div className="w-1.5 h-1.5 rounded-full bg-indigo-500 animate-pulse" />
          ) : isComplete ? (
            <div className="w-1.5 h-1.5 rounded-full bg-emerald-500" />
          ) : status === 'error' ? (
            <div className="w-1.5 h-1.5 rounded-full bg-red-500" />
          ) : null}
          <span className="text-xs text-ink-dim">{STATUS_LABEL[status] ?? ''}</span>
        </div>
      </div>

      {/* Content area */}
      <div className="flex-1 overflow-y-auto px-8 py-7">
        {/* Empty states */}
        {!hasReport && (
          <div className="space-y-3 animate-fade-up">
            {status === 'planning' && <Skeleton lines={[60, 90, 75]} />}
            {status === 'awaiting_approval' && (
              <p className="text-sm text-ink-dim">
                Review and approve the plan on the right to begin execution.
              </p>
            )}
            {status === 'executing' && <Skeleton lines={[80, 55, 70, 90, 60]} />}
          </div>
        )}

        {/* Report — with inline artifact placement */}
        {hasReport && (
          <article className="max-w-none animate-fade-up">
            <MarkdownRenderer content={before} streaming={isStreaming && !markerPresent && !after} />

            {/* Inline artifacts at marker position */}
            {hasArtifacts && markerPresent && (
              <ArtifactImages artifactPaths={artifactPaths} threadId={threadId!} />
            )}

            {after && (
              <MarkdownRenderer content={after} streaming={isStreaming} />
            )}

            {/* Artifacts at end if no marker was present */}
            {hasArtifacts && !markerPresent && (
              <ArtifactImages artifactPaths={artifactPaths} threadId={threadId!} />
            )}
          </article>
        )}

        <div ref={bottomRef} />
      </div>
    </div>
  )
}

function Skeleton({ lines }: { lines: number[] }) {
  return (
    <div className="space-y-2.5 pt-2">
      {lines.map((w, i) => (
        <div key={i} className="h-3 rounded-md bg-surface-2" style={{ width: `${w}%` }} />
      ))}
    </div>
  )
}
