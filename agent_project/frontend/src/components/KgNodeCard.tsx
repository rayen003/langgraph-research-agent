import { useState } from 'react'
import type { KgNode } from '../hooks/useKnowledgeGraph'
import { KgValueView } from './KgValueView'

interface Props {
  node: KgNode
  onClose: () => void
  onPatch: (id: string, patch: { value?: unknown; confidence?: number }) => Promise<void>
  onDelete: (id: string) => Promise<void>
}

function formatAge(updated_at: number): string {
  const age = Math.max(0, Date.now() / 1000 - updated_at)
  if (age < 60) return `${Math.round(age)}s ago`
  if (age < 3600) return `${Math.round(age / 60)}m ago`
  if (age < 86400) return `${Math.round(age / 3600)}h ago`
  return `${Math.round(age / 86400)}d ago`
}

export function KgNodeCard({ node, onClose, onPatch, onDelete }: Props) {
  const [editing, setEditing] = useState(false)
  const [draftValue, setDraftValue] = useState<string>(
    typeof node.value === 'string' ? node.value : JSON.stringify(node.value, null, 2),
  )
  const [busy, setBusy] = useState(false)

  const handleSave = async () => {
    setBusy(true)
    let parsedValue: unknown = draftValue
    try { parsedValue = JSON.parse(draftValue) } catch { /* keep as string */ }
    await onPatch(node.id, { value: parsedValue, confidence: 1.0 })
    setBusy(false)
    setEditing(false)
  }

  const handleDelete = async () => {
    if (!confirm(`Delete node ${node.id}?`)) return
    setBusy(true)
    await onDelete(node.id)
    setBusy(false)
    onClose()
  }

  return (
    <div className="absolute top-12 right-3 z-20 w-96 max-h-[calc(100vh-5rem)] overflow-y-auto bg-bg-overlay border border-border-accent rounded-md shadow-xl text-[11px]">
      <div className="px-3 py-2 border-b border-border-accent flex items-center justify-between">
        <div className="text-ink-muted font-medium">{node.node_type}</div>
        <button onClick={onClose} className="text-ink-dim hover:text-ink-muted">✕</button>
      </div>

      <div className="p-3 space-y-2">
        <div>
          <div className="text-[9px] uppercase text-ink-dim tracking-wider">Field</div>
          <div className="text-ink-muted font-mono">{node.ticker}::{node.field}</div>
        </div>

        <div>
          <div className="text-[9px] uppercase text-ink-dim tracking-wider">Value</div>
          {editing ? (
            <textarea
              value={draftValue}
              onChange={e => setDraftValue(e.target.value)}
              rows={8}
              className="w-full mt-1 bg-bg border border-border-accent rounded px-2 py-1 text-ink-muted font-mono text-[10px]"
            />
          ) : (
            <div className="mt-1">
              <KgValueView value={node.value} nodeType={node.node_type} />
            </div>
          )}
        </div>

        <div className="grid grid-cols-2 gap-2">
          <div>
            <div className="text-[9px] uppercase text-ink-dim tracking-wider">Source</div>
            <div className="text-ink-muted">{node.source}</div>
          </div>
          <div>
            <div className="text-[9px] uppercase text-ink-dim tracking-wider">Confidence</div>
            <div className="text-ink-muted">{(node.confidence * 100).toFixed(0)}%</div>
          </div>
        </div>

        <div>
          <div className="text-[9px] uppercase text-ink-dim tracking-wider">Updated</div>
          <div className="text-ink-dim">{formatAge(node.updated_at)}</div>
        </div>

        {node.run_id && (
          <div>
            <div className="text-[9px] uppercase text-ink-dim tracking-wider">Run</div>
            <div className="text-ink-dim font-mono text-[10px]">{node.run_id}</div>
          </div>
        )}

        <div className="flex gap-2 pt-2 border-t border-border-accent">
          {editing ? (
            <>
              <button
                onClick={handleSave} disabled={busy}
                className="flex-1 px-2 py-1 rounded bg-emerald-500/15 text-emerald-400 border border-emerald-500/30 hover:bg-emerald-500/25 disabled:opacity-50"
              >
                {busy ? '…' : 'Save'}
              </button>
              <button
                onClick={() => setEditing(false)} disabled={busy}
                className="px-2 py-1 rounded bg-zinc-800 text-ink-muted border border-zinc-700 hover:bg-zinc-700"
              >
                Cancel
              </button>
            </>
          ) : (
            <>
              <button
                onClick={() => setEditing(true)}
                className="flex-1 px-2 py-1 rounded bg-indigo-500/15 text-indigo-300 border border-indigo-500/30 hover:bg-indigo-500/25"
              >
                Edit
              </button>
              <button
                onClick={handleDelete} disabled={busy}
                className="px-2 py-1 rounded bg-red-500/10 text-red-400 border border-red-500/30 hover:bg-red-500/20"
              >
                Delete
              </button>
            </>
          )}
        </div>
      </div>
    </div>
  )
}
