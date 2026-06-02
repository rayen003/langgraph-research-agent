import { type ReactNode } from 'react'

/**
 * Minimal inline markdown renderer — handles the subset the compare-chat LLM
 * emits: **bold**, numbered/bulleted lists, paragraphs. No dependency.
 * Deliberately NOT a full parser — keeps bundle lean, covers the actual output.
 */

function renderInline(text: string): ReactNode[] {
  // Split on **bold** spans, keep delimiters.
  const parts = text.split(/(\*\*[^*]+\*\*|`[^`]+`)/g)
  return parts.map((p, i) => {
    if (p.startsWith('**') && p.endsWith('**')) {
      return <strong key={i} className="font-semibold text-ink">{p.slice(2, -2)}</strong>
    }
    if (p.startsWith('`') && p.endsWith('`')) {
      return <code key={i} className="font-mono text-[0.92em] text-accent">{p.slice(1, -1)}</code>
    }
    return <span key={i}>{p}</span>
  })
}

export function Markdown({ text }: { text: string }) {
  const lines = text.split('\n')
  const blocks: ReactNode[] = []
  let list: { ordered: boolean; items: string[] } | null = null

  const flush = (key: number) => {
    if (!list) return
    const L = list
    blocks.push(
      L.ordered ? (
        <ol key={`l${key}`} className="list-decimal pl-4 space-y-1 my-1.5">
          {L.items.map((it, i) => <li key={i} className="leading-relaxed">{renderInline(it)}</li>)}
        </ol>
      ) : (
        <ul key={`l${key}`} className="list-disc pl-4 space-y-1 my-1.5">
          {L.items.map((it, i) => <li key={i} className="leading-relaxed">{renderInline(it)}</li>)}
        </ul>
      ),
    )
    list = null
  }

  lines.forEach((raw, idx) => {
    const line = raw.trim()
    if (!line) { flush(idx); return }
    const ordered = /^\d+[.)]\s+/.exec(line)
    const bullet = /^[-*•]\s+/.exec(line)
    if (ordered) {
      if (!list || !list.ordered) { flush(idx); list = { ordered: true, items: [] } }
      list.items.push(line.replace(/^\d+[.)]\s+/, ''))
    } else if (bullet) {
      if (!list || list.ordered) { flush(idx); list = { ordered: false, items: [] } }
      list.items.push(line.replace(/^[-*•]\s+/, ''))
    } else {
      flush(idx)
      blocks.push(<p key={`p${idx}`} className="leading-relaxed my-1">{renderInline(line)}</p>)
    }
  })
  flush(lines.length)

  return <div className="text-[13px] text-ink-muted">{blocks}</div>
}
