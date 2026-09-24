import { BarChart3, FileText, Layers, Presentation, Table2 } from 'lucide-react'
import type { WorkspaceObject } from '../types'

const TYPE_LABEL: Record<string, string> = {
  uploaded_document: 'Document',
  document_analysis: 'Document analysis',
  dcf_run: 'DCF run',
  memo: 'Memo',
  deck: 'Deck',
  comparison: 'Comparison',
}

function iconFor(type: string) {
  if (type === 'uploaded_document') return <FileText size={14} />
  if (type === 'dcf_run') return <BarChart3 size={14} />
  if (type === 'deck') return <Presentation size={14} />
  if (type === 'comparison') return <Table2 size={14} />
  if (type === 'memo') return <FileText size={14} />
  return <Layers size={14} />
}

function timeAgo(iso: string): string {
  const diff = Date.now() - new Date(iso).getTime()
  const m = Math.floor(diff / 60000)
  if (m < 1) return 'now'
  if (m < 60) return `${m}m`
  const h = Math.floor(m / 60)
  if (h < 24) return `${h}h`
  return `${Math.floor(h / 24)}d`
}

interface Props {
  objects: WorkspaceObject[]
  onOpenObject?: (object: WorkspaceObject) => void
}

export function WorkspaceObjectsRail({ objects, onOpenObject }: Props) {
  return (
    <aside className="hidden xl:flex w-[240px] flex-shrink-0 flex-col border-r border-border-subtle bg-bg">
      <div className="px-3 py-3 border-b border-border-subtle">
        <div className="text-[11px] uppercase tracking-[0.16em] text-ink-dim">Workspace</div>
        <div className="mt-1 text-sm font-medium text-ink">Objects</div>
      </div>

      <div className="flex-1 overflow-y-auto p-2">
        {objects.length === 0 ? (
          <div className="rounded-md border border-border-subtle px-3 py-4 text-xs text-ink-dim">
            Workflow outputs will appear here.
          </div>
        ) : (
          <div className="space-y-1.5">
            {objects.map(obj => (
              <button
                key={obj.object_id}
                type="button"
                onClick={() => onOpenObject?.(obj)}
                className="w-full rounded-md border border-transparent px-2.5 py-2 text-left hover:border-border-subtle hover:bg-surface/50 focus:outline-none focus:ring-1 focus:ring-border-hover"
              >
                <div className="flex items-center gap-2 text-ink-muted">
                  <span className="text-ink-dim">{iconFor(obj.object_type)}</span>
                  <span className="truncate text-[11px] uppercase tracking-[0.12em]">
                    {TYPE_LABEL[obj.object_type] ?? obj.object_type}
                  </span>
                  <span className="ml-auto text-[11px] text-ink-dim">{timeAgo(obj.updated_at)}</span>
                </div>
                <div className="mt-1 truncate text-sm font-medium text-ink">{obj.title}</div>
                {obj.summary && (
                  <div className="mt-1 max-h-8 overflow-hidden text-xs leading-4 text-ink-muted">{obj.summary}</div>
                )}
              </button>
            ))}
          </div>
        )}
      </div>
    </aside>
  )
}
