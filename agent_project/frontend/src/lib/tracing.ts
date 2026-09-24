export type TraceCategory = 'run' | 'transport' | 'node' | 'model' | 'tool' | 'memory'
export type TraceStatus = 'started' | 'running' | 'completed' | 'error'

export interface TraceSpanEvent {
  type: 'trace_span'
  trace_id: string
  span_id: string
  parent_span_id?: string
  category: TraceCategory
  name: string
  status: TraceStatus
  started_at?: number
  ended_at?: number
  duration_ms?: number
  metadata?: Record<string, unknown>
  error?: string
}

export interface TraceSpan extends Omit<TraceSpanEvent, 'type'> {}

export function isTraceSpanEvent(data: Record<string, unknown>): data is Record<string, unknown> & TraceSpanEvent {
  return data.type === 'trace_span'
    && typeof data.trace_id === 'string'
    && typeof data.span_id === 'string'
}

export function mergeTraceSpan(spans: TraceSpan[], event: TraceSpanEvent): TraceSpan[] {
  const incoming: TraceSpan = {
    trace_id: event.trace_id,
    span_id: event.span_id,
    parent_span_id: event.parent_span_id,
    category: event.category,
    name: event.name,
    status: event.status,
    started_at: event.started_at,
    ended_at: event.ended_at,
    duration_ms: event.duration_ms,
    metadata: event.metadata,
    error: event.error,
  }
  const index = spans.findIndex(span => span.span_id === event.span_id)
  if (index < 0) return [...spans, incoming]
  const previous = spans[index]
  const next = [...spans]
  next[index] = {
    ...previous,
    ...incoming,
    started_at: previous.started_at ?? incoming.started_at,
    metadata: { ...(previous.metadata ?? {}), ...(incoming.metadata ?? {}) },
    error: incoming.error ?? previous.error,
  }
  return next
}
