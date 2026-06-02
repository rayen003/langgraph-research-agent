import { useEffect, useRef } from 'react'
import { MarkdownRenderer } from './MarkdownRenderer'
import type { ChatMessage, RunStatus } from '../types'

interface Props {
  messages: ChatMessage[]
  status: RunStatus
  onReset: () => void
}

export function ChatPane({ messages, status, onReset }: Props) {
  const bottomRef = useRef<HTMLDivElement>(null)
  const isResponding = status === 'chat_responding'

  useEffect(() => {
    if (isResponding) {
      bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
    }
  }, [messages, isResponding])

  return (
    <div className="flex-1 flex flex-col overflow-hidden min-w-0">
      {/* Top bar */}
      <div className="flex items-center gap-3 px-6 py-3.5 border-b border-border flex-shrink-0">
        <button
          onClick={onReset}
          className="text-zinc-700 hover:text-ink-muted transition-colors duration-150 flex-shrink-0"
          title="New session"
        >
          <svg width="14" height="14" viewBox="0 0 14 14" fill="none">
            <path
              d="M2 7H12M2 7L6 3M2 7L6 11"
              stroke="currentColor"
              strokeWidth="1.3"
              strokeLinecap="round"
              strokeLinejoin="round"
            />
          </svg>
        </button>
        <span className="text-sm text-ink-dim font-medium">Chat</span>
        <div className="flex-1" />
        {isResponding && (
          <div className="flex items-center gap-1.5">
            <div className="w-1.5 h-1.5 rounded-full bg-indigo-500 animate-pulse" />
            <span className="text-xs text-ink-dim">Responding…</span>
          </div>
        )}
      </div>

      {/* Message thread */}
      <div className="flex-1 overflow-y-auto px-6 py-5 space-y-6">
        {messages.map(msg => (
          <MessageBubble key={msg.id} message={msg} />
        ))}
        <div ref={bottomRef} />
      </div>
    </div>
  )
}

function MessageBubble({ message }: { message: ChatMessage }) {
  const isUser = message.role === 'user'

  if (isUser) {
    return (
      <div className="flex justify-end animate-fade-up">
        <div className="max-w-[75%] px-4 py-2.5 rounded-2xl rounded-tr-sm bg-surface-3 border border-border-accent">
          <p className="text-sm text-ink leading-relaxed whitespace-pre-wrap">{message.content}</p>
        </div>
      </div>
    )
  }

  return (
    <div className="flex justify-start animate-fade-up">
      <div className="max-w-[85%] space-y-1">
        {/* Assistant label */}
        <div className="flex items-center gap-1.5 px-1">
          <div className="w-4 h-4 rounded-md bg-indigo-500/20 border border-indigo-500/30 flex items-center justify-center flex-shrink-0">
            <svg width="8" height="8" viewBox="0 0 12 12" fill="none">
              <path d="M2 9L5 3L8 7L10 4" stroke="var(--color-accent-muted)" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
          </div>
          <span className="text-[11px] text-ink-dim font-medium">Agent</span>
        </div>

        {/* Content */}
        <div className="px-1">
          {message.content ? (
            <MarkdownRenderer content={message.content} streaming={message.streaming} />
          ) : (
            <ThinkingDots />
          )}
        </div>
      </div>
    </div>
  )
}

function ThinkingDots() {
  return (
    <div className="flex items-center gap-1 h-6">
      {[0, 1, 2].map(i => (
        <div
          key={i}
          className="w-1.5 h-1.5 rounded-full bg-zinc-600 animate-pulse"
          style={{ animationDelay: `${i * 150}ms` }}
        />
      ))}
    </div>
  )
}
