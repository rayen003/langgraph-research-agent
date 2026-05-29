import { useState, useCallback, useEffect } from 'react'
import type { Session, SessionMessage } from '../types'

const STORAGE_KEY = 'rAgent_sessions_v1'
const MAX_SESSIONS = 30

let _counter = 0
const uid = () => `${Date.now()}_${++_counter}`

function makeSession(title = 'New session'): Session {
  return {
    id: `s_${uid()}`,
    title,
    chatThreadId: `chat_${Math.random().toString(36).slice(2, 10)}`,
    messages: [],
    createdAt: new Date().toISOString(),
  }
}

function load(): { sessions: Session[]; activeId: string } {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (raw) {
      const parsed = JSON.parse(raw) as { sessions: Session[]; activeId: string }
      if (parsed.sessions?.length) return parsed
    }
  } catch { /* ignore */ }
  const s = makeSession()
  return { sessions: [s], activeId: s.id }
}

function save(sessions: Session[], activeId: string) {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify({ sessions, activeId }))
  } catch { /* ignore quota */ }
}

interface State {
  sessions: Session[]
  activeId: string
}

export function useSessionManager() {
  const [state, setState] = useState<State>(load)

  // Persist on every change
  useEffect(() => {
    save(state.sessions, state.activeId)
  }, [state])

  const activeSession = state.sessions.find(s => s.id === state.activeId) ?? state.sessions[0]

  const newSession = useCallback((opts?: { activate?: boolean }) => {
    const activate = opts?.activate !== false
    const s = makeSession()
    setState(prev => ({
      sessions: [s, ...prev.sessions].slice(0, MAX_SESSIONS),
      activeId: activate ? s.id : prev.activeId,
    }))
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
        return { sessions: [s], activeId: s.id }
      }
      const activeId = prev.activeId === id ? sessions[0].id : prev.activeId
      return { sessions, activeId }
    })
  }, [])

  /** Sync the stable LangGraph thread for a session's chat history. */
  const updateChatThreadId = useCallback((sessionId: string, chatThreadId: string) => {
    setState(prev => ({
      ...prev,
      sessions: prev.sessions.map(s =>
        s.id === sessionId ? { ...s, chatThreadId } : s,
      ),
    }))
  }, [])

  /** Append a message to the given session, updating title from first user msg. */
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

  /** Drop all messages with index >= fromIndex. Used when amending a prior user message. */
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
    activeSession,
    newSession,
    selectSession,
    deleteSession,
    addMessage,
    truncateMessagesFrom,
    updateChatThreadId,
  }
}
