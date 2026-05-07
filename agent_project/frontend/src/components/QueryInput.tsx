import { useState, useRef, useEffect } from 'react'
import type { Mode } from '../types'

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
}

export function QueryInput({ onSubmit, onUpload, disabled, autoFocus, mode, onModeChange }: Props) {
  const [value, setValue] = useState('')
  const [modeOpen, setModeOpen] = useState(false)
  const ref = useRef<HTMLTextAreaElement>(null)
  const modeRef = useRef<HTMLDivElement>(null)
  const fileRef = useRef<HTMLInputElement>(null)

  useEffect(() => {
    if (autoFocus) ref.current?.focus()
  }, [autoFocus])

  // Auto-resize textarea
  useEffect(() => {
    const el = ref.current
    if (!el) return
    el.style.height = 'auto'
    el.style.height = `${Math.min(el.scrollHeight, 160)}px`
  }, [value])

  // Close mode dropdown on outside click
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
    if (q && !disabled) {
      onSubmit(q, mode)
      setValue('')
    }
  }

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      submit()
    }
  }

  const handleFileChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0]
    if (file && onUpload) {
      onUpload(file)
      e.target.value = ''
    }
  }

  const currentMode = MODE_OPTIONS.find(o => o.value === mode) ?? MODE_OPTIONS[0]

  return (
    <div className="relative group">
      <div
        className={`
          flex items-end gap-3 rounded-xl border bg-[#111114]
          ${disabled ? 'border-[#1e1e1e]' : 'border-[#2a2a2a] focus-within:border-[#3a3a3a]'}
          transition-colors duration-200 px-4 py-3
        `}
      >
        {/* Mode selector */}
        <div ref={modeRef} className="relative flex-shrink-0 mb-0.5">
          <button
            type="button"
            onClick={() => !disabled && setModeOpen(v => !v)}
            disabled={disabled}
            className={`
              flex items-center gap-1.5 px-2 py-1 rounded-md text-[11px] font-medium
              border transition-colors duration-150
              ${disabled
                ? 'border-[#1e1e1e] text-zinc-700 cursor-not-allowed'
                : mode === 'auto'
                  ? 'border-[#2a2a2a] text-zinc-500 hover:text-zinc-400 hover:border-[#3a3a3a]'
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
            <div className="absolute bottom-full mb-2 left-0 w-52 bg-[#0d0d0d] border border-[#2a2a2a] rounded-xl overflow-hidden shadow-xl shadow-black/60 z-50 animate-fade-up">
              {MODE_OPTIONS.map(opt => (
                <button
                  key={opt.value}
                  type="button"
                  onClick={() => { onModeChange(opt.value); setModeOpen(false) }}
                  className={`
                    w-full flex items-start gap-2.5 px-3 py-2.5 text-left
                    hover:bg-[#111116] transition-colors duration-100
                    ${opt.value === mode ? 'bg-[#111116]' : ''}
                  `}
                >
                  <span className={`mt-0.5 flex-shrink-0 ${opt.value === mode ? 'text-indigo-400' : 'text-zinc-600'}`}>
                    {MODE_ICON[opt.value]}
                  </span>
                  <div className="space-y-0.5">
                    <p className={`text-xs font-medium ${opt.value === mode ? 'text-zinc-200' : 'text-zinc-400'}`}>
                      {opt.label}
                    </p>
                    <p className="text-[11px] text-zinc-600 leading-snug">{opt.hint}</p>
                  </div>
                  {opt.value === mode && (
                    <svg width="8" height="8" viewBox="0 0 8 8" fill="none" className="ml-auto mt-1 flex-shrink-0 text-indigo-400">
                      <path d="M1 4L3.5 6.5L7 2" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" strokeLinejoin="round" />
                    </svg>
                  )}
                </button>
              ))}
            </div>
          )}
        </div>

        {/* Textarea */}
        <textarea
          ref={ref}
          value={value}
          onChange={e => setValue(e.target.value)}
          onKeyDown={handleKeyDown}
          disabled={disabled}
          placeholder={
            mode === 'research'
              ? 'What do you want to research?'
              : mode === 'chat'
                ? 'Ask anything…'
                : 'Ask a question or start a research task…'
          }
          rows={1}
          className={`
            flex-1 bg-transparent text-sm text-zinc-200 placeholder-zinc-600
            focus:outline-none resize-none leading-relaxed
            ${disabled ? 'opacity-40 cursor-not-allowed' : ''}
          `}
          style={{ minHeight: '24px', maxHeight: '160px' }}
        />

        {/* Upload button */}
        {onUpload && (
          <>
            <input
              ref={fileRef}
              type="file"
              accept=".pdf,.docx,.doc,.xlsx,.xls,.csv,.txt,.md"
              className="hidden"
              onChange={handleFileChange}
            />
            <button
              type="button"
              onClick={() => fileRef.current?.click()}
              disabled={disabled}
              title="Upload document"
              className={`
                flex-shrink-0 w-7 h-7 rounded-lg flex items-center justify-center mb-0.5
                transition-all duration-150
                ${disabled
                  ? 'opacity-30 cursor-not-allowed text-zinc-700'
                  : 'text-zinc-500 hover:text-zinc-300 hover:bg-[#1e1e1e]'
                }
              `}
            >
              <svg width="13" height="13" viewBox="0 0 13 13" fill="none">
                <path d="M2 9.5V11h9V9.5" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" strokeLinejoin="round" />
                <path d="M6.5 2v7M4 4.5L6.5 2 9 4.5" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" strokeLinejoin="round" />
              </svg>
            </button>
          </>
        )}

        {/* Submit button */}
        <button
          onClick={submit}
          disabled={disabled || !value.trim()}
          className={`
            flex-shrink-0 w-7 h-7 rounded-lg flex items-center justify-center
            transition-all duration-150 mb-0.5
            ${value.trim() && !disabled
              ? 'bg-indigo-600 hover:bg-indigo-500 opacity-100'
              : 'bg-[#1e1e1e] opacity-40 cursor-not-allowed'
            }
          `}
        >
          <svg width="11" height="11" viewBox="0 0 11 11" fill="none">
            <path
              d="M5.5 1.5L9.5 5.5L5.5 9.5M1.5 5.5H9.5"
              stroke="white"
              strokeWidth="1.5"
              strokeLinecap="round"
              strokeLinejoin="round"
            />
          </svg>
        </button>
      </div>

      <p className="mt-2 text-center text-[11px] text-zinc-700">
        Enter to submit · Shift+Enter for new line
      </p>
    </div>
  )
}
