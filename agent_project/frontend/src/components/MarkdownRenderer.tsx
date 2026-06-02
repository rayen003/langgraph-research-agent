import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { createContext, useContext } from 'react'
import type { Components } from 'react-markdown'

// Track list nesting depth so we can style top-level vs nested items differently
const ListDepthCtx = createContext(0)

const components: Components = {
  // ── Headings ──────────────────────────────────────────────────────────────
  h1: ({ children }) => (
    <h1 className="mt-8 mb-3 text-lg font-semibold text-ink tracking-tight border-b border-border pb-2">
      {children}
    </h1>
  ),
  h2: ({ children }) => (
    <h2 className="mt-7 mb-3 text-base font-semibold text-ink tracking-tight flex items-center gap-2">
      <span className="inline-block w-0.5 h-4 rounded-full bg-indigo-500 flex-shrink-0" />
      {children}
    </h2>
  ),
  h3: ({ children }) => (
    <h3 className="mt-5 mb-2 text-sm font-semibold text-ink-muted tracking-tight">
      {children}
    </h3>
  ),
  h4: ({ children }) => (
    <h4 className="mt-4 mb-1.5 text-sm font-medium text-ink-muted uppercase tracking-wider">
      {children}
    </h4>
  ),

  // ── Body ──────────────────────────────────────────────────────────────────
  p: ({ children }) => (
    <p className="mt-3 text-sm text-ink-muted leading-[1.85] first:mt-0">
      {children}
    </p>
  ),
  strong: ({ children }) => (
    <strong className="font-semibold text-ink">{children}</strong>
  ),
  em: ({ children }) => (
    <em className="italic text-ink-muted">{children}</em>
  ),

  // ── Lists — depth-aware ───────────────────────────────────────────────────
  ul: ({ children }) => {
    const depth = useContext(ListDepthCtx)
    return (
      <ListDepthCtx.Provider value={depth + 1}>
        <ul className={
          depth === 0
            ? 'mt-3 space-y-2 pl-0 list-none first:mt-0'
            : 'mt-1.5 space-y-1 pl-4 list-none'
        }>
          {children}
        </ul>
      </ListDepthCtx.Provider>
    )
  },
  ol: ({ children }) => {
    const depth = useContext(ListDepthCtx)
    return (
      <ListDepthCtx.Provider value={depth + 1}>
        <ol className={
          depth === 0
            ? 'mt-3 space-y-2 pl-0 list-none first:mt-0'
            : 'mt-1.5 space-y-1 pl-4 list-none'
        }>
          {children}
        </ol>
      </ListDepthCtx.Provider>
    )
  },
  li: ({ children }) => {
    const depth = useContext(ListDepthCtx)
    const isTop = depth === 1
    return (
      <li className={`flex items-start gap-2.5 text-sm leading-[1.8] ${
        isTop ? 'text-ink-muted' : 'text-ink-muted'
      }`}>
        <span className={`flex-shrink-0 rounded-full ${
          isTop
            ? 'mt-[0.62rem] w-1.5 h-1.5 bg-indigo-400/80'
            : 'mt-[0.62rem] w-1 h-1 bg-zinc-600'
        }`} />
        <span className="flex-1 min-w-0">{children}</span>
      </li>
    )
  },

  // ── Blockquote ────────────────────────────────────────────────────────────
  blockquote: ({ children }) => (
    <blockquote className="mt-4 pl-4 border-l-2 border-indigo-500/30 text-ink-muted text-sm italic">
      {children}
    </blockquote>
  ),

  // ── Code ──────────────────────────────────────────────────────────────────
  code: ({ children, className }) => {
    const isInline = !className
    return isInline ? (
      <code className="px-1.5 py-0.5 rounded bg-surface-2 text-indigo-300 text-[0.8em] font-mono">
        {children}
      </code>
    ) : (
      <code className={`block text-[0.78rem] font-mono text-ink-muted ${className ?? ''}`}>
        {children}
      </code>
    )
  },
  pre: ({ children }) => (
    <pre className="mt-4 rounded-xl bg-bg border border-border px-4 py-3.5 overflow-x-auto text-[0.78rem] leading-relaxed">
      {children}
    </pre>
  ),

  // ── Table ─────────────────────────────────────────────────────────────────
  table: ({ children }) => (
    <div className="mt-5 overflow-x-auto rounded-xl border border-border-hover">
      <table className="w-full text-sm border-collapse">{children}</table>
    </div>
  ),
  thead: ({ children }) => (
    <thead className="bg-bg-overlay">{children}</thead>
  ),
  th: ({ children }) => (
    <th className="px-3 py-2 text-left text-[11px] font-semibold text-ink-dim uppercase tracking-wider border-b border-border-hover align-top">
      {children}
    </th>
  ),
  td: ({ children }) => (
    <td className="px-3 py-2 text-sm text-ink-muted border-b border-border last:border-none align-top break-words whitespace-normal">
      {children}
    </td>
  ),
  tr: ({ children }) => (
    <tr className="hover:bg-bg-overlay transition-colors duration-100">{children}</tr>
  ),

  // ── Misc ──────────────────────────────────────────────────────────────────
  hr: () => (
    <hr className="my-6 border-none h-px bg-surface-3" />
  ),
  a: ({ href, children }) => (
    <a
      href={href}
      target="_blank"
      rel="noopener noreferrer"
      className="text-indigo-400 hover:text-indigo-300 underline underline-offset-2 decoration-indigo-500/30 transition-colors"
    >
      {children}
    </a>
  ),
}

interface Props {
  content: string
  streaming?: boolean
  onCitationClick?: (citationNumber: string) => void
}

export function MarkdownRenderer({ content, streaming = false, onCitationClick }: Props) {
  const activeComponents: Components = onCitationClick
    ? {
        ...components,
        a: ({ href, children }) => {
          const match = String(href ?? '').match(/^#source-(\d+)$/)
          if (match) {
            return (
              <button
                type="button"
                onClick={() => onCitationClick(match[1])}
                className="text-indigo-300 hover:text-indigo-200 underline underline-offset-2 decoration-indigo-500/40 transition-colors"
              >
                {children}
              </button>
            )
          }
          return (
            <a
              href={href}
              target="_blank"
              rel="noopener noreferrer"
              className="text-indigo-400 hover:text-indigo-300 underline underline-offset-2 decoration-indigo-500/30 transition-colors"
            >
              {children}
            </a>
          )
        },
      }
    : components

  return (
    <div className="min-w-0">
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={activeComponents}>
        {content}
      </ReactMarkdown>
      {streaming && (
        <span className="inline-block w-0.5 h-[1.1em] bg-zinc-400 animate-blink align-middle ml-0.5 translate-y-px" />
      )}
    </div>
  )
}
