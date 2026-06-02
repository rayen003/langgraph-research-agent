import { useState, useRef, useEffect } from 'react'
import type { DocumentInfo, Mode } from '../types'
import { AttachmentChip } from './AttachmentChip'

const MODE_OPTIONS: { value: Mode; label: string; hint: string }[] = [
  { value: 'auto', label: 'Auto', hint: 'Automatically decides research or chat' },
  { value: 'research', label: 'Research', hint: 'Full research plan with HITL approval' },
  { value: 'chat', label: 'Chat', hint: 'Quick conversational answer' },
]

const MODE_ICON: Record<Mode, React.ReactNode> = {
  auto: (
    <svg width="10" height="10" viewBox="0 0 10 10" fill="none">
      <circle cx="5" cy="5" r="3.5" stroke="currentColor" strokeWidth="1.2" />
      <path d="M5 2V5L7 6.5" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round" />
    </svg>
  ),
  research: (
    <svg width="10" height="10" viewBox="0 0 10 10" fill="none">
      <circle cx="4" cy="4" r="2.5" stroke="currentColor" strokeWidth="1.2" />
      <path d="M6.5 6.5L8.5 8.5" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round" />
    </svg>
  ),
  chat: (
    <svg width="10" height="10" viewBox="0 0 10 10" fill="none">
      <path d="M1.5 2.5h7a.5.5 0 0 1 .5.5v4a.5.5 0 0 1-.5.5H3L1 9.5V3a.5.5 0 0 1 .5-.5z" stroke="currentColor" strokeWidth="1.2" strokeLinejoin="round" />
    </svg>
  ),
}

interface Props {
  onSubmit: (query: string, mode: Mode) => void
  onUpload?: (file: File) => void
  disabled: boolean
  autoFocus?: boolean
  mode: Mode
  onModeChange: (mode: Mode) => void
  docs?: DocumentInfo[]
  selectedDocId?: string | null
  onSelectDoc?: (docId: string) => void
  onRemoveDoc?: (docId: string) => void
}

export function QueryInput({
  onSubmit,
  onUpload,
  disabled,
  autoFocus,
  mode,
  onModeChange,
  docs = [],
  selectedDocId,
  onSelectDoc,
  onRemoveDoc,
}: Props) {
  const [value, setValue] = useState('')
  const [modeOpen, setModeOpen] = useState(false)
  const [sendingAttachments, setSendingAttachments] = useState(false)
  const ref = useRef<HTMLTextAreaElement>(null)
  const modeRef = useRef<HTMLDivElement>(null)
  const fileRef = useRef<HTMLInputElement>(null)

  useEffect(() => {
    if (autoFocus) ref.current?.focus()
  }, [autoFocus])

  useEffect(() => {
    const el = ref.current
    if (!el) return
    el.style.height = 'auto'
    el.style.height = `${Math.min(el.scrollHeight, 160)}px`
  }, [value])

  useEffect(() => {
    const handler = (e: MouseEvent) => {
      if (modeRef.current && !modeRef.current.contains(e.target as Node)) {
        setModeOpen(false)
      }
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [])

  const submit = () => {
    const q = value.trim()
    if (!q || disabled || sendingAttachments) return

    const send = () => {
      onSubmit(q, mode)
      setValue('')
      setSendingAttachments(false)
    }

    if (docs.length > 0) {
      setSendingAttachments(true)
      window.setTimeout(send, 240)
    } else {
      send()
    }
  }

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      submit()
    }
  }

  const handleFileChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const list = e.target.files
    if (!list?.length || !onUpload) return
    Array.from(list).forEach(file => onUpload(file))
    e.target.value = ''
  }

  const currentMode = MODE_OPTIONS.find(o => o.value === mode) ?? MODE_OPTIONS[0]
  const hasAttachments = docs.length > 0

  return (
    <div className="relative group z-20">
      <div
        className={`
          rounded-xl border bg-bg-raised
          ${disabled ? 'border-border-subtle' : 'border-border-hover focus-within:border-border-accent'}
          transition-colors duration-200
        `}
      >
        {hasAttachments && (
          <div className="flex gap-2 px-3 pt-3 pb-1 overflow-x-auto scrollbar-thin">
            {docs.map((doc, i) => (
              <AttachmentChip
                key={doc.doc_id}
                doc={doc}
                selected={doc.doc_id === selectedDocId}
                onSelect={onSelectDoc}
                onRemove={sendingAttachments ? undefined : onRemoveDoc}
                animationDelayMs={i * 45}
                exiting={sendingAttachments}
              />
            ))}
          </div>
        )}

        <div className={`flex items-end gap-3 px-4 overflow-visible ${hasAttachments ? 'pt-2 pb-3' : 'py-3'}`}>
          <div ref={modeRef} className="relative flex-shrink-0 mb-0.5 z-30">
            <button
              type="button"
            onClick={() => !disabled && !sendingAttachments && setModeOpen(v => !v)}
            disabled={disabled || sendingAttachments}
              className={`
                flex items-center gap-1.5 px-2 py-1 rounded-md text-[11px] font-medium
                border transition-colors duration-150
                ${disabled
                  ? 'border-border-subtle text-ink-disabled cursor-not-allowed'
                  : mode === 'auto'
                    ? 'border-border text-ink-dim hover:text-ink-muted hover:border-border-hover'
                    : mode === 'research'
                      ? 'border-indigo-900/60 text-indigo-400 bg-indigo-950/30'
                      : 'border-cyan-900/60 text-cyan-400 bg-cyan-950/20'
                }
              `}
            >
              <span className="flex-shrink-0">{MODE_ICON[mode]}</span>
              {currentMode.label}
              <svg width="8" height="8" viewBox="0 0 8 8" fill="none" className={`transition-transform duration-150 ${modeOpen ? 'rotate-180' : ''}`}>
                <path d="M1 2.5L4 5.5L7 2.5" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round" />
              </svg>
            </button>

            {modeOpen && (
              <div className="absolute bottom-full mb-2 left-0 w-52 bg-bg-overlay border border-border-hover rounded-xl overflow-hidden shadow-xl shadow-black/60 z-[100] animate-fade-up">
                {MODE_OPTIONS.map(opt => (
                  <button
                    key={opt.value}
                    type="button"
                    onClick={() => { onModeChange(opt.value); setModeOpen(false) }}
                    className={`
                      w-full flex items-start gap-2.5 px-3 py-2.5 text-left
                      hover:bg-bg-raised transition-colors duration-100
                      ${opt.value === mode ? 'bg-bg-raised' : ''}
                    `}
                  >
                    <span className={`mt-0.5 flex-shrink-0 ${opt.value === mode ? 'text-accent' : 'text-ink-disabled'}`}>
                      {MODE_ICON[opt.value]}
                    </span>
                    <div className="space-y-0.5">
                      <p className={`text-xs font-medium ${opt.value === mode ? 'text-ink' : 'text-ink-muted'}`}>
                        {opt.label}
                      </p>
                      <p className="text-[11px] text-ink-dim leading-snug">{opt.hint}</p>
                    </div>
                    {opt.value === mode && (
                      <svg width="8" height="8" viewBox="0 0 8 8" fill="none" className="ml-auto mt-1 flex-shrink-0 text-accent">
                        <path d="M1 4L3.5 6.5L7 2" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" strokeLinejoin="round" />
                      </svg>
                    )}
                  </button>
                ))}
              </div>
            )}
          </div>

          <textarea
            ref={ref}
            value={value}
            onChange={e => setValue(e.target.value)}
            onKeyDown={handleKeyDown}
            disabled={disabled || sendingAttachments}
            placeholder={
              hasAttachments
                ? 'Add a message about these files…'
                : mode === 'research'
                  ? 'What do you want to research?'
                  : mode === 'chat'
                    ? 'Ask anything…'
                    : 'Ask a question or start a research task…'
            }
            rows={1}
            className={`
              flex-1 bg-transparent text-sm text-ink placeholder-ink-dim
              focus:outline-none resize-none leading-relaxed
              ${disabled ? 'opacity-40 cursor-not-allowed' : ''}
            `}
            style={{ minHeight: '24px', maxHeight: '160px' }}
          />

          {onUpload && (
            <>
              <input
                ref={fileRef}
                type="file"
                multiple
                accept=".pdf,.docx,.doc,.xlsx,.xls,.csv,.txt,.md"
                className="hidden"
                onChange={handleFileChange}
              />
              <button
                type="button"
                onClick={() => fileRef.current?.click()}
                disabled={disabled || sendingAttachments}
                title="Upload document"
                className={`
                  flex-shrink-0 w-8 h-8 rounded-lg flex items-center justify-center mb-0.5
                  border border-transparent transition-all duration-150
                  ${disabled
                    ? 'opacity-30 cursor-not-allowed text-ink-disabled'
                    : 'text-ink-dim hover:text-ink-muted hover:bg-bg-overlay hover:border-border-subtle'
                  }
                `}
              >
                <svg width="14" height="14" viewBox="0 0 14 14" fill="none">
                  <path d="M7 2.5v9M4 5.5L7 2.5 10 5.5" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" strokeLinejoin="round" />
                  <path d="M2.5 11.5h9" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" />
                </svg>
              </button>
            </>
          )}

          <button
            type="button"
          onClick={submit}
          disabled={disabled || sendingAttachments || !value.trim()}
            className={`
              flex-shrink-0 w-8 h-8 rounded-lg flex items-center justify-center
              transition-all duration-150 mb-0.5
              ${value.trim() && !disabled
                ? 'bg-accent hover:opacity-90'
                : 'bg-surface-2 opacity-40 cursor-not-allowed'
              }
            `}
          >
            <svg width="12" height="12" viewBox="0 0 12 12" fill="none">
              <path d="M6 2.5v7M3.5 7.5L6 10l2.5-2.5" stroke="white" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
          </button>
        </div>
      </div>

      <p className="mt-2 text-center text-[11px] text-ink-disabled">
        Enter to submit · Shift+Enter for new line
      </p>
    </div>
  )
}
