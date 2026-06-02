import { ChevronsLeft, ChevronsRight } from 'lucide-react'

interface Props {
  onHide: () => void
  /** Panel docked on the left side of the layout → chevron points left. */
  edge?: 'left' | 'right'
  className?: string
  title?: string
}

/** Collapse a side panel to its reveal rail (width is preserved for resize). */
export function PanelHideButton({ onHide, edge = 'left', className = '', title = 'Hide panel' }: Props) {
  const Icon = edge === 'left' ? ChevronsLeft : ChevronsRight
  return (
    <button
      type="button"
      onClick={onHide}
      title={title}
      aria-label={title}
      className={`p-1.5 rounded-md text-ink-dim hover:text-ink-muted hover:bg-bg-overlay transition-colors ${className}`}
    >
      <Icon size={14} />
    </button>
  )
}
