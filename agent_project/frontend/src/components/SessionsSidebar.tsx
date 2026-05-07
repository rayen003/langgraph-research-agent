import { useState } from 'react'
import type { Session } from '../types'

function timeAgo(iso: string): string {
  const diff = Date.now() - new Date(iso).getTime()
  const m = Math.floor(diff / 60000)
  if (m < 1) return 'just now'
  if (m < 60) return `${m}m ago`
  const h = Math.floor(m / 60)
  if (h < 24) return `${h}h ago`
  return `${Math.floor(h / 24)}d ago`
}

interface Props {
  sessions: Session[]
  activeId: string
  onSelect: (id: string) => void
  onNew: () => void
  onDelete: (id: string) => void
  disabled?: boolean
}

export function SessionsSidebar({ sessions, activeId, onSelect, onNew, onDelete, disabled }: Props) {
  const [hoveredId, setHoveredId] = useState<string | null>(null)

  return (
    <div className="w-[220px] flex-shrink-0 flex flex-col bg-[#080808] border-r border-[#141414] overflow-hidden">
      {/* Header */}
      <div className="px-4 pt-5 pb-3 flex-shrink-0">
        <div className="flex items-center gap-2 mb-4">
          <div className="w-5 h-5 rounded-md bg-indigo-500 flex items-center justify-center flex-shrink-0">
            <svg width="10" height="10" viewBox="0 0 12 12" fill="none">
              <path d="M2 9L5 3L8 7L10 4" stroke="white" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
          </div>
          <span className="text-xs font-semibold text-zinc-300 tracking-wide">Agent</span>
        </div>

        <button
          onClick={onNew}
          disabled={disabled}
          className={`
            w-full flex items-center gap-2 px-3 py-2 rounded-lg text-xs
            border border-[#222] text-zinc-500
            transition-colors duration-150
            ${disabled ? 'opacity-40 cursor-not-allowed' : 'hover:bg-[#111116] hover:text-zinc-300 hover:border-[#2a2a2a]'}
          `}
        >
          <svg width="10" height="10" viewBox="0 0 10 10" fill="none">
            <path d="M5 1.5V8.5M1.5 5H8.5" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" />
          </svg>
          New session
        </button>
      </div>

      {/* Session list */}
      <div className="flex-1 overflow-y-auto px-2 pb-4 space-y-0.5">
        {sessions.length === 0 && (
          <p className="px-3 py-4 text-[11px] text-zinc-700 text-center">No sessions yet</p>
        )}
        {sessions.map(s => {
          const isActive = s.id === activeId
          const isHovered = hoveredId === s.id
          return (
            <div
              key={s.id}
              className="relative group"
              onMouseEnter={() => setHoveredId(s.id)}
              onMouseLeave={() => setHoveredId(null)}
            >
              <button
                onClick={() => !disabled && onSelect(s.id)}
                disabled={disabled}
                className={`
                  w-full text-left px-3 py-2.5 rounded-lg transition-colors duration-100
                  ${disabled ? 'cursor-not-allowed' : ''}
                  ${isActive
                    ? 'bg-[#111116] text-zinc-200'
                    : 'text-zinc-500 hover:bg-[#0d0d0d] hover:text-zinc-300'}
                `}
              >
                <p className={`text-xs leading-snug truncate pr-5 ${isActive ? 'font-medium' : ''}`}>
                  {s.title}
                </p>
                <p className="text-[10px] text-zinc-700 mt-0.5">{timeAgo(s.createdAt)}</p>
              </button>

              {/* Delete button — only visible on hover, only for non-active or if multiple sessions */}
              {(isHovered || isActive) && sessions.length > 1 && (
                <button
                  onClick={e => { e.stopPropagation(); onDelete(s.id) }}
                  className="
                    absolute right-2 top-1/2 -translate-y-1/2
                    w-5 h-5 flex items-center justify-center rounded
                    text-zinc-700 hover:text-zinc-400 hover:bg-[#1a1a1a]
                    transition-colors duration-100 opacity-0 group-hover:opacity-100
                  "
                  title="Delete session"
                >
                  <svg width="8" height="8" viewBox="0 0 8 8" fill="none">
                    <path d="M1 1L7 7M7 1L1 7" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round" />
                  </svg>
                </button>
              )}
            </div>
          )
        })}
      </div>
    </div>
  )
}
