import { useCallback, useEffect, useState } from 'react'

/** Persist panel visibility (hidden = collapsed to reveal rail) in localStorage. */
export function usePanelHidden(storageKey: string, defaultHidden = false) {
  const [hidden, setHidden] = useState(() => {
    try {
      const raw = localStorage.getItem(storageKey)
      if (raw === 'true') return true
      if (raw === 'false') return false
    } catch { /* ignore */ }
    return defaultHidden
  })

  useEffect(() => {
    try {
      localStorage.setItem(storageKey, String(hidden))
    } catch { /* quota */ }
  }, [hidden, storageKey])

  const hide = useCallback(() => setHidden(true), [])
  const show = useCallback(() => setHidden(false), [])
  const toggle = useCallback(() => setHidden(v => !v), [])

  return { hidden, hide, show, toggle, setHidden }
}
