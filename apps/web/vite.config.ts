import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// Backend address: defaults to 127.0.0.1:8000 (dev.sh convention); override with
// VITE_API_TARGET so the frontend proxy can point at a backend on a non-default port.
const apiTarget = process.env.VITE_API_TARGET ?? 'http://127.0.0.1:8000'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  test: {
    // Unit tests only — e2e/ is a separate Playwright package whose *.spec.ts
    // files must never run under vitest.
    include: ['src/**/*.test.{ts,tsx}'],
  },
  server: {
    port: 5273,
    host: '0.0.0.0',
    proxy: {
      '/api': {
        target: apiTarget,
        changeOrigin: false,
      },
    },
  },
})
