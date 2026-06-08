import { useState, useCallback, type ReactNode } from 'react'
import type { ActivityEntry } from '../../lib/activity'
import { ActivityBlock } from './ActivityBlock'

/**
 * Vertical stack of ActivityBlocks with airy rhythm and auto-collapse-on-
 * complete: the running block's detail stays open; finished blocks show just
 * the header + one-line summary (the list stays exhaustive — every step is a
 * visible card). A manual toggle sticks.
 *
 * Parent→child (workflow → substeps) renders as **indented siblings under a
 * faint guide line** — not a nested bordered box (that produced the spine
 * bleed). Cards are borderless soft fills, so containment reads from indent +
 * whitespace.
 */

interface Props {
  entries: ActivityEntry[]
  compact?: boolean
  renderBody?: (entry: ActivityEntry) => ReactNode
  emptyState?: ReactNode
}

export function BlockStack({ entries, compact = false, renderBody, emptyState }: Props) {
  const [overrides, setOverrides] = useState<Record<string, boolean>>({})

  const toggle = useCallback((id: string, currentlyOpen: boolean) => {
    setOverrides(prev => ({ ...prev, [id]: !currentlyOpen }))
  }, [])

  if (entries.length === 0) return <>{emptyState ?? null}</>

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
    return e.status === 'running' || e.status === 'started' || e.status === 'awaiting_input'
  }

  const renderOne = (e: ActivityEntry, index: number): ReactNode => {
    const open = isOpen(e)
    const kids = childrenByParent.get(e.activity_id) || []
    const body = renderBody?.(e)

    return (
      <div key={e.activity_id}>
        <ActivityBlock
          entry={e}
          open={open}
          onToggle={() => toggle(e.activity_id, open)}
          index={index}
          compact={compact}
        >
          {body}
        </ActivityBlock>
        {kids.length > 0 && (
          <div className="mt-1.5 ml-2.5 pl-3 border-l border-edge/40 space-y-1.5">
            {kids.map((k, i) => renderOne(k, i))}
          </div>
        )}
      </div>
    )
  }

  return (
    <div className="space-y-1.5">
      {roots.map((e, i) => renderOne(e, i))}
    </div>
  )
}
