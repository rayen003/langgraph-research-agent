import type { ActivityEntry } from './lib/activity'

export type Mode = 'auto' | 'research' | 'chat'
export type Intent = 'research' | 'chat' | null

export type RunStatus =
  | 'idle'
  | 'classifying'
  | 'planning'
  | 'workflow_running'
  | 'awaiting_assumptions'
  | 'awaiting_approval'
  | 'executing'
  | 'synthesizing'
  | 'complete'
  | 'error'
  | 'rejected'
  | 'chat_responding'

export type StepStatus = 'pending' | 'running' | 'completed' | 'failed'
export type ToolStatus = 'running' | 'done' | 'error'

export interface ToolCall {
  tool_name: string
  status: ToolStatus
  summary: string
  args_preview: string
}

export interface StepState {
  id: string
  description: string
  depends_on: string[]
  status: StepStatus
  tool_calls: ToolCall[]
  reasoning: string
}

export interface ChatMessage {
  id: string
  role: 'user' | 'assistant'
  content: string
  streaming?: boolean
}

export interface AgentRunState {
  status: RunStatus
  thread_id: string | null
  query: string
  mode: Mode
  resolved_intent: Intent
  // Research fields
  steps: StepState[]
  report: string
  artifact_paths: string[]
  error: string | null
  completed_steps: number
  // Chat fields
  chat_messages: ChatMessage[]
  /**
   * Unified activity log — fed by `type: "activity"` events from the
   * backend's `track_tool` / `track_workflow_step` helpers. Replaces the
   * removed `chat_tool_calls` array and the per-step `tool_calls` events.
   * `step.tool_calls` is still populated as a derived projection so
   * existing renderers (StepCard, ResearchStepsTrace) keep working.
   */
  activity: ActivityEntry[]
  dcf_review?: DcfReviewState | null
  dcf_evidence_items?: EvidenceItem[]
  dcf_citation_map?: Record<string, string>
}

export interface EvidenceItem {
  evidence_id: string
  kind: string
  source_tier: 'filing' | 'structured_api' | 'document' | 'news' | 'generic_web' | string
  source: string
  as_of: string
  title?: string
  url?: string
  text?: string
  field?: string
  value?: number
  section?: string
  filing_type?: string
  inferred?: boolean
}

export interface ConfidenceComponent {
  score: number
  label: 'high' | 'medium' | 'low'
  reason: string
}

export interface ConfidenceBreakdown {
  components: Record<string, ConfidenceComponent>
  aggregate_score: number
  label: 'high' | 'medium' | 'low'
  summary: string
}

export interface DcfReviewState {
  ticker: string
  horizon_years: number
  assumptions: Record<string, number>
  provenance: Record<string, { source?: string; confidence?: number; evidence_refs?: string[] }>
  memo_proposals?: Record<string, { rationale: string; confidence: number }>
  evidence_items?: EvidenceItem[]
}

// ── Session / history types ───────────────────────────────────────────────

export type SessionMessageType = 'user' | 'chat_response' | 'research_report'

export interface SessionMessage {
  id: string
  type: SessionMessageType
  content: string
  /** Only set on research_report messages */
  threadId?: string
  artifactPaths?: string[]
  /** Snapshot of tool calls captured at commit time (chat or research). */
  toolTrace?: ToolCall[]
  /** Snapshot of research step timeline at commit time (research only). */
  researchSteps?: StepState[]
  /**
   * Snapshot of the unified activity log captured at commit time.
   * Renders the same auditable view used for live runs.
   */
  activity?: ActivityEntry[]
  dcfEvidenceItems?: EvidenceItem[]
  dcfCitationMap?: Record<string, string>
  /**
   * DCF validity captured at commit time. When 'invalid', the message
   * renders with a red degraded banner so the user can't miss it.
   */
  validity?: 'valid' | 'invalid' | 'adjusting'
  /** Reason text emitted by convergence_gate when validity != 'valid'. */
  invalidationReason?: string
}

export interface Session {
  id: string
  title: string
  /** Stable LangGraph thread_id reused for chat multi-turn within this session */
  chatThreadId: string
  messages: SessionMessage[]
  createdAt: string
}

export interface DocumentInfo {
  doc_id: string
  filename: string
  session_id: string
  status: 'processing' | 'ready' | 'error'
  chunk_count: number
  page_count: number
  error?: string | null
  created_at: number
}

export interface JobSummary {
  thread_id: string
  query: string
  status: string
  mode: string
  intent: string | null
  created_at: string
}
