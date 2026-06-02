import { useState, useCallback, useEffect, useRef } from 'react'
import type { DocumentInfo } from '../types'

function makePendingDoc(sessionId: string, file: File): DocumentInfo {
  return {
    doc_id: `pending_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`,
    filename: file.name,
    session_id: sessionId,
    status: 'processing',
    chunk_count: 0,
    page_count: 0,
    created_at: Date.now() / 1000,
  }
}

export function useDocuments(sessionId: string, onDocReady?: (doc: DocumentInfo) => void) {
  const [docs, setDocs] = useState<DocumentInfo[]>([])
  const pollTimers = useRef<Map<string, ReturnType<typeof setInterval>>>(new Map())
  const onDocReadyRef = useRef(onDocReady)
  useEffect(() => { onDocReadyRef.current = onDocReady }, [onDocReady])

  useEffect(() => {
    pollTimers.current.forEach(t => clearInterval(t))
    pollTimers.current.clear()
    setDocs([])
    if (!sessionId) return

    let cancelled = false
    ;(async () => {
      try {
        const res = await fetch(`/documents?session_id=${encodeURIComponent(sessionId)}`)
        if (!res.ok || cancelled) return
        const list = (await res.json()) as DocumentInfo[]
        if (!cancelled) setDocs(list)
      } catch { /* offline */ }
    })()

    return () => { cancelled = true }
  }, [sessionId])

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
          if (info.status === 'ready') onDocReadyRef.current?.(info)
        }
      } catch { /* ignore network errors during polling */ }
    }, 1200)
    pollTimers.current.set(docId, timer)
  }, [])

  const upload = useCallback(async (file: File): Promise<DocumentInfo | null> => {
    if (!sessionId) return null

    const pending = makePendingDoc(sessionId, file)
    setDocs(prev => [...prev, pending])

    const formData = new FormData()
    formData.append('file', file)
    formData.append('session_id', sessionId)

    try {
      const res = await fetch('/documents', { method: 'POST', body: formData })
      if (!res.ok) {
        setDocs(prev => prev.filter(d => d.doc_id !== pending.doc_id))
        return null
      }
      const info = (await res.json()) as DocumentInfo
      setDocs(prev => prev.map(d => (d.doc_id === pending.doc_id ? info : d)))
      _pollStatus(info.doc_id)
      return info
    } catch {
      setDocs(prev => prev.filter(d => d.doc_id !== pending.doc_id))
      return null
    }
  }, [sessionId, _pollStatus])

  const remove = useCallback(async (docId: string) => {
    clearInterval(pollTimers.current.get(docId))
    pollTimers.current.delete(docId)
    setDocs(prev => prev.filter(d => d.doc_id !== docId))
    if (docId.startsWith('pending_')) return
    try {
      await fetch(`/documents/${docId}`, { method: 'DELETE' })
    } catch { /* ignore */ }
  }, [])

  return { docs, upload, remove }
}
