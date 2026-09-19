/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{js,jsx}'],
  theme: {
    extend: {
      colors: {
        ink: {
          950: '#080b12',
          900: '#0c111b',
          850: '#111827',
          800: '#161d2b',
          700: '#1f2937',
          600: '#2b3547',
        },
        accent: {
          DEFAULT: '#38bdf8',
          soft: '#7dd3fc',
          deep: '#0ea5e9',
        },
      },
      fontFamily: {
        sans: ['Inter', 'system-ui', '-apple-system', 'Segoe UI', 'Roboto', 'sans-serif'],
        mono: ['ui-monospace', 'SFMono-Regular', 'JetBrains Mono', 'Menlo', 'monospace'],
      },
      boxShadow: {
        card: '0 1px 0 0 rgba(255,255,255,0.04) inset, 0 12px 32px -18px rgba(0,0,0,0.9)',
        glow: '0 0 0 1px rgba(56,189,248,0.35), 0 0 28px -6px rgba(56,189,248,0.45)',
      },
      keyframes: {
        'fade-up': {
          from: { opacity: 0, transform: 'translateY(6px)' },
          to: { opacity: 1, transform: 'translateY(0)' },
        },
        pulseRing: {
          '0%': { boxShadow: '0 0 0 0 rgba(52,211,153,0.5)' },
          '70%': { boxShadow: '0 0 0 8px rgba(52,211,153,0)' },
          '100%': { boxShadow: '0 0 0 0 rgba(52,211,153,0)' },
        },
      },
      animation: {
        'fade-up': 'fade-up 0.25s ease-out both',
        'pulse-ring': 'pulseRing 2.4s ease-out infinite',
      },
    },
  },
  plugins: [],
}
