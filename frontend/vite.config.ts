import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    // Proxy in development so the frontend calls a same-origin /api path in
    // both dev and production. Without it the API base URL becomes an
    // environment-dependent constant, which is a small thing that reliably
    // breaks at deploy time.
    proxy: {
      '/api': { target: 'http://127.0.0.1:8000', changeOrigin: true },
    },
  },
  build: { outDir: 'dist', sourcemap: true },
})
