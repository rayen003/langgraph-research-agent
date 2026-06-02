import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

const backendPort = process.env.BACKEND_PORT || '8080'
const frontendPort = Number(process.env.FRONTEND_PORT || 5174)
const backendUrl = `http://127.0.0.1:${backendPort}`

const apiProxy = {
  '/runs': backendUrl,
  '/artifacts': backendUrl,
  '/documents': backendUrl,
  '/health': backendUrl,
  '/kg': backendUrl,
  '/sources': backendUrl,
  '/sessions': backendUrl,
}

export default defineConfig({
  plugins: [react()],
  server: {
    port: frontendPort,
    strictPort: false,
    proxy: apiProxy,
  },
  preview: {
    port: frontendPort,
    strictPort: false,
    proxy: apiProxy,
  },
})
