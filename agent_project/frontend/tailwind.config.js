/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  darkMode: 'class',
  theme: {
    extend: {
      fontFamily: {
        sans: ['Inter', '-apple-system', 'BlinkMacSystemFont', 'sans-serif'],
      },
      // ── "Slate Terminal" design tokens ──────────────────────────────────
      // Semantic color scale — never use raw hex in components.
      // All colors reference CSS custom properties defined in index.css.
      // Dark = :root defaults, Light = html.light overrides.
      colors: {
        // ── Background scale (darkest → lighter overlays) ──────────────
        bg: {
          DEFAULT: 'var(--color-bg)',
          raised: 'var(--color-bg-raised)',
          overlay: 'var(--color-bg-overlay)',
          input: 'var(--color-bg-input)',
        },
        // ── Surface scale (mid layers) ─────────────────────────────────
        surface: {
          DEFAULT: 'var(--color-surface)',
          2: 'var(--color-surface-2)',
          3: 'var(--color-surface-3)',
        },
        // ── Border scale (subtle → prominent) ──────────────────────────
        border: {
          DEFAULT: 'var(--color-border)',
          subtle: 'var(--color-border-subtle)',
          hover: 'var(--color-border-hover)',
          accent: 'var(--color-border-accent)',
        },
        // Alias used by KG components (`border-edge`, `bg-edge`, etc.)
        edge: {
          DEFAULT: 'var(--color-border)',
          2: 'var(--color-border-hover)',
        },
        // ── Ink scale (text) ───────────────────────────────────────────
        ink: {
          DEFAULT: 'var(--color-ink)',
          muted: 'var(--color-ink-muted)',
          dim: 'var(--color-ink-dim)',
          disabled: 'var(--color-ink-disabled)',
        },
        // ── Accent (blue — interaction only) ───────────────────────────
        accent: {
          DEFAULT: 'var(--color-accent)',
          soft: 'var(--color-accent-soft)',
          ring: 'var(--color-accent-ring)',
          muted: 'var(--color-accent-muted)',
        },
        // ── Semantic state colors ──────────────────────────────────────
        success: {
          DEFAULT: 'var(--color-success)',
          soft: 'var(--color-success-soft)',
        },
        danger: {
          DEFAULT: 'var(--color-danger)',
          soft: 'var(--color-danger-soft)',
        },
        warn: {
          DEFAULT: 'var(--color-warn)',
          soft: 'var(--color-warn-soft)',
        },
        // ── Legacy flat aliases (for backward compat) ──────────────────
        up: '#10b981',
        down: '#ef4444',
      },
      animation: {
        'pulse-ring': 'pulse-ring 1.8s ease-in-out infinite',
        'fade-up': 'fade-up 0.25s ease-out',
        'slide-in': 'slide-in 0.2s ease-out',
        'blink': 'blink 1s step-end infinite',
        'shimmer': 'shimmer 1.5s infinite',
        'settle': 'settle 0.4s ease-out',
        'attachment-in': 'attachment-in 0.28s ease-out both',
        'attachment-exit': 'attachment-exit 0.22s ease-in both',
        'message-send': 'message-send 0.32s cubic-bezier(0.22, 1, 0.36, 1) both',
        'pulse-subtle': 'pulse-subtle 1.4s ease-in-out infinite',
        'flash-dot': 'flash-dot 0.25s ease-out',
        'step-reveal': 'step-reveal 0.22s ease-out both',
      },
      keyframes: {
        'settle': {
          '0%':   { transform: 'scale(1)' },
          '45%':  { transform: 'scale(1.9)' },
          '100%': { transform: 'scale(1)' },
        },
        'pulse-ring': {
          '0%, 100%': { boxShadow: '0 0 0 0 rgba(99, 102, 241, 0.5)' },
          '50%': { boxShadow: '0 0 0 5px rgba(99, 102, 241, 0)' },
        },
        'fade-up': {
          '0%': { opacity: '0', transform: 'translateY(6px)' },
          '100%': { opacity: '1', transform: 'translateY(0)' },
        },
        'slide-in': {
          '0%': { opacity: '0', transform: 'translateX(-6px)' },
          '100%': { opacity: '1', transform: 'translateX(0)' },
        },
        'blink': {
          '0%, 100%': { opacity: '1' },
          '50%': { opacity: '0' },
        },
        'shimmer': {
          '0%': { backgroundPosition: '-200% 0' },
          '100%': { backgroundPosition: '200% 0' },
        },
        'attachment-in': {
          '0%': { opacity: '0', transform: 'translateY(10px) scale(0.94)' },
          '100%': { opacity: '1', transform: 'translateY(0) scale(1)' },
        },
        'attachment-exit': {
          '0%': { opacity: '1', transform: 'translateY(0) scale(1)' },
          '100%': { opacity: '0', transform: 'translateY(-14px) scale(0.92)' },
        },
        'message-send': {
          '0%': { opacity: '0', transform: 'translateY(18px)' },
          '100%': { opacity: '1', transform: 'translateY(0)' },
        },
        'pulse-subtle': {
          '0%, 100%': { opacity: '1' },
          '50%': { opacity: '0.72' },
        },
        'flash-dot': {
          '0%':   { transform: 'scale(1)', filter: 'brightness(1)' },
          '40%':  { transform: 'scale(1.4)', filter: 'brightness(1.5)' },
          '100%': { transform: 'scale(1)', filter: 'brightness(1)' },
        },
        'row-flash': {
          '0%':   { backgroundColor: 'var(--color-accent-soft)' },
          '100%': { backgroundColor: 'transparent' },
        },
        'step-reveal': {
          '0%': { opacity: '0', transform: 'translateY(8px)' },
          '100%': { opacity: '1', transform: 'translateY(0)' },
        },
      },
    },
  },
  plugins: [],
}