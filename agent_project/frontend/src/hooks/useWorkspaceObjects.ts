import { useEffect, useRef, useState } from 'react'
import type { WorkspaceObject } from '../types'

const POLL_INTERVAL = 5000

export function useWorkspaceObjects(sessionId?: string, enabled = true) {
  const [objects, setObjects] = useState<WorkspaceObject[]>([])
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null)

  useEffect(() => {
    if (!enabled) {
      setObjects([])
      return
    }

    const fetchObjects = async () => {
      try {
        const qs = sessionId ? `?session_id=${encodeURIComponent(sessionId)}` : ''
        const res = await fetch(`/workspace/objects${qs}`)
        if (!res.ok) return
        const data = (await res.json()) as WorkspaceObject[]
        setObjects(data)
      } catch {
        // backend may be offline during local startup
      }
    }

    fetchObjects()
    timerRef.current = setInterval(fetchObjects, POLL_INTERVAL)
    return () => {
      if (timerRef.current) clearInterval(timerRef.current)
      timerRef.current = null
    }
  }, [enabled, sessionId])

  return { objects }
}
