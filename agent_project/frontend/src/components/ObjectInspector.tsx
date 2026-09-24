import { useEffect, useState } from 'react'
import { Activity, Check, GitBranch, History, Info, MessageSquare, Quote, Send, ShieldCheck, X } from 'lucide-react'
import type { WorkspaceObject } from '../types'

type Tab = 'overview' | 'lineage' | 'versions' | 'evidence' | 'activity' | 'comments' | 'review'
type ObjectAction = { action_id: number; action_type: string; actor_id: string; previous_version_id?: string; resulting_version_id: string; created_at: string }
type ObjectComment = { comment_id: string; actor_id: string; body: string; object_version_id: string; block_id?: string; status: string; created_at: string }
type Suggestion = { suggestion_id: string; actor_id: string; rationale: string; patch: Record<string, unknown>; status: string; created_at: string }
type Approval = { approval_id: string; requested_by: string; assigned_to: string; note: string; status: string; created_at: string }

const TABS: Array<{ id: Tab; label: string; icon: typeof Info }> = [
  { id: 'overview', label: 'Overview', icon: Info },
  { id: 'lineage', label: 'Lineage', icon: GitBranch },
  { id: 'versions', label: 'Versions', icon: History },
  { id: 'evidence', label: 'Evidence', icon: Quote },
  { id: 'activity', label: 'Activity', icon: Activity },
  { id: 'comments', label: 'Comments', icon: MessageSquare },
  { id: 'review', label: 'Review', icon: ShieldCheck },
]

export function ObjectInspector({ object, workspaceId, onClose }: {
  object: WorkspaceObject
  workspaceId: string
  onClose: () => void
}) {
  const [tab, setTab] = useState<Tab>('overview')
  const [versions, setVersions] = useState<WorkspaceObject[]>([])
  const [actions, setActions] = useState<ObjectAction[]>([])
  const [comments, setComments] = useState<ObjectComment[]>([])
  const [suggestions, setSuggestions] = useState<Suggestion[]>([])
  const [approvals, setApprovals] = useState<Approval[]>([])
  const [comment, setComment] = useState('')
  const [suggestion, setSuggestion] = useState('')

  const refresh = async () => {
    const id = encodeURIComponent(object.object_id)
    const [versionRes, actionRes, commentRes, suggestionRes, approvalRes] = await Promise.all([
      fetch(`/workspace/objects/${id}/versions`),
      fetch(`/workspace/objects/${id}/actions`),
      fetch(`/workspace/objects/${id}/comments`),
      fetch(`/workspace/objects/${id}/suggestions`),
      fetch(`/workspace/objects/${id}/approvals`),
    ])
    if (versionRes.ok) setVersions(await versionRes.json())
    if (actionRes.ok) setActions(await actionRes.json())
    if (commentRes.ok) setComments(await commentRes.json())
    if (suggestionRes.ok) setSuggestions(await suggestionRes.json())
    if (approvalRes.ok) setApprovals(await approvalRes.json())
  }

  useEffect(() => { void refresh() }, [object.object_id])

  const addComment = async () => {
    const body = comment.trim()
    if (!body || !object.version_id) return
    const response = await fetch(`/workspace/objects/${encodeURIComponent(object.object_id)}/comments`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        workspace_id: workspaceId,
        object_version_id: object.version_id,
        actor_id: 'human:local',
        body,
      }),
    })
    if (response.ok) { setComment(''); await refresh() }
  }

  const addSuggestion = async () => {
    const replacement = suggestion.trim()
    if (!replacement || !object.version_id) return
    const response = await fetch(`/workspace/objects/${encodeURIComponent(object.object_id)}/suggestions`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ workspace_id: workspaceId, base_version_id: object.version_id, actor_id: 'human:local', patch: { replacement }, rationale: 'Proposed from object review' }),
    })
    if (response.ok) { setSuggestion(''); await refresh() }
  }

  const decideSuggestion = async (suggestionId: string, decision: 'accepted' | 'rejected') => {
    const response = await fetch(`/workspace/suggestions/${encodeURIComponent(suggestionId)}/decision`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ decision, actor_id: 'human:local' }) })
    if (response.ok) await refresh()
  }

  const requestApproval = async () => {
    if (!object.version_id) return
    const response = await fetch(`/workspace/objects/${encodeURIComponent(object.object_id)}/approvals`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ workspace_id: workspaceId, object_version_id: object.version_id, assigned_to: 'human:local', actor_id: 'human:local', note: 'Review current version' }) })
    if (response.ok) await refresh()
  }

  const decideApproval = async (approvalId: string, decision: 'approved' | 'rejected') => {
    const response = await fetch(`/workspace/approvals/${encodeURIComponent(approvalId)}/decision`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ decision, actor_id: 'human:local' }) })
    if (response.ok) await refresh()
  }

  return (
    <aside className="flex h-full w-full flex-col bg-bg">
      <header className="border-b border-border px-4 py-4">
        <div className="flex items-start gap-3">
          <div className="min-w-0 flex-1">
            <div className="text-[10px] uppercase tracking-[0.16em] text-ink-dim">{object.object_type.replace(/_/g, ' ')}</div>
            <h2 className="mt-1 truncate text-sm font-semibold text-ink">{object.title}</h2>
            <div className="mt-1 truncate font-mono text-[9px] text-ink-dim">{object.version_id || object.object_id}</div>
          </div>
          <button type="button" onClick={onClose} title="Close inspector" aria-label="Close inspector" className="flex h-7 w-7 items-center justify-center rounded text-ink-dim hover:bg-surface hover:text-ink"><X size={15} /></button>
        </div>
      </header>

      <nav className="grid grid-cols-4 border-b border-border-subtle px-2 py-2">
        {TABS.map(({ id, label, icon: Icon }) => (
          <button key={id} type="button" onClick={() => setTab(id)} className={`flex items-center justify-center gap-1 px-1 py-1.5 text-[10px] ${tab === id ? 'text-indigo-400' : 'text-ink-dim hover:text-ink-muted'}`}>
            <Icon size={11} />{label}
          </button>
        ))}
      </nav>

      <div className="flex-1 overflow-y-auto p-4 text-xs">
        {tab === 'overview' && <Overview object={object} />}
        {tab === 'lineage' && <Lineage object={object} />}
        {tab === 'versions' && <Timeline items={versions.map(v => ({ id: v.version_id || v.object_id, title: `v${v.version_number ?? '?'}`, meta: v.updated_by || v.created_by || 'system', time: v.updated_at }))} />}
        {tab === 'evidence' && <Evidence object={object} />}
        {tab === 'activity' && <Timeline items={actions.map(a => ({ id: String(a.action_id), title: a.action_type, meta: a.actor_id, time: a.created_at }))} />}
        {tab === 'comments' && (
          <div className="space-y-4">
            <div className="space-y-2">
              {comments.length === 0 && <Empty text="No comments on this object version." />}
              {comments.map(item => <div key={item.comment_id} className="border-l-2 border-indigo-500/40 pl-3"><div className="text-[10px] text-ink-dim">{item.actor_id} · {new Date(item.created_at).toLocaleString()}</div><p className="mt-1 leading-relaxed text-ink-muted">{item.body}</p><div className="mt-1 font-mono text-[9px] text-ink-dim">{item.object_version_id}</div></div>)}
            </div>
            <div className="flex items-end gap-2 border-t border-border-subtle pt-3">
              <textarea value={comment} onChange={e => setComment(e.target.value)} rows={3} placeholder="Comment on current version" className="min-w-0 flex-1 resize-none rounded border border-border bg-surface px-2.5 py-2 text-[11px] text-ink outline-none focus:border-indigo-500" />
              <button type="button" disabled={!object.version_id || !comment.trim()} onClick={() => void addComment()} title="Post comment" aria-label="Post comment" className="flex h-8 w-8 items-center justify-center rounded bg-indigo-600 text-white hover:bg-indigo-500 disabled:cursor-not-allowed disabled:opacity-40"><Send size={13} /></button>
            </div>
          </div>
        )}
        {tab === 'review' && <div className="space-y-6">
          <section><div className="mb-2 flex items-center"><h3 className="text-[10px] uppercase tracking-[0.12em] text-ink-dim">Suggestions</h3></div>
            <div className="space-y-2">{suggestions.length === 0 && <Empty text="No proposed changes." />}{suggestions.map(item => <div key={item.suggestion_id} className="rounded border border-border p-2"><div className="flex items-center gap-2 text-[10px] text-ink-dim"><span>{item.actor_id}</span><Status value={item.status} /></div><p className="mt-2 text-ink-muted">{String(item.patch.replacement ?? item.rationale)}</p>{item.status === 'pending' && <div className="mt-2 flex gap-1"><DecisionButton icon={Check} label="Accept" onClick={() => void decideSuggestion(item.suggestion_id, 'accepted')} /><DecisionButton icon={X} label="Reject" onClick={() => void decideSuggestion(item.suggestion_id, 'rejected')} /></div>}</div>)}</div>
            <div className="mt-3 flex gap-2"><input value={suggestion} onChange={event => setSuggestion(event.target.value)} placeholder="Suggest replacement or change" className="min-w-0 flex-1 rounded border border-border bg-surface px-2 py-2 text-[11px] text-ink outline-none" /><button type="button" disabled={!suggestion.trim() || !object.version_id} onClick={() => void addSuggestion()} className="rounded bg-indigo-600 px-2 text-[10px] text-white disabled:opacity-40">Propose</button></div>
          </section>
          <section className="border-t border-border-subtle pt-4"><div className="mb-2 flex items-center"><h3 className="text-[10px] uppercase tracking-[0.12em] text-ink-dim">Approvals</h3><button type="button" disabled={!object.version_id} onClick={() => void requestApproval()} className="ml-auto rounded border border-border px-2 py-1 text-[10px] text-ink-muted disabled:opacity-40">Request review</button></div>
            <div className="space-y-2">{approvals.length === 0 && <Empty text="No review requests." />}{approvals.map(item => <div key={item.approval_id} className="rounded border border-border p-2"><div className="flex items-center gap-2"><span className="text-[10px] text-ink-dim">{item.requested_by} → {item.assigned_to}</span><Status value={item.status} /></div><p className="mt-1 text-[11px] text-ink-muted">{item.note}</p>{item.status === 'pending' && <div className="mt-2 flex gap-1"><DecisionButton icon={Check} label="Approve" onClick={() => void decideApproval(item.approval_id, 'approved')} /><DecisionButton icon={X} label="Reject" onClick={() => void decideApproval(item.approval_id, 'rejected')} /></div>}</div>)}</div>
          </section>
        </div>}
      </div>
    </aside>
  )
}

function Overview({ object }: { object: WorkspaceObject }) {
  const rows = [['Status', object.status], ['Created by', object.created_by], ['Updated by', object.updated_by], ['Visibility', object.visibility], ['Thread', object.thread_id], ['Case', object.case_id]]
  const plan = object.object_type === 'research_case' ? (object.payload.plan ?? object.payload) as { tasks?: Array<{ task_id: string; capability_id: string; objective: string; dependency_task_ids?: string[] }> } : null
  const results = object.payload.results as Record<string, { status?: string }> | undefined
  return <div className="space-y-5"><p className="leading-relaxed text-ink-muted">{object.summary || 'No summary.'}</p><dl className="space-y-2">{rows.filter(([,v]) => v).map(([k,v]) => <div key={k} className="flex gap-3 border-b border-border-subtle pb-2"><dt className="w-20 shrink-0 text-ink-dim">{k}</dt><dd className="min-w-0 break-all text-ink-muted">{v}</dd></div>)}</dl>{plan?.tasks?.length ? <section><h3 className="mb-2 text-[10px] uppercase tracking-[0.12em] text-ink-dim">Task graph</h3><div className="space-y-2">{plan.tasks.map(task => <div key={task.task_id} className="rounded border border-border p-2"><div className="flex text-[10px] text-ink-dim"><span>{task.capability_id}</span><span className="ml-auto uppercase">{results?.[task.task_id]?.status ?? (object.status === 'complete' ? 'complete' : 'pending')}</span></div><p className="mt-1 text-[11px] leading-relaxed text-ink-muted">{task.objective}</p>{task.dependency_task_ids?.length ? <div className="mt-1 text-[9px] text-ink-disabled">After: {task.dependency_task_ids.join(', ')}</div> : null}</div>)}</div></section> : null}</div>
}

function Lineage({ object }: { object: WorkspaceObject }) {
  const ids = object.source_object_ids ?? []; const versions = object.source_version_ids ?? []
  return <div className="space-y-3">{ids.length === 0 && versions.length === 0 && <Empty text="No upstream lineage recorded." />}{ids.map((id,i) => <div key={id} className="border-l border-indigo-500/50 pl-3"><div className="break-all text-ink-muted">{id}</div>{versions[i] && <div className="mt-1 break-all font-mono text-[9px] text-ink-dim">{versions[i]}</div>}</div>)}</div>
}

function Evidence({ object }: { object: WorkspaceObject }) {
  const refs = object.source_refs ?? []
  return <div className="space-y-3">{refs.length === 0 && <Empty text="No evidence references recorded." />}{refs.map((ref, i) => <div key={String(ref.source_id || ref.citation_id || i)} className="border-b border-border-subtle pb-3"><div className="text-ink-muted">{String(ref.title || ref.filename || ref.source_id || ref.citation_id || 'Source')}</div><pre className="mt-1 whitespace-pre-wrap break-all font-mono text-[9px] text-ink-dim">{String(ref.citation_id || ref.source_id || '')}</pre></div>)}</div>
}

function Timeline({ items }: { items: Array<{ id: string; title: string; meta: string; time: string }> }) {
  return <div className="space-y-3">{items.length === 0 && <Empty text="No history recorded." />}{items.map(item => <div key={item.id} className="grid grid-cols-[8px_1fr] gap-3"><span className="mt-1 h-2 w-2 rounded-full bg-indigo-500" /><div><div className="text-ink-muted">{item.title}</div><div className="mt-0.5 text-[10px] text-ink-dim">{item.meta} · {new Date(item.time).toLocaleString()}</div></div></div>)}</div>
}

function Empty({ text }: { text: string }) { return <p className="text-[11px] text-ink-dim">{text}</p> }
function Status({ value }: { value: string }) { return <span className="ml-auto rounded bg-surface px-1.5 py-0.5 text-[9px] uppercase text-ink-dim">{value}</span> }
function DecisionButton({ icon: Icon, label, onClick }: { icon: typeof Check; label: string; onClick: () => void }) { return <button type="button" onClick={onClick} className="flex items-center gap-1 rounded border border-border px-2 py-1 text-[10px] text-ink-muted hover:bg-surface"><Icon size={10} />{label}</button> }
