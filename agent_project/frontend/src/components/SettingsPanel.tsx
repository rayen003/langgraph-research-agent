import { useState, useRef, useEffect } from 'react'
import { useTheme } from '../hooks/useTheme'

export function SettingsButton() {
  const [open, setOpen] = useState(false)
  const panelRef = useRef<HTMLDivElement>(null)

  // Close on click-outside
  useEffect(() => {
    if (!open) return
    const handler = (e: MouseEvent) => {
      if (panelRef.current && !panelRef.current.contains(e.target as Node)) {
        setOpen(false)
      }
    }
    // Use setTimeout so the opening click doesn't immediately close
    const id = setTimeout(() => document.addEventListener('mousedown', handler), 0)
    return () => {
      clearTimeout(id)
      document.removeEventListener('mousedown', handler)
    }
  }, [open])

  // Close on Escape
  useEffect(() => {
    if (!open) return
    const handler = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false)
    }
    document.addEventListener('keydown', handler)
    return () => document.removeEventListener('keydown', handler)
  }, [open])

  return (
    <>
      {/* ── Floating settings gear (bottom-left) ──── */}
      <button
        type="button"
        onClick={() => setOpen(prev => !prev)}
        className="fixed bottom-4 left-4 z-40 flex items-center justify-center w-9 h-9 rounded-xl border border-border-hover bg-bg-overlay text-ink-dim hover:text-ink-muted hover:border-border-hover shadow-lg shadow-black/40 transition-colors duration-150"
        title="Settings"
      >
        <svg width="15" height="15" viewBox="0 0 16 16" fill="none">
          <path
            d="M6.9 1.7a.5.5 0 0 1 .5-.4h1.2a.5.5 0 0 1 .5.4l.3 1.2a.5.5 0 0 0 .3.3l.3.2a.5.5 0 0 0 .4 0l1.2-.4a.5.5 0 0 1 .5.2l.6 1a.5.5 0 0 1-.1.5l-.8 1a.5.5 0 0 0-.1.4v.4a.5.5 0 0 0 .1.4l.8 1a.5.5 0 0 1 .1.5l-.6 1a.5.5 0 0 1-.5.2l-1.2-.4a.5.5 0 0 0-.4 0l-.3.2a.5.5 0 0 0-.3.3l-.3 1.2a.5.5 0 0 1-.5.4H7.4a.5.5 0 0 1-.5-.4l-.3-1.2a.5.5 0 0 0-.3-.3l-.3-.2a.5.5 0 0 0-.4 0l-1.2.4a.5.5 0 0 1-.5-.2l-.6-1a.5.5 0 0 1 .1-.5l.8-1a.5.5 0 0 0 .1-.4v-.4a.5.5 0 0 0-.1-.4l-.8-1a.5.5 0 0 1-.1-.5l.6-1a.5.5 0 0 1 .5-.2l1.2.4a.5.5 0 0 0 .4 0l.3-.2a.5.5 0 0 0 .3-.3l.3-1.2Z"
            stroke="currentColor"
            strokeWidth="1.1"
          />
          <circle cx="8" cy="8" r="2" stroke="currentColor" strokeWidth="1.1" />
        </svg>
      </button>

      {/* ── Settings panel ──── */}
      {open && (
        <div
          ref={panelRef}
          className="fixed bottom-16 left-4 z-50 w-72 rounded-xl border border-border bg-bg-raised shadow-xl shadow-black/50 animate-step-reveal"
        >
          <div className="flex items-center justify-between px-4 pt-4 pb-2">
            <span className="text-sm font-medium text-ink">Settings</span>
            <button
              type="button"
              onClick={() => setOpen(false)}
              className="text-ink-dim hover:text-ink-muted transition-colors"
            >
              <svg width="14" height="14" viewBox="0 0 16 16" fill="none">
                <path d="M4 4l8 8M12 4l-8 8" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" />
              </svg>
            </button>
          </div>

          <SettingsContent />

          <div className="px-4 pb-4 pt-2">
            <div className="text-[10px] text-ink-disabled tracking-wide uppercase">More settings coming soon</div>
          </div>
        </div>
      )}
    </>
  )
}

function SettingsContent() {
  const { theme, toggle } = useTheme()

  return (
    <div className="px-4 pb-2 space-y-3">
      {/* ── Appearance toggle ──── */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          {theme === 'dark' ? (
            <svg width="14" height="14" viewBox="0 0 16 16" fill="none">
              <path d="M13.5 8.5A5.5 5.5 0 0 1 7.5 2.5a5.5 5.5 0 1 0 6 6Z" stroke="currentColor" strokeWidth="1.4" strokeLinejoin="round" />
            </svg>
          ) : (
            <svg width="14" height="14" viewBox="0 0 16 16" fill="none">
              <circle cx="8" cy="8" r="4" stroke="currentColor" strokeWidth="1.4" />
              <path d="M8 1.5v1M8 13.5v1M2.5 8h-1M14.5 8h-1M3.4 3.4l.7.7M11.9 11.9l.7.7M3.4 12.6l.7-.7M11.9 4.1l.7-.7" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round" />
            </svg>
          )}
          <span className="text-sm text-ink-muted">Appearance</span>
        </div>

        <button
          type="button"
          onClick={toggle}
          className={`
            relative inline-flex h-6 w-11 items-center rounded-full transition-colors duration-200
            ${theme === 'dark' ? 'bg-surface-3' : 'bg-accent'}
          `}
          title={theme === 'dark' ? 'Switch to light mode' : 'Switch to dark mode'}
        >
          <span
            className={`
              inline-block h-4 w-4 rounded-full bg-white transition-transform duration-200
              ${theme === 'dark' ? 'translate-x-1' : 'translate-x-6'}
            `}
          />
        </button>
      </div>

      <div className="text-xs text-ink-dim">
        {theme === 'dark' ? 'Dark' : 'Light'} mode
      </div>
    </div>
  )
}