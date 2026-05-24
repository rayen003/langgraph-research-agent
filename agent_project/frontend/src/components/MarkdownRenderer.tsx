import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { createContext, useContext } from 'react'
import type { Components } from 'react-markdown'

// Track list nesting depth so we can style top-level vs nested items differently
const ListDepthCtx = createContext(0)

const components: Components = {
  // ── Headings ──────────────────────────────────────────────────────────────
  h1: ({ children }) => (
    <h1 className="mt-8 mb-3 text-lg font-semibold text-zinc-100 tracking-tight border-b border-[#1e1e1e] pb-2">
      {children}
    </h1>
  ),
  h2: ({ children }) => (
    <h2 className="mt-7 mb-3 text-base font-semibold text-zinc-100 tracking-tight flex items-center gap-2">
      <span className="inline-block w-0.5 h-4 rounded-full bg-indigo-500 flex-shrink-0" />
      {children}
    </h2>
  ),
  h3: ({ children }) => (
    <h3 className="mt-5 mb-2 text-sm font-semibold text-zinc-300 tracking-tight">
      {children}
    </h3>
  ),
  h4: ({ children }) => (
    <h4 className="mt-4 mb-1.5 text-sm font-medium text-zinc-400 uppercase tracking-wider">
      {children}
    </h4>
  ),

  // ── Body ──────────────────────────────────────────────────────────────────
  p: ({ children }) => (
    <p className="mt-3 text-sm text-zinc-300 leading-[1.85] first:mt-0">
      {children}
    </p>
  ),
  strong: ({ children }) => (
    <strong className="font-semibold text-zinc-100">{children}</strong>
  ),
  em: ({ children }) => (
    <em className="italic text-zinc-400">{children}</em>
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
        isTop ? 'text-zinc-300' : 'text-zinc-400'
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
    <blockquote className="mt-4 pl-4 border-l-2 border-indigo-500/30 text-zinc-400 text-sm italic">
      {children}
    </blockquote>
  ),

  // ── Code ──────────────────────────────────────────────────────────────────
  code: ({ children, className }) => {
    const isInline = !className
    return isInline ? (
      <code className="px-1.5 py-0.5 rounded bg-[#1a1a1e] text-indigo-300 text-[0.8em] font-mono">
        {children}
      </code>
    ) : (
      <code className={`block text-[0.78rem] font-mono text-zinc-300 ${className ?? ''}`}>
        {children}
      </code>
    )
  },
  pre: ({ children }) => (
    <pre className="mt-4 rounded-xl bg-[#0d0d10] border border-[#1e1e1e] px-4 py-3.5 overflow-x-auto text-[0.78rem] leading-relaxed">
      {children}
    </pre>
  ),

  // ── Table ─────────────────────────────────────────────────────────────────
  table: ({ children }) => (
    <div className="mt-5 overflow-x-auto rounded-xl border border-[#222]">
      <table className="w-full text-sm border-collapse">{children}</table>
    </div>
  ),
  thead: ({ children }) => (
    <thead className="bg-[#111116]">{children}</thead>
  ),
  th: ({ children }) => (
    <th className="px-3 py-2 text-left text-[11px] font-semibold text-zinc-500 uppercase tracking-wider border-b border-[#222] align-top">
      {children}
    </th>
  ),
  td: ({ children }) => (
    <td className="px-3 py-2 text-sm text-zinc-300 border-b border-[#1a1a1a] last:border-none align-top break-words whitespace-normal">
      {children}
    </td>
  ),
  tr: ({ children }) => (
    <tr className="hover:bg-[#111116] transition-colors duration-100">{children}</tr>
  ),

  // ── Misc ──────────────────────────────────────────────────────────────────
  hr: () => (
    <hr className="my-6 border-none h-px bg-[#1e1e1e]" />
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
