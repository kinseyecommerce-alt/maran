/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        desk: {
          bg:      '#0a0c10',
          panel:   '#11141a',
          elevated:'#161a22',
          border:  '#1e2430',
          muted:   '#2a3140',
          label:   '#6b7280',
          text:    '#c8cdd5',
          bright:  '#e8eaed',
        },
        buy:  { DEFAULT: '#22c55e', light: '#14532d', dark: '#16a34a' },
        sell: { DEFAULT: '#ef4444', light: '#7f1d1d', dark: '#dc2626' },
      },
      fontFamily: {
        sans: ['Inter', 'system-ui', 'sans-serif'],
        mono: ['JetBrains Mono', 'IBM Plex Mono', 'ui-monospace', 'monospace'],
      },
      fontSize: {
        '2xs': ['0.625rem', { lineHeight: '0.875rem' }],
      },
      boxShadow: {
        desk: '0 0 0 1px rgba(30,36,48,0.8)',
      },
    },
  },
  plugins: [],
}
