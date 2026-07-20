import { createApp } from 'vue'
import { createPinia } from 'pinia'
import ElementPlus from 'element-plus'
import 'element-plus/dist/index.css'
import App from './App.vue'
import router from './router'
import { setAuthenticationLostHandler } from './authSession'
import './styles.css'

setAuthenticationLostHandler(async () => {
  const currentPath = router.currentRoute.value.fullPath
  if (router.currentRoute.value.path === '/login') return
  await router.replace({ path: '/login', query: { redirect: currentPath } })
})

createApp(App).use(createPinia()).use(router).use(ElementPlus).mount('#app')
