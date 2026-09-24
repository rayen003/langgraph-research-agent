import { useEffect, useMemo } from 'react'
import { ArrowLeft, Bot, ExternalLink, FileText, Link2, UserRound } from 'lucide-react'
import { useAgentRun } from '../hooks/useAgentRun'
import { ActivityTrace } from './ActivityTrace'
import { MemoDraftReview } from './MemoDraftReview'
import { DeckOutlineReview } from './DeckOutlineReview'
import { WorkflowSetupCard } from './MessageThread'
import { MarkdownRenderer } from './MarkdownRenderer'
import type { CollaborationActor, CollaborationAssignment, WorkspaceObject } from '../types'

export function TaskView({ task, actors, objects, onBack, onOpenChannel, onOpenObject, onUpdateStatus }: {
  task: CollaborationAssignment
  actors: CollaborationActor[]
  objects: WorkspaceObject[]
  onBack: () => void
  onOpenChannel: () => void
  onOpenObject: (object: WorkspaceObject) => void
  onUpdateStatus: (status: CollaborationAssignment['status']) => void
}) {
  const { state, watchRun, approve, reject, submitWorkflowContext } = useAgentRun()
  const activity = state.activity
  const route = state.execution_trace.find(item => item.stage === 'semantic_router')
  const assignee = actors.find(actor => actor.actor_id === task.assigned_to)

  useEffect(() => {
    if (!task.thread_id) return
    watchRun(task.thread_id, task.description || task.title)
  }, [task.thread_id, watchRun])

  const byVersion = useMemo(
    () => new Map(objects.filter(object => object.version_id).map(object => [object.version_id!, object])),
    [objects],
  )
  const outputs = task.output_object_version_ids.map(id => ({ id, object: byVersion.get(id) }))
  const inputs = task.object_version_ids.map(id => ({ id, object: byVersion.get(id) }))

  return <main className="flex min-w-0 flex-1 flex-col bg-bg">
    <header className="flex min-h-[62px] items-center gap-3 border-b border-border-subtle px-5">
      <button type="button" onClick={onBack} title="Back to channel" className="flex h-8 w-8 items-center justify-center rounded text-ink-dim hover:bg-surface hover:text-ink"><ArrowLeft size={15} /></button>
      <div className="min-w-0 flex-1"><div className="text-[10px] uppercase tracking-[0.12em] text-ink-dim">Task</div><h1 className="truncate text-sm font-semibold text-ink">{task.title}</h1></div>
      <select aria-label="Task status" value={task.status} onChange={event => onUpdateStatus(event.target.value as CollaborationAssignment['status'])} className="rounded border border-border bg-bg-raised px-2 py-1.5 text-xs text-ink-muted outline-none focus:border-border-hover"><option value="open">Open</option><option value="working">Working</option><option value="blocked">Blocked</option><option value="completed">Completed</option><option value="cancelled">Cancelled</option></select>
    </header>

    <div className="flex-1 overflow-y-auto">
      <div className="mx-auto max-w-4xl px-6 py-6">
        <section className="border-b border-border-subtle pb-6">
          <p className="whitespace-pre-wrap text-sm leading-6 text-ink-muted">{task.description || task.title}</p>
          <div className="mt-4 flex flex-wrap items-center gap-x-5 gap-y-2 text-[11px] text-ink-dim">
            <span className="flex items-center gap-1.5">{assignee?.kind === 'human' ? <UserRound size={12} /> : <Bot size={12} />}{assignee?.display_name ?? task.assigned_to}</span>
            {task.thread_id && <span className="font-mono">{task.thread_id}</span>}
            {task.channel_id && <button type="button" onClick={onOpenChannel} className="flex items-center gap-1 text-blue-400 hover:text-blue-300"><ExternalLink size={11} />Open conversation</button>}
          </div>
          {route && <div className="mt-4 flex gap-4 border-l-2 border-blue-500/50 pl-3 text-[11px] text-ink-dim"><span>Route <strong className="font-medium text-ink-muted">{String(route.route_level || 'unknown')}</strong></span>{route.playbook_id ? <span>Playbook <strong className="font-medium text-ink-muted">{String(route.playbook_id)}</strong></span> : null}</div>}
        </section>

        <section className="border-b border-border-subtle py-6">
          <h2 className="text-xs font-semibold uppercase tracking-[0.12em] text-ink-dim">Work</h2>
          <ActivityTrace activities={activity} defaultOpen label="Execution details" threadId={task.thread_id ?? undefined} dcfReview={state.dcf_review ?? undefined} />
        </section>

        <ReferenceSection title="Outputs" items={outputs} empty="No output created yet." onOpenObject={onOpenObject} />
        {state.memo_review && task.thread_id && <MemoDraftReview review={state.memo_review} threadId={task.thread_id} />}
        {state.deck_review && task.thread_id && <DeckOutlineReview review={state.deck_review} threadId={task.thread_id} />}
        {state.workflow_context_review && <WorkflowSetupCard review={state.workflow_context_review} onSubmit={submitWorkflowContext} />}
        {state.status === 'awaiting_approval' && <div className="flex gap-3 py-4"><button type="button" onClick={approve}>Approve plan</button><button type="button" onClick={reject}>Reject plan</button></div>}
        {state.report && <MarkdownRenderer content={state.report} />}
        {state.chat_messages.filter(message => message.role === 'assistant').map(message => <MarkdownRenderer key={message.id} content={message.content} streaming={message.streaming} />)}
        {state.error && <p role="alert" className="py-3 text-sm text-red-400">{state.error}</p>}
        <ReferenceSection title="Sources and inputs" items={inputs} empty="No pinned sources." onOpenObject={onOpenObject} />
        {task.error && <section className="border-t border-border-subtle py-6"><h2 className="text-xs font-semibold uppercase tracking-[0.12em] text-red-400">Blocked</h2><p className="mt-3 text-xs leading-5 text-ink-muted">{task.error}</p></section>}
      </div>
    </div>
  </main>
}

function ReferenceSection({ title, items, empty, onOpenObject }: {
  title: string
  items: { id: string; object?: WorkspaceObject }[]
  empty: string
  onOpenObject: (object: WorkspaceObject) => void
}) {
  return <section className="border-b border-border-subtle py-6 last:border-b-0"><h2 className="text-xs font-semibold uppercase tracking-[0.12em] text-ink-dim">{title}</h2>{items.length === 0 ? <p className="mt-4 text-xs text-ink-dim">{empty}</p> : <div className="mt-4 space-y-2">{items.map(item => item.object ? <button key={item.id} type="button" onClick={() => onOpenObject(item.object!)} className="flex w-full items-center gap-3 rounded border border-border px-3 py-3 text-left hover:border-border-hover hover:bg-bg-raised"><FileText size={14} className="shrink-0 text-ink-dim" /><span className="min-w-0 flex-1"><span className="block truncate text-xs text-ink-muted">{item.object.title}</span><span className="mt-0.5 block text-[10px] text-ink-dim">{item.object.object_type.replace(/_/g, ' ')} · v{item.object.version_number ?? 1}</span></span></button> : <div key={item.id} className="flex items-center gap-2 rounded border border-border px-3 py-2 font-mono text-[10px] text-ink-dim"><Link2 size={11} />{item.id}</div>)}</div>}</section>
}
