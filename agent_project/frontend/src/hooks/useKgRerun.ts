import { useCallback, useRef, useState } from 'react'

interface RerunState {
  threadId: string | null
  status: 'idle' | 'running' | 'complete' | 'error'
  ticker: string | null
  error: string | null
}

interface RerunParams {
  ticker: string
  horizonYears: number
  overrides: Record<string, number>
  sessionId: string
  chatThreadId?: string          // reuse this LangGraph thread when defined (current-chat mode)
  onStarted?: (info: { threadId: string }) => void
  onComplete?: (info: { threadId: string; content?: string }) => void
  onError?: (msg: string) => void
}

/**
 * Trigger a DCF rerun by POSTing a [DCF_APPROVED] chat message to /runs.
 * The backend chat path handles fast-mode DCF (skips evidence/synthesis/thesis/memo
 * when all_assumptions is provided).
 *
 * Callbacks let the caller wire side effects (toast, session messages) without
 * coupling this hook to App-level state.
 */
export function useKgRerun() {
  const [state, setState] = useState<RerunState>({
    threadId: null,
    status: 'idle',
    ticker: null,
    error: null,
  })
  const esRef = useRef<EventSource | null>(null)
  const contentRef = useRef<string>('')

  const rerun = useCallback(async (params: RerunParams): Promise<string | null> => {
    const { ticker, horizonYears, overrides, sessionId, chatThreadId, onStarted, onComplete, onError } = params

    esRef.current?.close()
    esRef.current = null
    contentRef.current = ''

    const approvalPayload = {
      ticker,
      horizon_years: horizonYears,
      all_assumptions: overrides,
    }
    const query = `[DCF_APPROVED]:${JSON.stringify(approvalPayload)}`

    setState({ threadId: null, status: 'running', ticker, error: null })

    try {
      const res = await fetch('/runs', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          query,
          mode: 'chat',
          session_id: sessionId,
          thread_id: chatThreadId,  // undefined → backend creates fresh chat thread
        }),
      })
      if (!res.ok) {
        const errText = await res.text()
        setState(s => ({ ...s, status: 'error', error: errText }))
        onError?.(errText)
        return null
      }
      const { thread_id, start_event_id } = (await res.json()) as {
        thread_id: string
        start_event_id?: number
      }
      setState(s => ({ ...s, threadId: thread_id }))
      onStarted?.({ threadId: thread_id })

      const afterId = typeof start_event_id === 'number' ? start_event_id : 0
      const es = new EventSource(`/runs/${thread_id}/events?after_id=${afterId}`)
      esRef.current = es

      es.onmessage = (ev: MessageEvent) => {
        let data: Record<string, unknown>
        try { data = JSON.parse(ev.data as string) } catch { return }
        const type = data.type as string

        // Collect assistant tokens so we can hand back the full message
        if (type === 'chat_token' && typeof data.token === 'string') {
          contentRef.current += data.token
        }
        if (type === 'chat_complete' && typeof data.content === 'string') {
          // If we didn't stream tokens, the full content arrives here
          if (!contentRef.current) contentRef.current = data.content
        }

        if (type === 'done' || type === 'run_complete' || type === 'chat_complete') {
          setState(s => ({ ...s, status: 'complete' }))
          onComplete?.({ threadId: thread_id, content: contentRef.current || undefined })
          es.close()
          esRef.current = null
        } else if (type === 'error') {
          const msg = String(data.message || 'error')
          setState(s => ({ ...s, status: 'error', error: msg }))
          onError?.(msg)
          es.close()
          esRef.current = null
        }
      }
      es.onerror = () => {
        setState(s => {
          if (s.status === 'complete') return s
          return { ...s, status: 'error', error: 'Connection lost' }
        })
        es.close()
        esRef.current = null
      }

      return thread_id
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e)
      setState(s => ({ ...s, status: 'error', error: msg }))
      onError?.(msg)
      return null
    }
  }, [])

  const reset = useCallback(() => {
    esRef.current?.close()
    esRef.current = null
    setState({ threadId: null, status: 'idle', ticker: null, error: null })
  }, [])

  return { state, rerun, reset }
}
