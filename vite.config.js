import { readdirSync, rmSync } from 'node:fs'
import { join, resolve } from 'node:path'
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// 與 .gitignore 對應：本機的真實題庫只透過「匯入個人題庫」使用，不能被複製進 dist
const PRIVATE_BANK = /^(sc200\.json|sc200\.ai\.json|.*\.real\.json|.*\.drafts\.json)$/

function excludePrivateBanks() {
  let dataDir
  return {
    name: 'exclude-private-banks',
    apply: 'build',
    configResolved(config) {
      dataDir = resolve(config.root, config.build.outDir, 'data') // outDir 可能是絕對路徑，不能用 join
    },
    closeBundle() {
      let files
      try {
        files = readdirSync(dataDir)
      } catch (e) {
        if (e.code === 'ENOENT') return // 沒有 data 資料夾時略過
        throw e // 其他錯誤不可吞掉，否則私人題庫可能留在輸出中
      }
      for (const f of files) {
        if (PRIVATE_BANK.test(f)) rmSync(join(dataDir, f))
      }
    },
  }
}

// base: './' 讓打包後的資源使用相對路徑，部署到 GitHub Pages 子路徑時也能正常讀取
export default defineConfig({
  base: './',
  plugins: [react(), tailwindcss(), excludePrivateBanks()],
})
