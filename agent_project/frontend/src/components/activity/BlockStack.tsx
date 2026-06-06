import { useState, useCallback, type ReactNode } from 'react'
import type { ActivityEntry } from '../../lib/activity'
import { ActivityBlock } from './ActivityBlock'

/**
 * Vertical stack of ActivityBlocks with consistent rhythm (gap + whitespace),
 * staggered appearance, and auto-collapse-on-complete: the running block stays
 * expanded; finished blocks collapse to their one-line header. The analyst can
 * override either way by clicking — a manual toggle sticks.
 *
 * Parent-child nesting (workflow → substeps) is derived from
 * `parent_activity_id`; children render indented beneath their parent.
 */

interface Props {
  entries: ActivityEntry[]
  compact?: boolean
  /** Render a per-entry detail body (existing ActivityTrace renderers). */
  renderBody?: (entry: ActivityEntry) => ReactNode
  emptyState?: ReactNode
}

export function BlockStack({ entries, compact = false, renderBody, emptyState }: Props) {
  // activity_id → user's explicit open/closed override (sticks past status change).
  const [overrides, setOverrides] = useState<Record<string, boolean>>({})

  const toggle = useCallback((id: string, currentlyOpen: boolean) => {
    setOverrides(prev => ({ ...prev, [id]: !currentlyOpen }))
  }, [])

  if (entries.length === 0) return <>{emptyState ?? null}</>

  // Group children under parents; top-level = no parent (or parent absent).
  const ids = new Set(entries.map(e => e.activity_id))
  const childrenByParent = new Map<string, ActivityEntry[]>()
  const roots: ActivityEntry[] = []
  for (const e of entries) {
    const pid = e.parent_activity_id
    if (pid && ids.has(pid)) {
      const arr = childrenByParent.get(pid) || []
      arr.push(e)
      childrenByParent.set(pid, arr)
    } else {
      roots.push(e)
    }
  }

  const isOpen = (e: ActivityEntry): boolean => {
    if (e.activity_id in overrides) return overrides[e.activity_id]
    // Default: running/awaiting expanded, terminal states collapsed.
    return e.status === 'running' || e.status === 'started' || e.status === 'awaiting_input'
  }

  const renderOne = (e: ActivityEntry, index: number, nested: boolean): ReactNode => {
    const open = isOpen(e)
    const kids = childrenByParent.get(e.activity_id) || []
    const body = renderBody?.(e)
    // Body = the detail renderer plus any nested child blocks.
    const fullBody = (body || kids.length > 0) ? (
      <div className="space-y-2">
        {body}
        {kids.length > 0 && (
          <div className="space-y-2 pt-1">
            {kids.map((k, i) => renderOne(k, i, true))}
          </div>
        )}
      </div>
    ) : undefined

    return (
      <ActivityBlock
        key={e.activity_id}
        entry={e}
        open={open}
        onToggle={() => toggle(e.activity_id, open)}
        index={index}
        compact={compact}
        nested={nested}
      >
        {fullBody}
      </ActivityBlock>
    )
  }

  return (
    <div className="space-y-2.5">
      {roots.map((e, i) => renderOne(e, i, false))}
    </div>
  )
}
