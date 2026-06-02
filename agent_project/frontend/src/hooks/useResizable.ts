import { useCallback, useRef, useState, type RefObject } from 'react'

interface Options {
  /** Initial width in px. */
  defaultWidth: number
  /** Min allowed width in px. */
  minWidth?: number
  /** Max allowed width in px. */
  maxWidth?: number
  /**
   * Which edge is being dragged.
   * 'right' = handle on right edge (drag right to expand) — left sidebars.
   * 'left'  = handle on left edge  (drag left to expand)  — right panels.
   */
  side?: 'right' | 'left'
  /** localStorage key to persist the width across sessions. */
  storageKey?: string
  /** Optional ref to the resizable panel root (avoids closest() misses). */
  panelRef?: RefObject<HTMLElement | null>
}

function clamp(n: number, min: number, max: number) {
  return Math.min(max, Math.max(min, n))
}

/**
 * useResizable — drag-to-resize hook.
 *
 * Returns `{ width, reset, handleProps }`.
 *
 * `handleProps` goes on the drag-handle element (a thin strip on the panel
 * edge). Double-click resets to defaultWidth.
 */
export function useResizable({
  defaultWidth,
  minWidth = 140,
  maxWidth = 600,
  side = 'right',
  storageKey,
  panelRef,
}: Options) {
  const init = () => {
    if (storageKey) {
      const stored = localStorage.getItem(storageKey)
      if (stored) {
        const n = Number(stored)
        if (isFinite(n) && n >= minWidth && n <= maxWidth) return n
      }
    }
    return defaultWidth
  }

  const [width, setWidth] = useState<number>(init)
  const widthRef = useRef(width)
  widthRef.current = width

  const persist = useCallback((w: number) => {
    if (storageKey) localStorage.setItem(storageKey, String(w))
  }, [storageKey])

  const resolvePanel = useCallback(
    (target: EventTarget | null) => {
      if (panelRef?.current) return panelRef.current
      return (target as HTMLElement | null)?.closest('[data-resizable]') as HTMLElement | null
    },
    [panelRef],
  )

  const applyWidth = useCallback((el: HTMLElement | null, next: number, commit: boolean) => {
    if (el) el.style.width = `${next}px`
    if (commit) {
      setWidth(next)
      persist(next)
      // Let React own width again after drag ends.
      if (el) el.style.width = ''
    }
  }, [persist])

  const reset = useCallback(() => {
    applyWidth(panelRef?.current ?? null, defaultWidth, true)
  }, [applyWidth, defaultWidth, panelRef])

  const onMouseDown = useCallback((e: React.MouseEvent) => {
    e.preventDefault()
    const panel = resolvePanel(e.target)
    const startX = e.clientX
    const startW = widthRef.current

    const onMove = (ev: MouseEvent) => {
      const delta = side === 'right' ? ev.clientX - startX : startX - ev.clientX
      const next = clamp(startW + delta, minWidth, maxWidth)
      document.body.style.cursor = 'col-resize'
      document.body.style.userSelect = 'none'
      applyWidth(panel, next, false)
    }

    const finish = (ev: MouseEvent | Event) => {
      document.removeEventListener('mousemove', onMove)
      document.removeEventListener('mouseup', finish)
      window.removeEventListener('blur', onBlur)
      document.body.style.cursor = ''
      document.body.style.userSelect = ''
      const mouse = ev instanceof MouseEvent ? ev : null
      const delta = mouse
        ? side === 'right'
          ? mouse.clientX - startX
          : startX - mouse.clientX
        : 0
      const next = clamp(startW + delta, minWidth, maxWidth)
      applyWidth(panel, next, true)
    }

    const onBlur = () => finish(new MouseEvent('mouseup'))

    document.addEventListener('mousemove', onMove)
    document.addEventListener('mouseup', finish)
    window.addEventListener('blur', onBlur)
  }, [side, minWidth, maxWidth, applyWidth, resolvePanel])

  return {
    width,
    reset,
    handleProps: {
      onMouseDown,
      onDoubleClick: (e: React.MouseEvent) => {
        e.preventDefault()
        e.stopPropagation()
        reset()
      },
    },
  }
}

interface HeightOptions {
  defaultHeight: number
  minHeight?: number
  maxHeight?: number
  storageKey?: string
  panelRef?: RefObject<HTMLElement | null>
}

/**
 * Vertical resize — drag the top edge up/down. Double-click resets height.
 */
export function useResizableHeight({
  defaultHeight,
  minHeight = 56,
  maxHeight = 240,
  storageKey,
  panelRef,
}: HeightOptions) {
  const init = () => {
    if (storageKey) {
      const stored = localStorage.getItem(storageKey)
      if (stored) {
        const n = Number(stored)
        if (isFinite(n) && n >= minHeight && n <= maxHeight) return n
      }
    }
    return defaultHeight
  }

  const [height, setHeight] = useState<number>(init)
  const heightRef = useRef(height)
  heightRef.current = height

  const persist = useCallback((h: number) => {
    if (storageKey) localStorage.setItem(storageKey, String(h))
  }, [storageKey])

  const resolvePanel = useCallback(
    (target: EventTarget | null) => {
      if (panelRef?.current) return panelRef.current
      return (target as HTMLElement | null)?.closest('[data-resizable-height]') as HTMLElement | null
    },
    [panelRef],
  )

  const applyHeight = useCallback((el: HTMLElement | null, next: number, commit: boolean) => {
    if (el) el.style.height = `${next}px`
    if (commit) {
      setHeight(next)
      persist(next)
      if (el) el.style.height = ''
    }
  }, [persist])

  const reset = useCallback(() => {
    applyHeight(panelRef?.current ?? null, defaultHeight, true)
  }, [applyHeight, defaultHeight, panelRef])

  const onMouseDown = useCallback((e: React.MouseEvent) => {
    e.preventDefault()
    const panel = resolvePanel(e.target)
    const startY = e.clientY
    const startH = heightRef.current

    const onMove = (ev: MouseEvent) => {
      // Drag up → taller; drag down → shorter.
      const delta = startY - ev.clientY
      const next = clamp(startH + delta, minHeight, maxHeight)
      document.body.style.cursor = 'row-resize'
      document.body.style.userSelect = 'none'
      applyHeight(panel, next, false)
    }

    const finish = (ev: MouseEvent | Event) => {
      document.removeEventListener('mousemove', onMove)
      document.removeEventListener('mouseup', finish)
      window.removeEventListener('blur', onBlur)
      document.body.style.cursor = ''
      document.body.style.userSelect = ''
      const mouse = ev instanceof MouseEvent ? ev : null
      const delta = mouse ? startY - mouse.clientY : 0
      const next = clamp(startH + delta, minHeight, maxHeight)
      applyHeight(panel, next, true)
    }

    const onBlur = () => finish(new MouseEvent('mouseup'))

    document.addEventListener('mousemove', onMove)
    document.addEventListener('mouseup', finish)
    window.addEventListener('blur', onBlur)
  }, [minHeight, maxHeight, applyHeight, resolvePanel])

  return {
    height,
    reset,
    handleProps: {
      onMouseDown,
      onDoubleClick: (e: React.MouseEvent) => {
        e.preventDefault()
        e.stopPropagation()
        reset()
      },
    },
  }
}
