import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { fileURLToPath, URL } from 'node:url'

export default defineConfig({
  plugins: [vue()],
  resolve: { alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) } },
  server: {
    port: Number(process.env.ROBOTCARE_E2E_FRONTEND_PORT || 5173),
    proxy: { '/api': { target: process.env.ROBOTCARE_DEV_API_TARGET || 'http://localhost:8000', changeOrigin: true } },
  },
})
