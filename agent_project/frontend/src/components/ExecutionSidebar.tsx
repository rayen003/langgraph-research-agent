import type { RunStatus, StepState, DcfReviewState, DeckReviewState } from '../types'
import type { ActivityEntry } from '../lib/activity'
import { StepCard } from './StepCard'
import { ActivityTrace, DcfHitlSection } from './ActivityTrace'
import { DeckOutlineReview } from './DeckOutlineReview'
import { PanelHideButton } from './PanelHideButton'

interface Props {
  status: RunStatus
  steps: StepState[]
  completedSteps: number
  error: string | null
  activity?: ActivityEntry[]
  dcfReview?: DcfReviewState
  deckReview?: DeckReviewState
  onApprove: () => void
  onReject: () => void
  threadId?: string | null
  onHide?: () => void
}

export function ExecutionSidebar({
  status,
  steps,
  completedSteps,
  error,
  activity,
  dcfReview,
  deckReview,
  onApprove,
  onReject,
  threadId,
  onHide,
}: Props) {
  const totalSteps = steps.length
  const hasActivity = activity && activity.length > 0
  const isChatMode = totalSteps === 0 && hasActivity

  // Phase 2: surface DCF model_validity from the live workflow activity so
  // the user sees a red banner the moment convergence_gate emits invalid.
  const workflowEntry = activity?.find(
    a => a.kind === 'workflow' && a.meta && typeof a.meta === 'object',
  )
  const wfMeta = (workflowEntry?.meta ?? {}) as Record<string, unknown>
  const liveValidity = typeof wfMeta.model_validity === 'string'
    ? (wfMeta.model_validity as string) : null
  const liveInvalidationReason = typeof wfMeta.invalidation_reason === 'string'
    ? wfMeta.invalidation_reason as string : ''
  const isDegraded = liveValidity === 'invalid'
  const progress = totalSteps > 0 ? (completedSteps / totalSteps) * 100 : 0
  const isAwaitingPlan = status === 'awaiting_approval'
  const isAwaitingAssumptions = status === 'awaiting_assumptions'
  const isAwaitingOutlineReview = status === 'awaiting_outline_review'
  const isAwaiting = isAwaitingPlan || isAwaitingAssumptions || isAwaitingOutlineReview
  const isSynthesizing = status === 'synthesizing'
  const isComplete = status === 'complete'
  const isError = status === 'error'
  const isRejected = status === 'rejected'

  return (
    <div className="w-full flex flex-col bg-bg border-l border-border overflow-hidden h-full">

      {/* ── Header ───────────────────────────────────────── */}
      <div className="px-5 pt-5 pb-4 border-b border-border flex-shrink-0 space-y-3">
        <div className="flex items-center justify-between">
          <span className="text-[11px] font-medium text-ink-dim tracking-widest uppercase">
            {isChatMode ? 'Workflow' : 'Execution'}
          </span>
          <div className="flex items-center gap-1">
            {totalSteps > 0 && (
              <span className="text-[11px] text-zinc-700 tabular-nums">
                {completedSteps} / {totalSteps} steps
              </span>
            )}
            {onHide && <PanelHideButton onHide={onHide} edge="right" className="text-ink-dim hover:text-ink-muted" />}
          </div>
        </div>

        {/* Progress bar */}
        <div className="h-[2px] bg-surface-3 rounded-full overflow-hidden">
          <div
            className={`h-full rounded-full transition-all duration-700 ease-out ${
              isComplete && isDegraded
                ? 'bg-amber-500'
                : isComplete
                ? 'bg-emerald-600'
                : isError
                ? 'bg-red-600'
                : isSynthesizing
                ? 'bg-violet-500'
                : 'bg-indigo-500'
            }`}
            style={{
              width: isSynthesizing ? '100%' : isComplete ? '100%' : `${progress}%`,
            }}
          />
        </div>

        {/* Current step label */}
        {!isChatMode && (
          <CurrentStepLabel status={status} steps={steps} completedSteps={completedSteps} totalSteps={totalSteps} />
        )}
      </div>

      {/* ── Step list ────────────────────────────────────── */}
      <div className="flex-1 overflow-y-auto px-5 py-4 space-y-3">

        {/* Degraded banner (Phase 2) */}
        {isDegraded && (
          <div className="rounded-md border border-red-500/40 bg-red-500/10 px-3 py-2 text-[12px] text-red-200">
            <div className="font-semibold flex items-center gap-1.5">
              <span>⚠</span>
              <span>Model invalid — degraded run</span>
            </div>
            {liveInvalidationReason && (
              <div className="mt-1 text-[11px] text-red-300/90 leading-snug">
                {liveInvalidationReason}
              </div>
            )}
          </div>
        )}

        {/* Chat mode: render activity trace directly */}
        {isChatMode && (
          <ActivityTrace activities={activity!} defaultOpen label="Activity" />
        )}

        {status === 'planning' && (
          <div className="space-y-3 animate-fade-up">
            <PlanSkeleton />
          </div>
        )}

        {steps.map((step, idx) => (
          <StepCard
            key={step.id}
            step={step}
            index={idx}
            isLast={idx === steps.length - 1}
          />
        ))}

        {isSynthesizing && (
          <div className="flex items-center gap-2.5 pt-1 animate-fade-up">
            <div className="w-1.5 h-1.5 rounded-full bg-violet-500 animate-pulse flex-shrink-0" />
            <span className="text-xs text-ink-dim">Synthesizing report…</span>
          </div>
        )}

        {isComplete && (
          <div className="flex items-center gap-2 pt-1 animate-fade-up">
            <div className="w-1.5 h-1.5 rounded-full bg-emerald-500 flex-shrink-0" />
            <span className="text-xs text-ink-dim">Report ready</span>
          </div>
        )}

        {isError && (
          <div className="animate-fade-up space-y-1.5 pt-1">
            <div className="flex items-center gap-2">
              <div className="w-1.5 h-1.5 rounded-full bg-red-500 flex-shrink-0" />
              <span className="text-xs text-red-500 font-medium">Execution failed</span>
            </div>
            {error && (
              <pre className="text-[10px] text-red-700 leading-relaxed whitespace-pre-wrap break-all bg-red-950/20 border border-red-900/30 rounded-md px-2.5 py-2">
                {error}
              </pre>
            )}
          </div>
        )}

        {isRejected && (
          <p className="text-xs text-ink-dim pt-1 animate-fade-up">
            Plan rejected. Start a new research query to try again.
          </p>
        )}
      </div>

      {/* ── HITL footer ──────────────────────────────────── */}
      {isAwaiting && (
        <div className="flex-shrink-0 px-5 py-4 border-t border-border space-y-3 animate-fade-up">
          {isAwaitingAssumptions && dcfReview && threadId && (
            <DcfHitlSection review={dcfReview} threadId={threadId} onApprove={onApprove} onReject={onReject} />
          )}
          {isAwaitingOutlineReview && deckReview && threadId && (
            <DeckOutlineReview review={deckReview} threadId={threadId} onApprove={onApprove} onReject={onReject} />
          )}
          {!isAwaitingAssumptions && !isAwaitingOutlineReview && (
            <>
              <p className="text-[11px] text-ink-dim leading-relaxed">
                {isAwaitingPlan
                  ? 'Review the plan above. Once approved, execution begins immediately.'
                  : 'Review workflow assumptions. Approve to continue deterministic valuation.'}
              </p>
              <div className="flex gap-2">
                <button onClick={onApprove} className="flex-1 py-2 rounded-lg text-xs font-medium bg-indigo-600 hover:bg-indigo-500 active:bg-indigo-700 text-white transition-colors duration-150">
                  {isAwaitingPlan ? 'Approve & Run' : 'Approve Assumptions'}
                </button>
                <button onClick={onReject} className="flex-1 py-2 rounded-lg text-xs font-medium bg-surface hover:bg-surface-2 active:bg-surface text-ink-dim border border-border-hover transition-colors duration-150">
                  {isAwaitingPlan ? 'Reject' : 'Reject Assumptions'}
                </button>
              </div>
            </>
          )}
        </div>
      )}
    </div>
  )
}

function CurrentStepLabel({
  status,
  steps,
  completedSteps,
  totalSteps,
}: {
  status: RunStatus
  steps: StepState[]
  completedSteps: number
  totalSteps: number
}) {
  if (status === 'planning') {
    return <p className="text-[11px] text-zinc-700">Building execution plan…</p>
  }
  if (status === 'workflow_running') {
    return <p className="text-[11px] text-indigo-400">Running deterministic workflow…</p>
  }
  if (status === 'awaiting_assumptions') {
    return <p className="text-[11px] text-amber-500">Awaiting assumption validation</p>
  }
  if (status === 'awaiting_outline_review') {
    return <p className="text-[11px] text-amber-500">Awaiting deck outline review</p>
  }
  if (status === 'awaiting_approval') {
    return <p className="text-[11px] text-zinc-700">Awaiting your approval</p>
  }
  if (status === 'synthesizing') {
    return <p className="text-[11px] text-violet-500">Writing final report…</p>
  }
  if (status === 'complete') {
    return <p className="text-[11px] text-emerald-600">All steps complete</p>
  }
  if (status === 'executing') {
    const running = steps.find(s => s.status === 'running')
    if (running) {
      return (
        <p className="text-[11px] text-ink-dim truncate">
          <span className="text-indigo-400">Step {completedSteps + 1}</span>
          {' '}—{' '}
          <span>{running.description.length > 50 ? running.description.slice(0, 50) + '…' : running.description}</span>
        </p>
      )
    }
  }
  return null
}

function PlanSkeleton() {
  return (
    <div className="space-y-5">
      {[70, 85, 60, 75].map((w, i) => (
        <div key={i} className="flex gap-3 items-start">
          <div className="w-3.5 h-3.5 rounded-full bg-surface-2 flex-shrink-0 mt-0.5" />
          <div className="flex-1 space-y-1.5">
            <div className="h-2.5 rounded bg-surface-3" style={{ width: `${w}%` }} />
            <div className="h-2 rounded bg-surface" style={{ width: `${w - 20}%` }} />
          </div>
        </div>
      ))}
    </div>
  )
}
