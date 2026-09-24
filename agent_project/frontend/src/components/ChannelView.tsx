import { useMemo, useState } from 'react'
import { AtSign, Bot, Hash, Link2, Send, UserRound, X } from 'lucide-react'
import type { CollaborationActor, CollaborationAssignment, CollaborationChannel, CollaborationMention, CollaborationMessage, WorkspaceObject } from '../types'

export function ChannelView({ channel, actors, messages, assignments, objects, onSend, onOpenTask, onClose }: {
  channel: CollaborationChannel
  actors: CollaborationActor[]
  messages: CollaborationMessage[]
  assignments: CollaborationAssignment[]
  objects: WorkspaceObject[]
  onSend: (body: string, mentions: CollaborationMention[], objectVersionIds: string[]) => void
  onOpenTask: (task: CollaborationAssignment) => void
  onClose: () => void
}) {
  const [value, setValue] = useState('')
  const [mentionOpen, setMentionOpen] = useState(false)
  const [attached, setAttached] = useState<WorkspaceObject[]>([])
  const actorById = useMemo(() => new Map(actors.map(actor => [actor.actor_id, actor])), [actors])
  const tasksByMessage = useMemo(() => {
    const grouped = new Map<string, CollaborationAssignment[]>()
    assignments.filter(task => task.channel_id === channel.channel_id && task.source_message_id).forEach(task => {
      const current = grouped.get(task.source_message_id!) ?? []
      grouped.set(task.source_message_id!, [...current, task])
    })
    return grouped
  }, [assignments, channel.channel_id])

  const insertMention = (actor: CollaborationActor) => {
    setValue(current => `${current}${current && !current.endsWith(' ') ? ' ' : ''}@${actor.handle} `)
    setMentionOpen(false)
  }
  const submit = () => {
    const body = value.trim(); if (!body) return
    const mentions: CollaborationMention[] = []
    for (const actor of actors) {
      const label = `@${actor.handle}`; let start = body.indexOf(label)
      while (start >= 0) {
        mentions.push({ mention_id: `mention:${Date.now()}:${mentions.length}`, kind: actor.kind === 'agent' ? 'agent' : 'human', target_id: actor.actor_id, label, start, end: start + label.length, requested_action: inferAction(body), context_refs: attached.map(item => item.version_id).filter(Boolean) as string[] })
        start = body.indexOf(label, start + label.length)
      }
    }
    onSend(body, mentions, attached.map(item => item.version_id).filter(Boolean) as string[])
    setValue(''); setAttached([])
  }

  return <main className="flex min-w-0 flex-1 flex-col bg-bg">
    <header className="flex h-[62px] items-center gap-3 border-b border-border-subtle px-5"><Hash size={16} className="text-ink-dim" /><div className="min-w-0"><h1 className="text-sm font-semibold text-ink">{channel.name}</h1><p className="truncate text-[10px] text-ink-dim">{channel.topic || 'Shared workspace conversation'}</p></div><button type="button" onClick={onClose} title="Close channel" className="ml-auto text-ink-dim hover:text-ink"><X size={16} /></button></header>
    <div className="flex-1 overflow-y-auto px-6 py-5">
      {messages.length === 0 && <div className="mx-auto mt-20 max-w-sm text-center"><Hash size={24} className="mx-auto text-ink-disabled" /><h2 className="mt-3 text-sm text-ink">Start #{channel.name}</h2><p className="mt-1 text-xs text-ink-dim">Mention people or agents and attach exact object versions.</p></div>}
      <div className="mx-auto max-w-3xl space-y-5">{messages.map(message => { const actor = actorById.get(message.actor_id); const tasks = tasksByMessage.get(message.message_id) ?? []; return <article key={message.message_id} className="flex gap-3"><div className="flex h-7 w-7 shrink-0 items-center justify-center rounded bg-surface text-ink-dim">{actor?.kind === 'agent' ? <Bot size={14} /> : <UserRound size={14} />}</div><div className="min-w-0 flex-1"><div className="flex items-baseline gap-2"><span className="text-xs font-semibold text-ink">{actor?.display_name ?? message.actor_id}</span><time className="text-[9px] text-ink-disabled">{new Date(message.created_at).toLocaleString()}</time></div><p className="mt-1 whitespace-pre-wrap text-sm leading-6 text-ink-muted">{message.body}</p>{message.object_version_ids.length > 0 && <div className="mt-2 flex flex-wrap gap-1">{message.object_version_ids.map(id => <span key={id} className="flex items-center gap-1 rounded border border-border px-2 py-1 font-mono text-[9px] text-ink-dim"><Link2 size={9} />{id}</span>)}</div>}{tasks.map(task => <button key={task.assignment_id} type="button" onClick={() => onOpenTask(task)} className="mt-3 flex w-full items-center gap-3 rounded border border-border bg-bg-raised px-3 py-3 text-left hover:border-border-hover"><span className={`h-2 w-2 shrink-0 rounded-full ${task.status === 'completed' ? 'bg-emerald-500' : task.status === 'blocked' ? 'bg-red-500' : 'bg-blue-500'}`} /><span className="min-w-0 flex-1"><span className="block truncate text-xs font-medium text-ink">{task.title}</span><span className="mt-1 block text-[10px] text-ink-dim">{task.status} · {actorById.get(task.assigned_to)?.display_name ?? task.assigned_to}</span></span><span className="text-[10px] text-ink-dim">Open</span></button>)}</div></article> })}</div>
    </div>
    <div className="border-t border-border-subtle px-5 pb-5 pt-3"><div className="relative mx-auto max-w-3xl rounded-md border border-border bg-bg-raised focus-within:border-border-hover">
      {attached.length > 0 && <div className="flex gap-1 overflow-x-auto border-b border-border-subtle px-3 py-2">{attached.map(item => <span key={item.object_id} className="flex shrink-0 items-center gap-1 rounded bg-surface px-2 py-1 text-[9px] text-ink-muted">{item.title}<button type="button" onClick={() => setAttached(list => list.filter(value => value.object_id !== item.object_id))}><X size={9} /></button></span>)}</div>}
      <textarea value={value} onChange={event => setValue(event.target.value)} onKeyDown={event => { if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); submit() } }} rows={3} placeholder={`Message #${channel.name}`} className="w-full resize-none bg-transparent px-3 py-3 text-sm text-ink outline-none" />
      <div className="flex items-center gap-1 px-2 pb-2"><button type="button" title="Mention" onClick={() => setMentionOpen(open => !open)} className="flex h-7 w-7 items-center justify-center rounded text-ink-dim hover:bg-surface hover:text-ink"><AtSign size={14} /></button><select title="Attach object version" value="" onChange={event => { const object = objects.find(item => item.object_id === event.target.value); if (object && !attached.some(item => item.object_id === object.object_id)) setAttached(list => [...list, object]) }} className="h-7 max-w-[170px] bg-transparent text-[10px] text-ink-dim outline-none"><option value="">Attach object</option>{objects.filter(item => item.version_id).map(item => <option key={item.object_id} value={item.object_id}>{item.title} · v{item.version_number ?? 1}</option>)}</select><button type="button" onClick={submit} disabled={!value.trim()} title="Send message" className="ml-auto flex h-7 w-7 items-center justify-center rounded bg-blue-600 text-white disabled:opacity-40"><Send size={13} /></button></div>
      {mentionOpen && <div className="absolute bottom-11 left-2 z-30 w-64 rounded-md border border-border bg-bg-overlay p-1 shadow-2xl">{actors.map(actor => <button key={actor.actor_id} type="button" onClick={() => insertMention(actor)} className="flex w-full items-center gap-2 rounded px-2 py-2 text-left hover:bg-surface"><span className="text-ink-dim">{actor.kind === 'agent' ? <Bot size={13} /> : <UserRound size={13} />}</span><div><div className="text-xs text-ink-muted">{actor.display_name}</div><div className="text-[9px] text-ink-dim">@{actor.handle}{actor.kind === 'agent' ? ` · ${actor.capabilities.join(', ')}` : ''}</div></div></button>)}</div>}
    </div></div>
  </main>
}

function inferAction(body: string): string | null {
  const value = body.toLowerCase()
  if (/\b(review|check|audit)\b/.test(value)) return 'review'
  if (/\b(build|create|draft|write|run|research|analy[sz]e|compare)\b/.test(value)) return 'execute'
  return null
}
