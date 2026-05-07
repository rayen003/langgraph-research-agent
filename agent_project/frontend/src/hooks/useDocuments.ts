import { useState, useCallback, useEffect, useRef } from 'react'
import type { DocumentInfo } from '../types'

export function useDocuments(sessionId: string) {
  const [docs, setDocs] = useState<DocumentInfo[]>([])
  const pollTimers = useRef<Map<string, ReturnType<typeof setInterval>>>(new Map())

  // Clear documents and cancel pollers when session changes
  useEffect(() => {
    pollTimers.current.forEach(t => clearInterval(t))
    pollTimers.current.clear()
    setDocs([])
  }, [sessionId])

  // Cancel all pollers on unmount
  useEffect(() => {
    return () => {
      pollTimers.current.forEach(t => clearInterval(t))
    }
  }, [])

  const _pollStatus = useCallback((docId: string) => {
    const timer = setInterval(async () => {
      try {
        const res = await fetch(`/documents/${docId}/status`)
        if (!res.ok) return
        const info = (await res.json()) as DocumentInfo
        setDocs(prev => prev.map(d => d.doc_id === docId ? info : d))
        if (info.status !== 'processing') {
          clearInterval(pollTimers.current.get(docId))
          pollTimers.current.delete(docId)
        }
      } catch { /* ignore network errors during polling */ }
    }, 1200)
    pollTimers.current.set(docId, timer)
  }, [])

  const upload = useCallback(async (file: File): Promise<DocumentInfo | null> => {
    if (!sessionId) return null
    const formData = new FormData()
    formData.append('file', file)
    formData.append('session_id', sessionId)

    try {
      const res = await fetch('/documents', { method: 'POST', body: formData })
      if (!res.ok) return null
      const info = (await res.json()) as DocumentInfo
      setDocs(prev => [...prev, info])
      _pollStatus(info.doc_id)
      return info
    } catch {
      return null
    }
  }, [sessionId, _pollStatus])

  const remove = useCallback(async (docId: string) => {
    clearInterval(pollTimers.current.get(docId))
    pollTimers.current.delete(docId)
    setDocs(prev => prev.filter(d => d.doc_id !== docId))
    try {
      await fetch(`/documents/${docId}`, { method: 'DELETE' })
    } catch { /* ignore */ }
  }, [])

  return { docs, upload, remove }
}
