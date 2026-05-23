/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      fontFamily: {
        sans: ['Inter', '-apple-system', 'BlinkMacSystemFont', 'sans-serif'],
      },
      animation: {
        'pulse-ring': 'pulse-ring 1.8s ease-in-out infinite',
        'fade-up': 'fade-up 0.25s ease-out',
        'slide-in': 'slide-in 0.2s ease-out',
        'blink': 'blink 1s step-end infinite',
        'shimmer': 'shimmer 1.5s infinite',
        'settle': 'settle 0.4s ease-out',
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
      },
    },
  },
  plugins: [],
}
