import type { Session, SessionGroup } from '../types'

export interface SessionLayoutSyncPayload {
  groups: Array<{
    id: string
    name: string
    color: string
    collapsed: boolean
    sort_order: number
    created_at: string
  }>
  sessions: Array<{
    session_id: string
    title_override: string | null
    pinned: boolean
    group_id: string | null
    sort_order: number
    updated_at: string
  }>
}

export function toSyncPayload(
  sessions: Session[],
  groups: SessionGroup[],
): SessionLayoutSyncPayload {
  const now = new Date().toISOString()
  return {
    groups: groups.map(g => ({
      id: g.id,
      name: g.name,
      color: g.color,
      collapsed: !!g.collapsed,
      sort_order: g.sortOrder,
      created_at: g.createdAt,
    })),
    sessions: sessions.map(s => ({
      session_id: s.id,
      title_override: s.title,
      pinned: !!s.pinned,
      group_id: s.groupId ?? null,
      sort_order: s.sortOrder ?? 0,
      updated_at: now,
    })),
  }
}

export async function fetchSessionLayout(): Promise<SessionLayoutSyncPayload | null> {
  try {
    const res = await fetch('/sessions/layout')
    if (!res.ok) return null
    return (await res.json()) as SessionLayoutSyncPayload
  } catch {
    return null
  }
}

export async function pushSessionLayout(payload: SessionLayoutSyncPayload): Promise<void> {
  try {
    await fetch('/sessions/layout', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    })
  } catch {
    /* offline — localStorage remains source of truth */
  }
}

export function mergeLayoutFromServer(
  sessions: Session[],
  groups: SessionGroup[],
  remote: SessionLayoutSyncPayload,
): { sessions: Session[]; groups: SessionGroup[] } {
  const remoteGroups: SessionGroup[] = remote.groups.map(g => ({
    id: g.id,
    name: g.name,
    color: g.color as SessionGroup['color'],
    collapsed: g.collapsed,
    sortOrder: g.sort_order,
    createdAt: g.created_at,
  }))

  const metaById = new Map(remote.sessions.map(s => [s.session_id, s]))
  const mergedSessions = sessions.map(s => {
    const meta = metaById.get(s.id)
    if (!meta) return s
    return {
      ...s,
      title: meta.title_override ?? s.title,
      pinned: meta.pinned,
      groupId: meta.group_id,
      sortOrder: meta.sort_order,
    }
  })

  return { sessions: mergedSessions, groups: remoteGroups }
}
