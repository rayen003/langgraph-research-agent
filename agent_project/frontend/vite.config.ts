import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5174,
    proxy: {
      '/runs': 'http://localhost:8080',
      '/artifacts': 'http://localhost:8080',
      '/documents': 'http://localhost:8080',
      '/health': 'http://localhost:8080',
    },
  },
})
