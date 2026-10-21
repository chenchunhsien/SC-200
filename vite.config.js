import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// base: './' 讓打包後的資源使用相對路徑，部署到 GitHub Pages 子路徑時也能正常讀取
export default defineConfig({
  base: './',
  plugins: [react(), tailwindcss()],
})
