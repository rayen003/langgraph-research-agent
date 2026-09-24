import { useState } from 'react'
import { Bell, Bot, FileText, Hash, Inbox, Layers, ListTodo, Plus, UserRound } from 'lucide-react'
import type { CollaborationActor, CollaborationAssignment, CollaborationChannel, CollaborationNotification, WorkspaceObject } from '../types'

type View = 'channels' | 'tasks' | 'objects' | 'agents' | 'inbox'

export function WorkspaceRail({ channels, actors, objects, notifications, assignments, activeChannelId, activeTaskId, onSelectChannel, onOpenTask, onOpenObject, onCreateChannel, onStartDirect, onOpenNotification, onUpdateAssignment }: {
  channels: CollaborationChannel[]
  actors: CollaborationActor[]
  objects: WorkspaceObject[]
  notifications: CollaborationNotification[]
  assignments: CollaborationAssignment[]
  activeChannelId: string | null
  activeTaskId: string | null
  onSelectChannel: (channel: CollaborationChannel) => void
  onOpenTask: (task: CollaborationAssignment) => void
  onOpenObject: (object: WorkspaceObject) => void
  onCreateChannel: (name: string) => void
  onStartDirect: (actor: CollaborationActor) => void
  onOpenNotification: (notification: CollaborationNotification) => void
  onUpdateAssignment: (assignmentId: string, status: CollaborationAssignment['status']) => void
}) {
  const [view, setView] = useState<View>('channels')
  const unread = notifications.filter(item => !item.read).length

  const create = () => {
    const name = window.prompt('Channel name')?.trim().replace(/^#/, '')
    if (name) onCreateChannel(name)
  }

  const cases = objects.filter(object => object.object_type === 'research_case')
  const artifacts = objects.filter(object => object.object_type !== 'research_case' && object.object_type !== 'task_result')
  return <aside className="hidden w-[248px] flex-shrink-0 flex-col border-r border-border-subtle bg-bg xl:flex">
    <header className="border-b border-border-subtle px-3 py-3">
      <div className="text-[10px] uppercase tracking-[0.16em] text-ink-dim">Workspace</div>
      <div className="mt-1 text-sm font-semibold text-ink">Finance</div>
    </header>
    <nav className="grid grid-cols-5 border-b border-border-subtle p-1.5">
      <Nav icon={Hash} label="Channels" active={view === 'channels'} onClick={() => setView('channels')} />
      <Nav icon={ListTodo} label="Tasks" active={view === 'tasks'} onClick={() => setView('tasks')} />
      <Nav icon={Layers} label="Objects" active={view === 'objects'} onClick={() => setView('objects')} />
      <Nav icon={Bot} label="Agents" active={view === 'agents'} onClick={() => setView('agents')} />
      <button type="button" title="Inbox" onClick={() => setView('inbox')} className={`relative flex h-9 items-center justify-center rounded ${view === 'inbox' ? 'bg-surface text-ink' : 'text-ink-dim hover:text-ink-muted'}`}><Inbox size={15} />{unread > 0 && <span className="absolute right-1 top-0.5 min-w-3 rounded bg-blue-500 px-1 text-[8px] leading-3 text-white">{unread}</span>}</button>
    </nav>
    <div className="flex-1 overflow-y-auto p-2">
      {view === 'channels' && <>
        <Section title="Channels" action={<button type="button" onClick={create} title="New channel" className="text-ink-dim hover:text-ink"><Plus size={13} /></button>} />
        {channels.length === 0 && <Empty text="Create channel for shared work." />}
        {channels.map(channel => <button key={channel.channel_id} type="button" onClick={() => onSelectChannel(channel)} className={`mb-0.5 flex w-full items-center gap-2 rounded px-2 py-2 text-left text-xs ${activeChannelId === channel.channel_id ? 'bg-surface text-ink' : 'text-ink-muted hover:bg-bg-raised'}`}><Hash size={13} /><span className="truncate">{channel.name}</span></button>)}
        <Section title="Direct messages" />
        {actors.filter(actor => actor.actor_id !== 'human:local').map(actor => <button key={actor.actor_id} type="button" onClick={() => onStartDirect(actor)} className="mb-0.5 flex w-full items-center gap-2 rounded px-2 py-2 text-left text-xs text-ink-muted hover:bg-bg-raised"><span className={`h-1.5 w-1.5 rounded-full ${actor.status === 'offline' ? 'bg-ink-disabled' : 'bg-emerald-500'}`} /><span className="truncate">{actor.display_name}</span>{actor.kind === 'agent' && <Bot size={11} className="ml-auto text-ink-dim" />}</button>)}
      </>}
      {view === 'tasks' && <><Section title="Tasks" />{assignments.length === 0 && <Empty text="Agent and human assignments appear here." />}{assignments.map(task => <button key={task.assignment_id} type="button" onClick={() => onOpenTask(task)} className={`mb-1 w-full rounded border-l-2 px-2 py-2 text-left hover:bg-bg-raised ${activeTaskId === task.assignment_id ? 'border-blue-500 bg-bg-raised' : task.status === 'blocked' ? 'border-red-500/60' : task.status === 'completed' ? 'border-emerald-500/50' : 'border-blue-500/40'}`}><div className="truncate text-xs text-ink-muted">{task.title}</div><div className="mt-1 text-[9px] uppercase text-ink-dim">{task.status} · {actors.find(actor => actor.actor_id === task.assigned_to)?.display_name ?? task.assigned_to}</div></button>)}{cases.length > 0 && <><Section title="Cases" />{cases.map(item => <button key={item.object_id} type="button" onClick={() => onOpenObject(item)} className="mb-1 w-full rounded border-l-2 border-indigo-500/40 px-2 py-2 text-left hover:bg-bg-raised"><div className="truncate text-xs text-ink-muted">{item.title}</div><div className="mt-1 text-[9px] uppercase text-ink-dim">{item.status} · v{item.version_number ?? 1}</div></button>)}</>}</>}
      {view === 'objects' && <>
        <Section title="Recent objects" />
        {artifacts.length === 0 && <Empty text="Workflow outputs appear here." />}
        {artifacts.map(object => <button key={object.object_id} type="button" onClick={() => onOpenObject(object)} className="mb-1 w-full rounded px-2 py-2 text-left hover:bg-bg-raised"><div className="flex items-center gap-2 text-[10px] uppercase text-ink-dim"><FileText size={12} />{object.object_type.replace(/_/g, ' ')}</div><div className="mt-1 truncate text-xs text-ink-muted">{object.title}</div><div className="mt-0.5 font-mono text-[9px] text-ink-disabled">v{object.version_number ?? 1}</div></button>)}
      </>}
      {view === 'agents' && <>
        <Section title="People and agents" />
        {actors.map(actor => <div key={actor.actor_id} className="mb-1 flex items-start gap-2 rounded px-2 py-2 hover:bg-bg-raised"><span className="mt-0.5 text-ink-dim">{actor.kind === 'agent' ? <Bot size={14} /> : <UserRound size={14} />}</span><div className="min-w-0"><div className="truncate text-xs text-ink-muted">{actor.display_name}</div><div className="text-[10px] text-ink-dim">@{actor.handle} · {actor.status}</div>{actor.capabilities.length > 0 && <div className="mt-1 line-clamp-2 text-[9px] text-ink-disabled">{actor.capabilities.join(' · ')}</div>}</div></div>)}
      </>}
      {view === 'inbox' && <>
        <Section title="Inbox" />
        {assignments.map(item => <div key={item.assignment_id} className="mb-2 rounded border border-border p-2"><div className="text-[9px] uppercase text-ink-dim">Assigned by {item.assigned_by}</div><div className="mt-1 text-xs leading-snug text-ink-muted">{item.title}</div><select value={item.status} onChange={event => onUpdateAssignment(item.assignment_id, event.target.value as CollaborationAssignment['status'])} className="mt-2 w-full rounded border border-border bg-bg-raised px-1 py-1 text-[10px] text-ink-dim"><option value="open">Open</option><option value="working">Working</option><option value="blocked">Blocked</option><option value="completed">Completed</option><option value="cancelled">Cancelled</option></select></div>)}
        {notifications.length === 0 && <Empty text="No notifications." />}
        {notifications.map(item => <button key={item.notification_id} type="button" onClick={() => onOpenNotification(item)} className={`mb-1 w-full rounded border-l-2 px-2 py-2 text-left ${item.read ? 'border-transparent text-ink-dim' : 'border-blue-500 bg-blue-500/5 text-ink-muted'}`}><div className="flex gap-2"><Bell size={12} className="mt-0.5 shrink-0" /><div><div className="text-xs">{item.title}</div><div className="mt-1 line-clamp-2 text-[10px] text-ink-dim">{item.body}</div></div></div></button>)}
      </>}
    </div>
  </aside>
}

export function MobileWorkspaceBar({ channels, objects, notifications, activeChannelId, onSelectChannel, onOpenObject, onOpenNotification }: {
  channels: CollaborationChannel[]
  objects: WorkspaceObject[]
  notifications: CollaborationNotification[]
  activeChannelId: string | null
  onSelectChannel: (channel: CollaborationChannel) => void
  onOpenObject: (object: WorkspaceObject) => void
  onOpenNotification: (notification: CollaborationNotification) => void
}) {
  const [open, setOpen] = useState(false)
  const unread = notifications.filter(item => !item.read).length
  return <div className="fixed inset-x-0 bottom-0 z-30 border-t border-border bg-bg-overlay/95 px-2 pb-[env(safe-area-inset-bottom)] pt-1 backdrop-blur xl:hidden">
    <div className="flex items-center justify-around">{[
      { label: 'Channels', icon: Hash, action: () => setOpen(value => !value) },
      { label: 'Objects', icon: Layers, action: () => objects[0] && onOpenObject(objects[0]) },
      { label: 'Inbox', icon: Inbox, action: () => notifications[0] && onOpenNotification(notifications[0]) },
    ].map(({ label, icon: Icon, action }) => <button key={label} type="button" title={label} onClick={action} className="relative flex h-10 w-20 items-center justify-center text-ink-dim"><Icon size={16} />{label === 'Inbox' && unread > 0 && <span className="absolute right-3 top-1 min-w-3 rounded bg-blue-500 px-1 text-[8px] text-white">{unread}</span>}</button>)}</div>
    {open && <div className="absolute bottom-12 left-2 right-2 max-h-[55vh] overflow-y-auto rounded-md border border-border bg-bg-overlay p-2 shadow-2xl"><div className="mb-2 text-[10px] uppercase tracking-[0.12em] text-ink-dim">Channels</div>{channels.map(channel => <button key={channel.channel_id} type="button" onClick={() => { onSelectChannel(channel); setOpen(false) }} className={`mb-1 flex w-full items-center gap-2 rounded px-3 py-2 text-left text-xs ${activeChannelId === channel.channel_id ? 'bg-surface text-ink' : 'text-ink-muted hover:bg-surface'}`}><Hash size={13} />{channel.name}</button>)}{objects.length > 0 && <><div className="mb-2 mt-3 text-[10px] uppercase tracking-[0.12em] text-ink-dim">Recent objects</div>{objects.slice(0, 6).map(object => <button key={object.object_id} type="button" onClick={() => { onOpenObject(object); setOpen(false) }} className="mb-1 block w-full truncate rounded px-3 py-2 text-left text-xs text-ink-muted hover:bg-surface">{object.title}</button>)}</>}</div>}
  </div>
}

function Nav({ icon: Icon, label, active, onClick }: { icon: typeof Hash; label: string; active: boolean; onClick: () => void }) {
  return <button type="button" title={label} onClick={onClick} className={`flex h-9 items-center justify-center rounded ${active ? 'bg-surface text-ink' : 'text-ink-dim hover:text-ink-muted'}`}><Icon size={15} /></button>
}
function Section({ title, action }: { title: string; action?: React.ReactNode }) { return <div className="mb-2 flex items-center px-2 pt-1 text-[10px] uppercase tracking-[0.12em] text-ink-dim"><span>{title}</span><span className="ml-auto">{action}</span></div> }
function Empty({ text }: { text: string }) { return <div className="px-2 py-4 text-[11px] text-ink-dim">{text}</div> }
