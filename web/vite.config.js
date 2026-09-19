import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  build: { outDir: 'dist', emptyOutDir: true, chunkSizeWarningLimit: 1200 },
  server: {
    host: true,
    // Для локальной разработки: npm run dev + бэкенд на роутере.
    proxy: { '/api': { target: 'http://192.168.10.1:8088', changeOrigin: true } },
  },
})
