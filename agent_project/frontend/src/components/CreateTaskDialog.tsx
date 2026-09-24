import { useEffect, useRef, useState } from 'react'
import { X } from 'lucide-react'
import type { CollaborationActor, CollaborationAssignment, WorkspaceObject } from '../types'

export function CreateTaskDialog({ workspaceId, channelId, actors, objects, initialGoal = '', onClose, onCreated }: {
  workspaceId: string
  channelId?: string
  actors: CollaborationActor[]
  objects: WorkspaceObject[]
  initialGoal?: string
  onClose: () => void
  onCreated: (task: CollaborationAssignment) => void
}) {
  const dialog = useRef<HTMLDialogElement>(null)
  const [goal, setGoal] = useState(initialGoal)
  const [assignee, setAssignee] = useState('agent:research')
  const [due, setDue] = useState('')
  const [refs, setRefs] = useState<string[]>([])
  const [start, setStart] = useState(true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  useEffect(() => { dialog.current?.showModal() }, [])
  async function submit(event: React.FormEvent) {
    event.preventDefault()
    if (!goal.trim() || busy) return
    setBusy(true)
    setError('')
    try {
      const response = await fetch(`/collaboration/workspaces/${encodeURIComponent(workspaceId)}/assignments`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ title: goal.trim().slice(0, 160), description: goal.trim(), assigned_to: assignee,
          channel_id: channelId, object_version_ids: refs, due_at: due || null, start_now: start }),
      })
      if (!response.ok) throw new Error((await response.json()).detail || 'Task creation failed')
      onCreated(await response.json())
    } catch (failure) { setError(failure instanceof Error ? failure.message : 'Task creation failed') }
    finally { setBusy(false) }
  }
  const field = 'w-full rounded border border-border bg-bg-raised p-2 text-sm text-ink'
  return <dialog ref={dialog} onCancel={onClose} className="m-auto w-[calc(100%-2rem)] max-w-lg rounded-md border border-border bg-bg p-5 text-ink backdrop:bg-black/60">
    <form onSubmit={submit} className="space-y-4">
      <header className="flex items-center justify-between"><h2 className="text-base font-semibold">Create task</h2><button type="button" title="Close" onClick={onClose}><X size={18} /></button></header>
      <label className="block text-xs">Goal<textarea autoFocus required value={goal} onChange={event => setGoal(event.target.value)} rows={4} className={`${field} mt-1`} /></label>
      <label className="block text-xs">Assignee<select value={assignee} onChange={event => setAssignee(event.target.value)} className={`${field} mt-1`}>{actors.map(actor => <option key={actor.actor_id} value={actor.actor_id}>{actor.display_name}</option>)}</select></label>
      <label className="block text-xs">Deadline<input type="date" value={due} onChange={event => setDue(event.target.value)} className={`${field} mt-1`} /></label>
      <fieldset className="max-h-36 overflow-auto"><legend className="mb-2 text-xs">Sources and objects</legend>{objects.filter(object => object.version_id).map(object => <label key={object.version_id} className="flex items-start gap-2 py-1 text-xs"><input type="checkbox" checked={refs.includes(object.version_id!)} onChange={event => setRefs(current => event.target.checked ? [...current, object.version_id!] : current.filter(id => id !== object.version_id))} /><span className="break-words">{object.title} · v{object.version_number}</span></label>)}</fieldset>
      {assignee.startsWith('agent:') && <label className="flex gap-2 text-sm"><input type="checkbox" checked={start} onChange={event => setStart(event.target.checked)} />Start now</label>}
      {error && <p role="alert" className="text-sm text-red-400">{error}</p>}
      <button type="submit" disabled={busy || !goal.trim()} className="rounded bg-blue-600 px-4 py-2 text-sm text-white disabled:opacity-50">{busy ? 'Creating...' : 'Create task'}</button>
    </form>
  </dialog>
}
