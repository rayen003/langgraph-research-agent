import { useRef, type ReactNode } from 'react'
import { ChevronsLeft, ChevronsRight } from 'lucide-react'
import { useResizable } from '../hooks/useResizable'

interface Props {
  children: ReactNode
  defaultWidth: number
  minWidth?: number
  maxWidth?: number
  /** Handle on the inner edge: `right` for left-docked panels, `left` for right-docked. */
  side: 'left' | 'right'
  storageKey: string
  className?: string
  innerClassName?: string
  /** When true, panel collapses to a slim reveal rail (resize width is preserved). */
  hidden?: boolean
  onReveal?: () => void
  /** Label on the vertical reveal rail when hidden. */
  revealLabel?: string
}

/**
 * Horizontal panel with drag-to-resize and optional hide-to-rail.
 * Handle sits outside panel bounds so it stays grabbable after shrinking.
 */
export function ResizablePanel({
  children,
  defaultWidth,
  minWidth,
  maxWidth,
  side,
  storageKey,
  className = '',
  innerClassName = '',
  hidden = false,
  onReveal,
  revealLabel = 'Panel',
}: Props) {
  const panelRef = useRef<HTMLDivElement>(null)
  const { width, handleProps } = useResizable({
    defaultWidth,
    minWidth,
    maxWidth,
    side,
    storageKey,
    panelRef,
  })

  if (hidden) {
    const RevealIcon = side === 'right' ? ChevronsRight : ChevronsLeft
    return (
      <div
        className={`flex-shrink-0 flex flex-col items-stretch border-border-subtle ${
          side === 'right' ? 'border-r' : 'border-l'
        } ${className}`}
      >
        <button
          type="button"
          onClick={onReveal}
          title={`Show ${revealLabel}`}
          aria-label={`Show ${revealLabel}`}
          className="group flex flex-col items-center justify-center gap-2 py-6 px-1.5 min-h-[120px] h-full hover:bg-bg-overlay transition-colors"
        >
          <RevealIcon size={14} className="text-ink-dim group-hover:text-ink-muted transition-colors" />
          <span className="text-[9px] font-medium uppercase tracking-[0.14em] text-ink-disabled group-hover:text-ink-dim [writing-mode:vertical-rl] rotate-180 select-none">
            {revealLabel}
          </span>
        </button>
      </div>
    )
  }

  return (
    <div
      ref={panelRef}
      data-resizable
      style={{ width }}
      className={`relative flex-shrink-0 ${className}`}
    >
      <div className={`h-full w-full min-w-0 flex flex-col overflow-hidden ${innerClassName}`}>
        {children}
      </div>
      <div
        role="separator"
        aria-orientation="vertical"
        aria-label="Resize panel"
        title="Drag to resize · double-click to reset"
        {...handleProps}
        className={`absolute top-0 bottom-0 w-3 z-50 cursor-col-resize touch-none select-none group ${
          side === 'right' ? 'right-0 translate-x-1/2' : 'left-0 -translate-x-1/2'
        }`}
      >
        <div className="absolute inset-y-0 left-1/2 w-px -translate-x-1/2 bg-edge/40 group-hover:bg-indigo-400/70 group-active:bg-indigo-400 transition-colors" />
      </div>
    </div>
  )
}
