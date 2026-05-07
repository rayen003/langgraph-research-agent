// Shared ActivityEvent contract — mirror of agent_project/activity.py.
// Keep these two files in lockstep.

export const ACTIVITY_EVENT_TYPE = 'activity'

export type ActivityKind =
  | 'tool'
  | 'workflow'
  | 'workflow_step'
  | 'node'

export type ActivityStatus =
  | 'started'
  | 'running'
  | 'completed'
  | 'skipped'
  | 'error'
  | 'awaiting_input'

export type ActivityScope = 'chat' | 'research' | 'workflow'

export interface ActivityEvent {
  type: typeof ACTIVITY_EVENT_TYPE
  activity_id: string
  kind: ActivityKind
  name: string
  scope: ActivityScope
  status: ActivityStatus
  parent_activity_id?: string
  display_label?: string
  step_id?: string
  started_at?: number
  ended_at?: number
  summary?: string
  args_preview?: string
  confidence_label?: string
  flag_count?: number
  error?: string
  meta?: Record<string, unknown>
}

// Frontend-side aggregation: a single Activity can receive multiple
// events (start -> running -> completed). The store collapses them into
// one entry keyed by activity_id, preserving the most recent status.
export interface ActivityEntry {
  activity_id: string
  parent_activity_id?: string
  kind: ActivityKind
  name: string
  display_label?: string
  scope: ActivityScope
  status: ActivityStatus
  step_id?: string
  started_at?: number
  ended_at?: number
  summary: string
  args_preview: string
  confidence_label?: string
  flag_count?: number
  error?: string
  meta?: Record<string, unknown>
}

export function isActivityEvent(data: Record<string, unknown>): boolean {
  return data?.type === ACTIVITY_EVENT_TYPE && typeof data.activity_id === 'string'
}

/** Map an ActivityEntry's lifecycle status into the legacy `ToolCall.status`
 *  triplet (`running` / `done` / `error`) used by `StepCard`,
 *  `ResearchStepsTrace`, and the activity row renderer. */
export function activityStatusToToolStatus(
  status: ActivityStatus,
): 'running' | 'done' | 'error' {
  if (status === 'completed' || status === 'skipped') return 'done'
  if (status === 'error') return 'error'
  return 'running'
}

/**
 * Merge an incoming ActivityEvent into an existing entry list.
 *
 * - If `activity_id` already exists, fields from the new event win
 *   (status, ended_at, summary, error, ...) but timestamps and other
 *   already-set fields on the existing entry are preserved when the new
 *   event omits them.
 * - Otherwise the entry is appended.
 *
 * Returned list is a new array (immutable update friendly).
 */
export function mergeActivity(
  entries: ActivityEntry[],
  event: ActivityEvent,
): ActivityEntry[] {
  const idx = entries.findIndex(e => e.activity_id === event.activity_id)
  const incoming: ActivityEntry = {
    activity_id: event.activity_id,
    parent_activity_id: event.parent_activity_id,
    kind: event.kind,
    name: event.name,
    display_label: event.display_label,
    scope: event.scope,
    status: event.status,
    step_id: event.step_id,
    started_at: event.started_at,
    ended_at: event.ended_at,
    summary: event.summary ?? '',
    args_preview: event.args_preview ?? '',
    confidence_label: event.confidence_label,
    flag_count: event.flag_count,
    error: event.error,
    meta: event.meta,
  }

  if (idx < 0) return [...entries, incoming]

  const prev = entries[idx]
  const merged: ActivityEntry = {
    ...prev,
    ...incoming,
    started_at: prev.started_at ?? incoming.started_at,
    summary: incoming.summary || prev.summary,
    args_preview: incoming.args_preview || prev.args_preview,
    display_label: incoming.display_label ?? prev.display_label,
    confidence_label: incoming.confidence_label ?? prev.confidence_label,
    flag_count: incoming.flag_count ?? prev.flag_count,
    error: incoming.error ?? prev.error,
    meta: incoming.meta ?? prev.meta,
  }
  const next = [...entries]
  next[idx] = merged
  return next
}
