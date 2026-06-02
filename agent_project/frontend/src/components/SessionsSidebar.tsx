import { useCallback, useEffect, useMemo, useRef, useState, type DragEvent, type ReactNode } from 'react'
import { ChevronDown, FolderPlus, GripVertical, MoreHorizontal, Pin } from 'lucide-react'
import type { Session, SessionGroup, SessionGroupColor } from '../types'
import { ResizablePanel } from './ResizablePanel'
import { PanelHideButton } from './PanelHideButton'

const GROUP_DOT: Record<SessionGroupColor, string> = {
  blue: 'bg-blue-500',
  teal: 'bg-teal-500',
  indigo: 'bg-indigo-500',
  slate: 'bg-slate-500',
  violet: 'bg-violet-500',
  amber: 'bg-amber-500',
}

const GROUP_RING: Record<SessionGroupColor, string> = {
  blue: 'ring-blue-500/40',
  teal: 'ring-teal-500/40',
  indigo: 'ring-indigo-500/40',
  slate: 'ring-slate-500/40',
  violet: 'ring-violet-500/40',
  amber: 'ring-amber-500/40',
}

function timeAgo(iso: string): string {
  const diff = Date.now() - new Date(iso).getTime()
  const m = Math.floor(diff / 60000)
  if (m < 1) return 'just now'
  if (m < 60) return `${m}m ago`
  const h = Math.floor(m / 60)
  if (h < 24) return `${h}h ago`
  return `${Math.floor(h / 24)}d ago`
}

function sortSessions(list: Session[]) {
  return [...list].sort((a, b) => (a.sortOrder ?? 0) - (b.sortOrder ?? 0))
}

interface Props {
  sessions: Session[]
  groups: SessionGroup[]
  activeId: string
  disabled?: boolean
  onSelect: (id: string) => void
  onNew: () => void
  onDelete: (id: string) => void
  onRename: (id: string, title: string) => void
  onPin: (id: string, pinned: boolean) => void
  onCreateGroup: () => void
  onUpdateGroup: (id: string, patch: Partial<Pick<SessionGroup, 'name' | 'color' | 'collapsed'>>) => void
  onDeleteGroup: (id: string) => void
  onMoveToGroup: (sessionId: string, groupId: string | null) => void
  onReorderSessions: (orderedIds: string[]) => void
  hidden?: boolean
  onHide?: () => void
  onReveal?: () => void
}

export function SessionsSidebar({
  sessions,
  groups,
  activeId,
  disabled,
  onSelect,
  onNew,
  onDelete,
  onRename,
  onPin,
  onCreateGroup,
  onUpdateGroup,
  onDeleteGroup,
  onMoveToGroup,
  onReorderSessions,
  hidden = false,
  onHide,
  onReveal,
}: Props) {
  const [menuSessionId, setMenuSessionId] = useState<string | null>(null)
  const [menuGroupId, setMenuGroupId] = useState<string | null>(null)
  const [editingId, setEditingId] = useState<string | null>(null)
  const [editTitle, setEditTitle] = useState('')
  const [dragSessionId, setDragSessionId] = useState<string | null>(null)
  const [dropTarget, setDropTarget] = useState<string | null>(null)
  const menuRef = useRef<HTMLDivElement>(null)

  const sortedGroups = useMemo(
    () => [...groups].sort((a, b) => a.sortOrder - b.sortOrder),
    [groups],
  )

  const pinned = useMemo(
    () => sortSessions(sessions.filter(s => s.pinned)),
    [sessions],
  )

  const ungrouped = useMemo(
    () => sortSessions(sessions.filter(s => !s.pinned && !s.groupId)),
    [sessions],
  )

  const sessionsByGroup = useMemo(() => {
    const map = new Map<string, Session[]>()
    for (const g of sortedGroups) map.set(g.id, [])
    for (const s of sessions) {
      if (s.pinned || !s.groupId) continue
      if (!map.has(s.groupId)) map.set(s.groupId, [])
      map.get(s.groupId)!.push(s)
    }
    for (const [k, list] of map) map.set(k, sortSessions(list))
    return map
  }, [sessions, sortedGroups])

  useEffect(() => {
    const close = (e: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) {
        setMenuSessionId(null)
        setMenuGroupId(null)
      }
    }
    document.addEventListener('mousedown', close)
    return () => document.removeEventListener('mousedown', close)
  }, [])

  const startRename = (s: Session) => {
    setEditingId(s.id)
    setEditTitle(s.title)
    setMenuSessionId(null)
  }

  const commitRename = () => {
    if (editingId && editTitle.trim()) onRename(editingId, editTitle.trim())
    setEditingId(null)
  }

  const handleDragStart = (e: DragEvent, sessionId: string) => {
    if (disabled) { e.preventDefault(); return }
    setDragSessionId(sessionId)
    e.dataTransfer.setData('text/session-id', sessionId)
    e.dataTransfer.effectAllowed = 'move'
  }

  const handleDragEnd = () => {
    setDragSessionId(null)
    setDropTarget(null)
  }

  const handleDropOnBucket = useCallback((
    e: DragEvent,
    bucket: { type: 'pinned' | 'ungrouped' | 'group'; groupId?: string },
    bucketSessions: Session[],
  ) => {
    e.preventDefault()
    const sessionId = e.dataTransfer.getData('text/session-id') || dragSessionId
    if (!sessionId) return

    const targetGroupId = bucket.type === 'group' ? bucket.groupId! : null
    if (bucket.type === 'pinned') {
      onPin(sessionId, true)
      onMoveToGroup(sessionId, null)
    } else {
      onMoveToGroup(sessionId, targetGroupId)
    }

    const without = bucketSessions.filter(s => s.id !== sessionId)
    const insertBefore = dropTarget && without.some(s => s.id === dropTarget)
      ? without.findIndex(s => s.id === dropTarget)
      : without.length
    const ordered = [...without.slice(0, insertBefore), sessions.find(s => s.id === sessionId)!, ...without.slice(insertBefore)]
      .filter(Boolean)
      .map(s => s.id)
    onReorderSessions(ordered)
    setDropTarget(null)
    setDragSessionId(null)
  }, [dragSessionId, dropTarget, onMoveToGroup, onPin, onReorderSessions, sessions])

  const renderSession = (s: Session, bucketSessions: Session[]) => {
    const isActive = s.id === activeId
    const isDragging = dragSessionId === s.id
    const isDropBefore = dropTarget === s.id && dragSessionId && dragSessionId !== s.id

    return (
      <div
        key={s.id}
        draggable={!disabled && editingId !== s.id}
        onDragStart={e => handleDragStart(e, s.id)}
        onDragEnd={handleDragEnd}
        onDragOver={e => { e.preventDefault(); setDropTarget(s.id) }}
        onDrop={e => handleDropOnBucket(e, s.groupId && !s.pinned
          ? { type: 'group', groupId: s.groupId }
          : s.pinned
            ? { type: 'pinned' }
            : { type: 'ungrouped' },
        bucketSessions)}
        className={`relative group rounded-lg ${isDragging ? 'opacity-40' : ''} ${isDropBefore ? 'ring-1 ring-accent/50' : ''}`}
      >
        {editingId === s.id ? (
          <div className="px-2 py-1.5">
            <input
              autoFocus
              value={editTitle}
              onChange={e => setEditTitle(e.target.value)}
              onBlur={commitRename}
              onKeyDown={e => {
                if (e.key === 'Enter') commitRename()
                if (e.key === 'Escape') setEditingId(null)
              }}
              className="w-full px-2 py-1.5 text-xs rounded-md bg-bg-input border border-border text-ink outline-none focus:border-accent/50"
            />
          </div>
        ) : (
          <>
            <button
              type="button"
              onClick={() => !disabled && onSelect(s.id)}
              disabled={disabled}
              className={`
                w-full text-left pl-2 pr-8 py-2.5 rounded-lg transition-colors duration-100 flex items-start gap-1.5
                ${disabled ? 'cursor-not-allowed' : ''}
                ${isActive ? 'bg-bg-overlay text-ink' : 'text-ink-dim hover:bg-bg-raised hover:text-ink-muted'}
              `}
            >
              <GripVertical size={12} className="flex-shrink-0 mt-0.5 text-ink-disabled opacity-0 group-hover:opacity-100 cursor-grab" />
              <div className="min-w-0 flex-1">
                <p className={`text-xs leading-snug truncate flex items-center gap-1 ${isActive ? 'font-medium' : ''}`}>
                  {s.pinned && <Pin size={10} className="flex-shrink-0 text-ink-dim" />}
                  {s.title}
                </p>
                <p className="text-[10px] text-ink-disabled mt-0.5">{timeAgo(s.createdAt)}</p>
              </div>
            </button>

            <div className="absolute right-1 top-1/2 -translate-y-1/2 flex items-center">
              <button
                type="button"
                onClick={e => {
                  e.stopPropagation()
                  setMenuGroupId(null)
                  setMenuSessionId(prev => (prev === s.id ? null : s.id))
                }}
                className="w-6 h-6 flex items-center justify-center rounded text-ink-disabled hover:text-ink-muted hover:bg-surface opacity-0 group-hover:opacity-100 transition-opacity"
                aria-label="Session options"
              >
                <MoreHorizontal size={14} />
              </button>
            </div>

            {menuSessionId === s.id && (
              <div
                ref={menuRef}
                className="absolute right-0 top-full mt-0.5 z-50 min-w-[140px] py-1 rounded-lg border border-border bg-bg-raised shadow-xl"
              >
                <MenuItem onClick={() => startRename(s)}>Rename</MenuItem>
                <MenuItem onClick={() => { onPin(s.id, !s.pinned); setMenuSessionId(null) }}>
                  {s.pinned ? 'Unpin' : 'Pin'}
                </MenuItem>
                {sortedGroups.length > 0 && (
                  <div className="border-t border-border-subtle my-1 pt-1">
                    <div className="px-3 py-1 text-[9px] uppercase tracking-wide text-ink-disabled">Move to</div>
                    <MenuItem onClick={() => { onMoveToGroup(s.id, null); setMenuSessionId(null) }}>Ungrouped</MenuItem>
                    {sortedGroups.map(g => (
                      <MenuItem key={g.id} onClick={() => { onMoveToGroup(s.id, g.id); setMenuSessionId(null) }}>
                        <span className={`inline-block w-1.5 h-1.5 rounded-full mr-1.5 ${GROUP_DOT[g.color]}`} />
                        {g.name}
                      </MenuItem>
                    ))}
                  </div>
                )}
                {sessions.length > 1 && (
                  <>
                    <div className="border-t border-border-subtle my-1" />
                    <MenuItem danger onClick={() => { onDelete(s.id); setMenuSessionId(null) }}>Delete</MenuItem>
                  </>
                )}
              </div>
            )}
          </>
        )}
      </div>
    )
  }

  return (
    <ResizablePanel
      defaultWidth={220}
      minWidth={160}
      maxWidth={420}
      side="right"
      storageKey="ui.sessionsSidebarWidth"
      className="bg-bg border-r border-border-subtle"
      hidden={hidden}
      onReveal={onReveal}
      revealLabel="Chats"
    >
      <div className="flex flex-col h-full overflow-hidden">
        <div className="px-4 pt-5 pb-3 flex-shrink-0 space-y-2">
          <div className="flex items-center gap-2 mb-2">
            <div className="w-5 h-5 rounded-md bg-accent flex items-center justify-center flex-shrink-0">
              <svg width="10" height="10" viewBox="0 0 12 12" fill="none">
                <path d="M2 9L5 3L8 7L10 4" stroke="white" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
              </svg>
            </div>
            <span className="text-xs font-semibold text-ink-muted tracking-wide flex-1">Agent</span>
            {onHide && <PanelHideButton onHide={onHide} edge="left" />}
          </div>

          <button
            type="button"
            onClick={onNew}
            disabled={disabled}
            className={`
              w-full flex items-center gap-2 px-3 py-2 rounded-lg text-xs
              border border-border text-ink-dim transition-colors duration-150
              ${disabled ? 'opacity-40 cursor-not-allowed' : 'hover:bg-bg-overlay hover:text-ink-muted hover:border-border-hover'}
            `}
          >
            <svg width="10" height="10" viewBox="0 0 10 10" fill="none">
              <path d="M5 1.5V8.5M1.5 5H8.5" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" />
            </svg>
            New session
          </button>

          <button
            type="button"
            onClick={onCreateGroup}
            disabled={disabled}
            className={`
              w-full flex items-center gap-2 px-3 py-1.5 rounded-lg text-[11px]
              text-ink-dim transition-colors duration-150
              ${disabled ? 'opacity-40 cursor-not-allowed' : 'hover:bg-bg-raised hover:text-ink-muted'}
            `}
          >
            <FolderPlus size={12} />
            New group
          </button>
        </div>

        <div className="flex-1 overflow-y-auto px-2 pb-4 space-y-3">
          {pinned.length > 0 && (
            <section
              onDragOver={e => e.preventDefault()}
              onDrop={e => handleDropOnBucket(e, { type: 'pinned' }, pinned)}
            >
              <div className="px-2 py-1 text-[9px] uppercase tracking-wide text-ink-disabled flex items-center gap-1">
                <Pin size={10} /> Pinned
              </div>
              <div className="space-y-0.5">{pinned.map(s => renderSession(s, pinned))}</div>
            </section>
          )}

          {sortedGroups.map(g => {
            const groupSessions = sessionsByGroup.get(g.id) ?? []
            return (
              <section
                key={g.id}
                className={`relative rounded-lg ${dragSessionId ? 'ring-1 ring-transparent hover:ring-border' : ''}`}
                onDragOver={e => { e.preventDefault(); e.currentTarget.classList.add('ring-accent/30') }}
                onDragLeave={e => e.currentTarget.classList.remove('ring-accent/30')}
                onDrop={e => {
                  e.currentTarget.classList.remove('ring-accent/30')
                  handleDropOnBucket(e, { type: 'group', groupId: g.id }, groupSessions)
                }}
              >
                <div className={`relative flex items-center gap-1.5 px-2 py-1.5 rounded-md group/header ${GROUP_RING[g.color]} ring-1 ring-inset`}>
                  <button
                    type="button"
                    onClick={() => onUpdateGroup(g.id, { collapsed: !g.collapsed })}
                    className="p-0.5 text-ink-dim hover:text-ink-muted"
                  >
                    <ChevronDown size={12} className={`transition-transform ${g.collapsed ? '-rotate-90' : ''}`} />
                  </button>
                  <span className={`w-2 h-2 rounded-full flex-shrink-0 ${GROUP_DOT[g.color]}`} />
                  <span className="text-[11px] font-medium text-ink-muted truncate flex-1">{g.name}</span>
                  <span className="text-[10px] text-ink-disabled tabular-nums">{groupSessions.length}</span>
                  <button
                    type="button"
                    onClick={() => setMenuGroupId(prev => (prev === g.id ? null : g.id))}
                    className="p-0.5 text-ink-disabled hover:text-ink-muted opacity-0 group-hover/header:opacity-100"
                  >
                    <MoreHorizontal size={12} />
                  </button>
                  {menuGroupId === g.id && (
                    <div className="absolute right-2 top-8 z-50 min-w-[140px] py-1 rounded-lg border border-border bg-bg-raised shadow-xl">
                      <MenuItem onClick={() => {
                        const name = window.prompt('Group name', g.name)
                        if (name?.trim()) onUpdateGroup(g.id, { name: name.trim() })
                        setMenuGroupId(null)
                      }}>Rename</MenuItem>
                      <div className="px-3 py-1.5 flex gap-1.5">
                        {(Object.keys(GROUP_DOT) as SessionGroupColor[]).map(c => (
                          <button
                            key={c}
                            type="button"
                            title={c}
                            onClick={() => { onUpdateGroup(g.id, { color: c }); setMenuGroupId(null) }}
                            className={`w-4 h-4 rounded-full ${GROUP_DOT[c]} ${g.color === c ? 'ring-2 ring-ink/30' : 'opacity-70 hover:opacity-100'}`}
                          />
                        ))}
                      </div>
                      <div className="border-t border-border-subtle my-1" />
                      <MenuItem danger onClick={() => { onDeleteGroup(g.id); setMenuGroupId(null) }}>Delete group</MenuItem>
                    </div>
                  )}
                </div>
                {!g.collapsed && (
                  <div className="mt-0.5 space-y-0.5 pl-1">
                    {groupSessions.length === 0 ? (
                      <p className="px-3 py-2 text-[10px] text-ink-disabled italic">Drop chats here</p>
                    ) : (
                      groupSessions.map(s => renderSession(s, groupSessions))
                    )}
                  </div>
                )}
              </section>
            )
          })}

          {ungrouped.length > 0 && (
            <section
              onDragOver={e => e.preventDefault()}
              onDrop={e => handleDropOnBucket(e, { type: 'ungrouped' }, ungrouped)}
            >
              {sortedGroups.length > 0 && (
                <div className="px-2 py-1 text-[9px] uppercase tracking-wide text-ink-disabled">Chats</div>
              )}
              <div className="space-y-0.5">{ungrouped.map(s => renderSession(s, ungrouped))}</div>
            </section>
          )}

          {sessions.length === 0 && (
            <p className="px-3 py-4 text-[11px] text-ink-disabled text-center">No sessions yet</p>
          )}
        </div>
      </div>
    </ResizablePanel>
  )
}

function MenuItem({
  children,
  onClick,
  danger,
}: {
  children: ReactNode
  onClick: () => void
  danger?: boolean
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={`w-full text-left px-3 py-1.5 text-xs transition-colors ${
        danger
          ? 'text-down hover:bg-danger-soft'
          : 'text-ink-muted hover:bg-bg-overlay hover:text-ink'
      }`}
    >
      {children}
    </button>
  )
}
