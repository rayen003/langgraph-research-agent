import { useState, useCallback, useEffect } from 'react'

// ── Shapes ───────────────────────────────────────────────────────────────────

export interface KgNode {
  id: string
  session_id: string | null
  ticker: string
  node_type: string
  field: string
  value: unknown
  confidence: number
  source: string
  input_hash: string | null
  run_id: string | null
  created_at: number
  updated_at: number
}

export interface KgEdge {
  id: string
  session_id: string | null
  src_id: string
  tgt_id: string
  relation: string
  confidence: number
  source: string
  created_at: number
}

export interface KgQueryResult {
  query: unknown
  answer: string
  matched_nodes: KgNode[]
  traversal_path: string[]
}

// ── Hook ─────────────────────────────────────────────────────────────────────

export function useKnowledgeGraph(sessionId: string | null) {
  const [nodes, setNodes] = useState<KgNode[]>([])
  const [edges, setEdges] = useState<KgEdge[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [highlightPath, setHighlightPath] = useState<string[]>([])

  const refresh = useCallback(async () => {
    if (!sessionId) return
    setLoading(true); setError(null)
    try {
      const res = await fetch(`/kg/${sessionId}`)
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      const data = (await res.json()) as { nodes: KgNode[]; edges: KgEdge[] }
      setNodes(data.nodes ?? [])
      setEdges(data.edges ?? [])
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }, [sessionId])

  // Initial fetch on session change
  useEffect(() => { refresh() }, [refresh])

  const patchNode = useCallback(async (nodeId: string, patch: { value?: unknown; confidence?: number }) => {
    if (!sessionId) return
    await fetch(`/kg/${sessionId}/nodes/${encodeURIComponent(nodeId)}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(patch),
    })
    await refresh()
  }, [sessionId, refresh])

  const deleteNode = useCallback(async (nodeId: string) => {
    if (!sessionId) return
    await fetch(`/kg/${sessionId}/nodes/${encodeURIComponent(nodeId)}`, { method: 'DELETE' })
    await refresh()
  }, [sessionId, refresh])

  const createNode = useCallback(async (n: {
    ticker: string; node_type: string; field: string
    value: unknown; confidence?: number; source?: string
  }) => {
    if (!sessionId) return
    await fetch(`/kg/${sessionId}/nodes`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ confidence: 1.0, source: 'user_stated', ...n }),
    })
    await refresh()
  }, [sessionId, refresh])

  const queryNL = useCallback(async (question: string, ticker?: string): Promise<KgQueryResult | null> => {
    if (!sessionId) return null
    const res = await fetch(`/kg/${sessionId}/query`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ question, ticker }),
    })
    if (!res.ok) return null
    const result = (await res.json()) as KgQueryResult
    setHighlightPath(result.traversal_path ?? [])
    return result
  }, [sessionId])

  const clearHighlight = useCallback(() => setHighlightPath([]), [])

  return {
    nodes, edges, loading, error,
    highlightPath, clearHighlight,
    refresh, patchNode, deleteNode, createNode, queryNL,
  }
}
