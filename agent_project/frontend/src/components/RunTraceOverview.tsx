import { Bot, Boxes, Clock3, Database, GitBranch, Route, Wrench } from 'lucide-react'
import type { ExecutionTraceStep, ExecutionTraceTask } from '../types'
import type { TraceCategory, TraceSpan } from '../lib/tracing'

export function RunTraceOverview({ trace, spans = [] }: { trace: ExecutionTraceStep[]; spans?: TraceSpan[] }) {
  if (!trace.length && !spans.length) return null
  const route = [...trace].reverse().find(item => item.stage === 'semantic_router')
  const playbook = [...trace].reverse().find(item => item.stage === 'load_playbook')
  const memory = [...trace].reverse().find(item => item.stage === 'build_memory_context')
  const casePlan = [...trace].reverse().find(item => item.stage === 'plan_case')
  const taskResults = trace.filter(item => item.stage === 'execute_case_task')
  const statusByTask = new Map(taskResults.map(item => [item.task_id, item.status]))

  return <div className="space-y-4 border-b border-border-subtle pb-4">
    <div className="grid grid-cols-2 gap-2">
      <Fact icon={Route} label="Route" value={route?.route_level || 'pending'} detail={route?.confidence != null ? `${Math.round(route.confidence * 100)}% confidence` : undefined} />
      <Fact icon={GitBranch} label="Playbook" value={playbook?.playbook_id || 'base'} detail={route?.latency_class || undefined} />
      <Fact icon={Database} label="Context" value={`${memory?.object_ids?.length ?? 0} objects`} detail={`${memory?.citation_ids?.length ?? 0} sources`} />
      <Fact icon={Wrench} label="Tools" value={`${playbook?.allowed_tools?.length ?? 0} allowed`} detail={route?.selected_workflow || undefined} />
    </div>
    {spans.length > 0 && <TraceWaterfall spans={spans} />}
    {casePlan?.tasks?.length ? <CaseDag tasks={casePlan.tasks} statusByTask={statusByTask} /> : null}
    <details className="group"><summary className="cursor-pointer text-[10px] uppercase tracking-[0.12em] text-ink-dim">Full trace · {trace.length} events</summary><div className="mt-3 space-y-2">{trace.map((item, index) => <div key={`${item.stage}:${index}`} className="grid grid-cols-[8px_1fr] gap-2"><span className={`mt-1.5 h-1.5 w-1.5 rounded-full ${item.status === 'failed' ? 'bg-red-500' : item.status === 'completed' ? 'bg-emerald-500' : 'bg-indigo-500'}`} /><div><div className="text-[11px] text-ink-muted">{label(item.stage)}</div>{item.reason && <div className="text-[9px] leading-relaxed text-ink-dim">{item.reason}</div>}</div></div>)}</div></details>
  </div>
}

const TRACE_COLORS: Record<TraceCategory, string> = {
  run: 'bg-zinc-500',
  transport: 'bg-cyan-500',
  node: 'bg-indigo-500',
  model: 'bg-amber-500',
  tool: 'bg-emerald-500',
  memory: 'bg-fuchsia-500',
}

function TraceWaterfall({ spans }: { spans: TraceSpan[] }) {
  const visible = spans
    .filter(span => typeof span.started_at === 'number')
    .sort((a, b) => (a.started_at ?? 0) - (b.started_at ?? 0))
  if (!visible.length) return null
  const start = Math.min(...visible.map(span => span.started_at!))
  const end = Math.max(
    start + 0.001,
    ...visible.map(span => span.ended_at ?? Date.now() / 1000),
  )
  const range = end - start

  return <section>
    <div className="mb-2 flex items-center gap-1 text-[10px] uppercase tracking-[0.12em] text-ink-dim">
      <Clock3 size={11} />Trace waterfall
    </div>
    <div className="space-y-2">
      {visible.map(span => {
        const offset = Math.max(0, ((span.started_at! - start) / range) * 100)
        const spanEnd = span.ended_at ?? end
        const width = Math.max(1.5, ((spanEnd - span.started_at!) / range) * 100)
        const duration = span.duration_ms ?? ((spanEnd - span.started_at!) * 1000)
        const model = typeof span.metadata?.model === 'string' ? span.metadata.model : ''
        return <div key={span.span_id}>
          <div className="mb-1 flex min-w-0 items-center gap-2 text-[9px]">
            <span className="truncate text-ink-muted">{traceLabel(span.name)}</span>
            {model && <span className="truncate text-ink-disabled">{model}</span>}
            <span className="ml-auto shrink-0 tabular-nums text-ink-dim">{formatDuration(duration)}</span>
          </div>
          <div className="relative h-1.5 overflow-hidden rounded-sm bg-surface-3">
            <div
              className={`absolute h-full rounded-sm ${span.status === 'error' ? 'bg-red-500' : TRACE_COLORS[span.category]}`}
              style={{ left: `${Math.min(offset, 98.5)}%`, width: `${Math.min(width, 100 - offset)}%` }}
            />
          </div>
        </div>
      })}
    </div>
  </section>
}

function formatDuration(ms: number) {
  if (ms < 1000) return `${Math.round(ms)}ms`
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)}s`
  return `${Math.floor(ms / 60_000)}m ${Math.round((ms % 60_000) / 1000)}s`
}

function traceLabel(value: string) {
  return value.replace(/[_:]+/g, ' ').replace(/^./, char => char.toUpperCase())
}

function CaseDag({ tasks, statusByTask }: { tasks: ExecutionTraceTask[]; statusByTask: Map<string | undefined, string | null | undefined> }) {
  return <section><div className="mb-2 flex items-center gap-1 text-[10px] uppercase tracking-[0.12em] text-ink-dim"><Boxes size={11} />Case tasks</div><div className="space-y-2">{tasks.map(task => <div key={task.task_id} className="rounded border border-border bg-bg-raised p-2"><div className="flex items-center gap-2"><Bot size={11} className="text-indigo-400" /><span className="text-[10px] text-ink-muted">{task.capability_id}</span><span className="ml-auto text-[9px] uppercase text-ink-dim">{statusByTask.get(task.task_id) || 'pending'}</span></div><p className="mt-1 text-[11px] leading-relaxed text-ink-muted">{task.objective}</p>{task.dependency_task_ids.length > 0 && <div className="mt-1 text-[9px] text-ink-dim">After: {task.dependency_task_ids.join(', ')}</div>}</div>)}</div></section>
}

function Fact({ icon: Icon, label: text, value, detail }: { icon: typeof Route; label: string; value: string; detail?: string }) { return <div className="rounded border border-border-subtle p-2"><div className="flex items-center gap-1 text-[9px] uppercase text-ink-dim"><Icon size={10} />{text}</div><div className="mt-1 truncate text-[11px] text-ink-muted">{value}</div>{detail && <div className="truncate text-[9px] text-ink-disabled">{detail}</div>}</div> }
function label(value: string) { return value.replace(/_/g, ' ').replace(/^./, char => char.toUpperCase()) }
