import type { KgNode } from '../hooks/useKnowledgeGraph'

const PCT_FIELDS = new Set([
  'revenue_growth',
  'fcff_margin',
  'wacc',
  'terminal_growth',
  'tax_rate',
])

const FIELD_LABELS: Record<string, string> = {
  revenue_growth: 'Revenue growth',
  fcff_margin: 'FCFF margin',
  wacc: 'WACC',
  terminal_growth: 'Terminal growth',
  tax_rate: 'Tax rate',
  base_revenue: 'Base revenue',
  shares_outstanding: 'Shares out',
  net_debt: 'Net debt',
}

function fmt(field: string, v: number): string {
  if (!isFinite(v)) return '—'
  if (PCT_FIELDS.has(field)) return (v * 100).toFixed(2) + '%'
  if (Math.abs(v) >= 1e6) return v.toLocaleString(undefined, { maximumFractionDigits: 0 })
  return v.toFixed(4)
}

/**
 * Build a human-readable diff message comparing the original run's assumptions
 * against the user's edited overrides. Returned text is the user-facing message
 * appended to the chat session.
 */
export function buildDcfDiffMessage(
  ticker: string,
  horizonYears: number,
  _originalRunNode: KgNode,
  originalAssumptions: KgNode[],
  overrides: Record<string, number>,
  target: 'current' | 'new',
): string {
  interface Change { label: string; before: string; after: string }
  const changes: Change[] = []
  for (const a of originalAssumptions) {
    const orig = typeof a.value === 'number' ? a.value : Number(a.value)
    const nu = overrides[a.field]
    if (!isFinite(orig) || !isFinite(nu)) continue
    if (Math.abs(orig - nu) < 1e-9) continue
    changes.push({
      label: FIELD_LABELS[a.field] || a.field,
      before: fmt(a.field, orig),
      after: fmt(a.field, nu),
    })
  }

  const targetTag = target === 'new' ? 'new chat' : 'current chat'
  const headline = `🔄  DCF Rerun · ${ticker} · ${horizonYears}y · ${targetTag}`

  if (changes.length === 0) {
    return `${headline}\n\n(no assumption changes — re-running prior model)`
  }

  // Right-pad label, before so the arrows align in monospace.
  const labelW = Math.max(...changes.map(c => c.label.length))
  const beforeW = Math.max(...changes.map(c => c.before.length))
  const rows = changes.map(c =>
    `  ${c.label.padEnd(labelW)}   ${c.before.padStart(beforeW)}  →  ${c.after}`,
  )

  return `${headline}\n\nChanged assumptions:\n${rows.join('\n')}`
}
