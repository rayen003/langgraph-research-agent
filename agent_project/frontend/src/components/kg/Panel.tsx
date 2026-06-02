import { type ReactNode } from 'react'
import { X } from 'lucide-react'

/**
 * Shared dock-panel chrome. All KG detail panels (inspector, news, category,
 * compare, query) render inside this so they share header, padding, scroll,
 * and the single-accent visual language. The panel fills the right dock — it
 * never floats over the canvas.
 */
interface PanelProps {
  icon?: ReactNode
  title: string
  subtitle?: string
  actions?: ReactNode      // header-right controls (before close)
  footer?: ReactNode       // pinned bottom bar
  onClose: () => void
  children: ReactNode
}

export function Panel({ icon, title, subtitle, actions, footer, onClose, children }: PanelProps) {
  return (
    <div className="flex h-full flex-col bg-surface">
      <header className="flex items-center gap-2.5 px-4 h-12 border-b border-edge flex-shrink-0">
        {icon && <span className="text-ink-muted">{icon}</span>}
        <div className="min-w-0 flex-1">
          <div className="text-[14px] font-medium text-ink truncate leading-tight">{title}</div>
          {subtitle && <div className="text-[11px] text-ink-dim truncate">{subtitle}</div>}
        </div>
        {actions}
        <button
          onClick={onClose}
          aria-label="Close panel"
          className="text-ink-dim hover:text-ink p-1 -mr-1 rounded hover:bg-surface-2 transition"
        >
          <X size={16} />
        </button>
      </header>

      <div className="flex-1 overflow-y-auto">{children}</div>

      {footer && (
        <footer className="border-t border-edge px-4 py-3 flex-shrink-0">{footer}</footer>
      )}
    </div>
  )
}

/** Section caption — 11px uppercase, the consistent label style. */
export function Caption({ children }: { children: ReactNode }) {
  return (
    <div className="text-[11px] uppercase tracking-wide text-ink-dim font-medium mb-2">
      {children}
    </div>
  )
}
