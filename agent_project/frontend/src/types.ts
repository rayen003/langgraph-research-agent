import type { ActivityEntry } from './lib/activity'
import type { TraceSpan } from './lib/tracing'

export type Mode = 'auto' | 'research' | 'chat'
export type Intent = 'research' | 'chat' | null

export type RunStatus =
  | 'idle'
  | 'classifying'
  | 'planning'
  | 'workflow_running'
  | 'awaiting_workflow_context'
  | 'awaiting_assumptions'
  | 'awaiting_outline_review'
  | 'awaiting_memo_review'
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
  trace_spans: TraceSpan[]
  execution_trace: ExecutionTraceStep[]
  dcf_review?: DcfReviewState | null
  dcf_evidence_items?: EvidenceItem[]
  dcf_citation_map?: Record<string, string>
  deck_review?: DeckReviewState | null
  memo_review?: MemoReviewState | null
  workflow_context_review?: WorkflowContextReviewState | null
}

export interface ExecutionTraceTask {
  task_id: string
  capability_id: string
  objective: string
  dependency_task_ids: string[]
  required_output_types: string[]
}

export interface ExecutionTraceStep {
  type: 'execution_step'
  stage: string
  route_level?: string | null
  playbook_id?: string | null
  selected_workflow?: string | null
  confidence?: number | null
  latency_class?: string | null
  allowed_tools?: string[]
  tool_calls?: string[]
  object_ids?: string[]
  citation_ids?: string[]
  status?: string | null
  reason?: string | null
  case_id?: string
  task_id?: string
  objective?: string
  tasks?: ExecutionTraceTask[]
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

export interface DeckOutlineSlide {
  slide_id: string
  layout: string
  title: string
  block_refs: string[]
  notes?: string
}

export interface DeckBlockPreview {
  block_id: string
  kind: string
  title: string
  source_type: string
}

export interface DeckReviewState {
  deck_title: string
  hitl_mode: 'partial' | 'full' | 'disabled' | string
  slide_count: number
  outline: { slides: DeckOutlineSlide[]; rationale?: string }
  blocks_preview: DeckBlockPreview[]
}

export interface MemoSourceState {
  type: 'workspace_object' | 'document' | 'manual_text' | string
  title: string
  summary?: string
  object_id?: string | null
  version_id?: string | null
  doc_id?: string | null
}

export interface MemoDraftState {
  title: string
  executive_summary: string
  sections: Record<string, string>
  recommendation: string
  source_version_ids: string[]
  source_refs: Record<string, any>[]
  confidence: number
  limitations: string[]
}

export interface MemoReviewState {
  draft: MemoDraftState
  sources: MemoSourceState[]
  context_version_id?: string | null
}

export interface WorkflowContextReviewState {
  workflow_id: string
  title: string
  context: Record<string, any>
  controls: Array<{
    field: string
    label: string
    type: string
    options: Array<string | { value: string; label: string; selected?: boolean }>
  }>
}

// ── Session / history types ───────────────────────────────────────────────

export type SessionMessageType = 'user' | 'chat_response' | 'research_report'

export interface SessionMessage {
  id: string
  type: SessionMessageType
  content: string
  /** Files staged in the composer when this user message was sent. */
  attachedDocs?: AttachedDocSnapshot[]
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
  /** Runtime spans retained for post-run latency inspection. */
  traceSpans?: TraceSpan[]
  /** Router/playbook/case decisions retained with run output. */
  executionTrace?: ExecutionTraceStep[]
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

export type SessionGroupColor = 'blue' | 'teal' | 'indigo' | 'slate' | 'violet' | 'amber'

export interface SessionGroup {
  id: string
  name: string
  color: SessionGroupColor
  collapsed?: boolean
  sortOrder: number
  createdAt: string
}

export interface Session {
  id: string
  title: string
  /** Stable LangGraph thread_id reused for chat multi-turn within this session */
  chatThreadId: string
  messages: SessionMessage[]
  createdAt: string
  /** Pinned sessions float to the top of the sidebar. */
  pinned?: boolean
  /** Optional group membership for sidebar organization. */
  groupId?: string | null
  /** Sort index within pinned bucket, group, or ungrouped list. */
  sortOrder?: number
}

export type DocStage =
  | 'queued' | 'uploading' | 'parsing' | 'chunking' | 'embedding' | 'ready' | 'error'

export interface DocumentInfo {
  doc_id: string
  filename: string
  session_id: string
  status: 'processing' | 'ready' | 'error'
  /** Fine-grained ingest progress while status === 'processing'. */
  stage?: DocStage | null
  chunk_count: number
  page_count: number
  error?: string | null
  created_at: number
}

export interface DocumentCitation {
  citation_id: string
  doc_id: string
  filename: string
  page?: number | string | null
  chunk_index: number
  citation_label: string
  company?: string | null
  ticker?: string | null
  doc_type?: string | null
  fiscal_period?: string | null
  text: string
  previous_text?: string
  next_text?: string
  tables?: Array<{
    table_id?: string
    page?: number | string | null
    caption?: string
    headers: string[]
    rows: string[][]
    bbox?: number[] | null
    confidence?: number
  }>
}

/** Snapshot of an attachment at send time (shown on the user bubble). */
export interface AttachedDocSnapshot {
  doc_id: string
  filename: string
  status: DocumentInfo['status']
  page_count?: number
}

export interface JobSummary {
  thread_id: string
  query: string
  status: string
  mode: string
  intent: string | null
  created_at: string
}

export type WorkspaceObjectType =
  | 'document_analysis'
  | 'dcf_run'
  | 'memo'
  | 'deck'
  | 'comparison'
  | string

export interface WorkspaceObject {
  object_id: string
  version_id?: string
  version_number?: number
  object_type: WorkspaceObjectType
  schema_ref?: string | null
  schema_version?: string
  title: string
  status: string
  session_id?: string | null
  thread_id?: string | null
  case_id?: string | null
  task_id?: string | null
  run_id?: string | null
  source_message_id?: string | null
  created_by?: string | null
  updated_by?: string | null
  source_object_ids: string[]
  source_version_ids?: string[]
  entity_refs?: Record<string, any>[]
  source_refs?: Record<string, any>[]
  kg_node_ids: string[]
  artifact_paths: string[]
  search_text?: string | null
  tags?: string[]
  confidence?: number | null
  quality?: Record<string, any>
  visibility?: string
  summary?: string | null
  payload: Record<string, any>
  created_at: string
  updated_at: string
}

export interface CollaborationActor {
  actor_id: string
  kind: 'human' | 'agent' | 'system'
  display_name: string
  handle: string
  avatar_url?: string | null
  capabilities: string[]
  status: 'available' | 'working' | 'waiting' | 'blocked' | 'offline'
  role?: string
}

export interface CollaborationChannel {
  channel_id: string
  workspace_id: string
  kind: 'channel' | 'direct' | 'case' | 'object'
  name: string
  topic: string
  created_by: string
  object_id?: string | null
  case_id?: string | null
  created_at: string
  updated_at: string
}

export interface CollaborationMention {
  mention_id: string
  kind: 'human' | 'agent' | 'channel' | 'object' | 'case' | 'citation'
  target_id: string
  label: string
  start: number
  end: number
  requested_action?: string | null
  context_refs: string[]
}

export interface CollaborationMessage {
  message_id: string
  channel_id: string
  workspace_id: string
  actor_id: string
  body: string
  mentions: CollaborationMention[]
  object_version_ids: string[]
  parent_message_id?: string | null
  created_at: string
  edited_at?: string | null
  assignment?: CollaborationAssignment
}

export interface CollaborationNotification {
  notification_id: string
  workspace_id: string
  actor_id: string
  event_type: string
  title: string
  body: string
  target_type: string
  target_id: string
  read: boolean
  created_at: string
}

export interface CollaborationAssignment {
  assignment_id: string
  workspace_id: string
  title: string
  description: string
  assigned_by: string
  assigned_to: string
  case_id?: string | null
  object_version_ids: string[]
  output_object_version_ids: string[]
  status: 'open' | 'working' | 'blocked' | 'completed' | 'cancelled'
  due_at?: string | null
  channel_id?: string | null
  source_message_id?: string | null
  thread_id?: string | null
  error?: string | null
  created_at: string
  updated_at: string
}
