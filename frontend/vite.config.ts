import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import AutoImport from 'unplugin-auto-import/vite'
import Components from 'unplugin-vue-components/vite'
import { ElementPlusResolver } from 'unplugin-vue-components/resolvers'
import { fileURLToPath, URL } from 'node:url'

export default defineConfig({
  plugins: [
    vue(),
    // 按需引入 element-plus：只打包实际用到的组件与样式，
    // 替代 main.ts 里的全量 app.use(ElementPlus)
    AutoImport({ resolvers: [ElementPlusResolver()], dts: 'src/auto-imports.d.ts' }),
    Components({ resolvers: [ElementPlusResolver()], dts: 'src/components.d.ts' }),
  ],
  resolve: { alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) } },
  // 不要给 element-plus 配 manualChunks：手动分块会把被分块的模块当成 chunk 入口，
  // 保留其全部导出，等于关掉 tree-shaking（实测整包 937 kB）。交给 rollup 自动按
  // 组件拆分即可（实测主包 225 kB，其余按页面/组件懒加载）。
  server: {
    port: Number(process.env.ROBOTCARE_E2E_FRONTEND_PORT || 5173),
    proxy: { '/api': { target: process.env.ROBOTCARE_DEV_API_TARGET || 'http://localhost:8000', changeOrigin: true } },
  },
})
