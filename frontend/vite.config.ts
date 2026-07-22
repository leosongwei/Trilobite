import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
  build: {
    outDir: '../src/trilobite/static',
    emptyOutDir: true,
  },
  server: {
    proxy: {
      '/api': 'http://localhost:2345',
    },
  },
})
