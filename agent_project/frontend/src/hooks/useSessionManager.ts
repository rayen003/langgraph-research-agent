import { useState, useCallback, useEffect, useRef } from 'react'
import type { Session, SessionGroup, SessionGroupColor, SessionMessage } from '../types'
import {
  fetchSessionLayout,
  mergeLayoutFromServer,
  pushSessionLayout,
  toSyncPayload,
} from '../api/sessionLayout'

const STORAGE_KEY = 'rAgent_sessions_v2'
const LEGACY_KEY = 'rAgent_sessions_v1'
const MAX_SESSIONS = 30

let _counter = 0
const uid = () => `${Date.now()}_${++_counter}`

interface Store {
  sessions: Session[]
  groups: SessionGroup[]
  activeId: string
}

function makeSession(title = 'New session'): Session {
  return {
    id: `s_${uid()}`,
    title,
    chatThreadId: `chat_${Math.random().toString(36).slice(2, 10)}`,
    messages: [],
    createdAt: new Date().toISOString(),
    pinned: false,
    groupId: null,
    sortOrder: 0,
  }
}

function normalizeSession(s: Session, index: number): Session {
  return {
    ...s,
    pinned: !!s.pinned,
    groupId: s.groupId ?? null,
    sortOrder: s.sortOrder ?? index,
  }
}

function loadLocal(): Store {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (raw) {
      const parsed = JSON.parse(raw) as Store
      if (parsed.sessions?.length) {
        return {
          sessions: parsed.sessions.map(normalizeSession),
          groups: parsed.groups ?? [],
          activeId: parsed.activeId,
        }
      }
    }
  } catch { /* ignore */ }

  // Migrate v1 → v2
  try {
    const legacy = localStorage.getItem(LEGACY_KEY)
    if (legacy) {
      const parsed = JSON.parse(legacy) as { sessions: Session[]; activeId: string }
      if (parsed.sessions?.length) {
        return {
          sessions: parsed.sessions.map(normalizeSession),
          groups: [],
          activeId: parsed.activeId,
        }
      }
    }
  } catch { /* ignore */ }

  const s = makeSession()
  return { sessions: [s], groups: [], activeId: s.id }
}

function saveLocal(store: Store) {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(store))
  } catch { /* quota */ }
}

function sortByOrder(a: { sortOrder?: number }, b: { sortOrder?: number }) {
  return (a.sortOrder ?? 0) - (b.sortOrder ?? 0)
}

function reindex(ids: string[], sessions: Session[]): Session[] {
  const order = new Map(ids.map((id, i) => [id, i]))
  return sessions.map(s => (order.has(s.id) ? { ...s, sortOrder: order.get(s.id)! } : s))
}

const GROUP_COLORS: SessionGroupColor[] = ['blue', 'teal', 'indigo', 'slate', 'violet', 'amber']

export function useSessionManager() {
  const [state, setState] = useState<Store>(loadLocal)
  const syncTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const hydrated = useRef(false)

  const scheduleSync = useCallback((store: Store) => {
    if (syncTimer.current) clearTimeout(syncTimer.current)
    syncTimer.current = setTimeout(() => {
      void pushSessionLayout(toSyncPayload(store.sessions, store.groups))
    }, 400)
  }, [])

  useEffect(() => {
    saveLocal(state)
    if (hydrated.current) scheduleSync(state)
  }, [state, scheduleSync])

  useEffect(() => {
    void (async () => {
      const remote = await fetchSessionLayout()
      if (remote?.groups?.length || remote?.sessions?.length) {
        setState(prev => {
          const merged = mergeLayoutFromServer(prev.sessions, prev.groups, remote)
          return { ...prev, ...merged }
        })
      }
      hydrated.current = true
    })()
    return () => {
      if (syncTimer.current) clearTimeout(syncTimer.current)
    }
  }, [])

  const activeSession = state.sessions.find(s => s.id === state.activeId) ?? state.sessions[0]

  const newSession = useCallback((opts?: { activate?: boolean; groupId?: string | null }) => {
    const activate = opts?.activate !== false
    const s = makeSession()
    if (opts?.groupId) s.groupId = opts.groupId
    setState(prev => {
      let sessions = [s, ...prev.sessions]
      if (sessions.length > MAX_SESSIONS) {
        for (let i = sessions.length - 1; i >= 0 && sessions.length > MAX_SESSIONS; i--) {
          if (!sessions[i].pinned && sessions[i].id !== s.id) {
            sessions = [...sessions.slice(0, i), ...sessions.slice(i + 1)]
          }
        }
      }
      return {
        ...prev,
        sessions: sessions.map((x, i) => ({ ...x, sortOrder: x.sortOrder ?? i })),
        activeId: activate ? s.id : prev.activeId,
      }
    })
    return s
  }, [])

  const selectSession = useCallback((id: string) => {
    setState(prev => ({ ...prev, activeId: id }))
  }, [])

  const deleteSession = useCallback((id: string) => {
    setState(prev => {
      const sessions = prev.sessions.filter(s => s.id !== id)
      if (sessions.length === 0) {
        const s = makeSession()
        return { sessions: [s], groups: prev.groups, activeId: s.id }
      }
      const activeId = prev.activeId === id ? sessions[0].id : prev.activeId
      return { ...prev, sessions, activeId }
    })
  }, [])

  const renameSession = useCallback((id: string, title: string) => {
    const trimmed = title.trim()
    if (!trimmed) return
    setState(prev => ({
      ...prev,
      sessions: prev.sessions.map(s => (s.id === id ? { ...s, title: trimmed } : s)),
    }))
  }, [])

  const pinSession = useCallback((id: string, pinned: boolean) => {
    setState(prev => ({
      ...prev,
      sessions: prev.sessions.map(s => (s.id === id ? { ...s, pinned } : s)),
    }))
  }, [])

  const createGroup = useCallback((name?: string, color?: SessionGroupColor) => {
    const gRef: { current: SessionGroup | null } = { current: null }
    setState(prev => {
      const g: SessionGroup = {
        id: `g_${uid()}`,
        name: (name?.trim() || 'New group'),
        color: color ?? GROUP_COLORS[prev.groups.length % GROUP_COLORS.length],
        collapsed: false,
        sortOrder: prev.groups.length,
        createdAt: new Date().toISOString(),
      }
      gRef.current = g
      return { ...prev, groups: [...prev.groups, g] }
    })
    return gRef.current!
  }, [])

  const updateGroup = useCallback((id: string, patch: Partial<Pick<SessionGroup, 'name' | 'color' | 'collapsed'>>) => {
    setState(prev => ({
      ...prev,
      groups: prev.groups.map(g => (g.id === id ? { ...g, ...patch } : g)),
    }))
  }, [])

  const deleteGroup = useCallback((id: string) => {
    setState(prev => ({
      ...prev,
      groups: prev.groups.filter(g => g.id !== id),
      sessions: prev.sessions.map(s => (s.groupId === id ? { ...s, groupId: null } : s)),
    }))
  }, [])

  const moveSessionToGroup = useCallback((sessionId: string, groupId: string | null) => {
    setState(prev => {
      const targetSessions = prev.sessions.filter(s =>
        groupId ? s.groupId === groupId : !s.groupId && !s.pinned,
      )
      const nextOrder = targetSessions.length
      return {
        ...prev,
        sessions: prev.sessions.map(s =>
          s.id === sessionId ? { ...s, groupId, sortOrder: nextOrder } : s,
        ),
      }
    })
  }, [])

  const reorderSessions = useCallback((orderedIds: string[]) => {
    setState(prev => ({
      ...prev,
      sessions: reindex(orderedIds, prev.sessions),
    }))
  }, [])

  const reorderGroups = useCallback((orderedIds: string[]) => {
    setState(prev => ({
      ...prev,
      groups: prev.groups
        .map(g => {
          const idx = orderedIds.indexOf(g.id)
          return idx >= 0 ? { ...g, sortOrder: idx } : g
        })
        .sort(sortByOrder),
    }))
  }, [])

  const updateChatThreadId = useCallback((sessionId: string, chatThreadId: string) => {
    setState(prev => ({
      ...prev,
      sessions: prev.sessions.map(s =>
        s.id === sessionId ? { ...s, chatThreadId } : s,
      ),
    }))
  }, [])

  const addMessage = useCallback((sessionId: string, msg: SessionMessage) => {
    setState(prev => ({
      ...prev,
      sessions: prev.sessions.map(s => {
        if (s.id !== sessionId) return s
        const messages = [...s.messages, msg]
        const title =
          s.title === 'New session' && msg.type === 'user'
            ? msg.content.slice(0, 60)
            : s.title
        return { ...s, messages, title }
      }),
    }))
  }, [])

  const truncateMessagesFrom = useCallback((sessionId: string, fromIndex: number) => {
    setState(prev => ({
      ...prev,
      sessions: prev.sessions.map(s => {
        if (s.id !== sessionId) return s
        return { ...s, messages: s.messages.slice(0, Math.max(0, fromIndex)) }
      }),
    }))
  }, [])

  return {
    sessions: state.sessions,
    groups: state.groups,
    activeSession,
    newSession,
    selectSession,
    deleteSession,
    renameSession,
    pinSession,
    createGroup,
    updateGroup,
    deleteGroup,
    moveSessionToGroup,
    reorderSessions,
    reorderGroups,
    addMessage,
    truncateMessagesFrom,
    updateChatThreadId,
  }
}
