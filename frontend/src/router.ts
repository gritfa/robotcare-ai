import { createRouter, createWebHistory } from 'vue-router'
import { useAuthStore } from './stores'
import AppLayout from './layouts/AppLayout.vue'
import AdminLayout from './layouts/AdminLayout.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/login', component: () => import('./views/LoginView.vue'), meta: { public: true, guest: true } },
    { path: '/register', component: () => import('./views/RegisterView.vue'), meta: { public: true, guest: true } },
    {
      path: '/', component: AppLayout,
      children: [
        { path: '', name: 'dashboard', component: () => import('./views/DashboardView.vue') },
        { path: 'devices', name: 'devices', component: () => import('./views/DevicesView.vue') },
        { path: 'guides', name: 'guides', component: () => import('./views/GuidesView.vue') },
        { path: 'diagnostics/new', name: 'diagnostic-new', component: () => import('./views/DiagnosticStartView.vue') },
        { path: 'diagnostics/:id', name: 'diagnostic-session', component: () => import('./views/DiagnosticSessionView.vue') },
        { path: 'history', name: 'history', component: () => import('./views/HistoryView.vue') },
        { path: 'reports/:id', name: 'report', component: () => import('./views/ReportView.vue') },
      ],
    },
    { path: '/admin', component: AdminLayout, meta: { admin: true }, children: [{ path: '', component: () => import('./views/AdminView.vue') }] },
    { path: '/:pathMatch(.*)*', redirect: '/' },
  ],
})

router.beforeEach(async (to) => {
  const auth = useAuthStore()
  if (!to.meta.public && !auth.isAuthenticated) return { path: '/login', query: { redirect: to.fullPath } }
  if (auth.isAuthenticated && !auth.profileLoaded) {
    try {
      await auth.loadMe()
    } catch {
      if (!auth.isAuthenticated) return { path: '/login', query: { redirect: to.fullPath } }
    }
  }
  if (to.meta.guest && auth.isAuthenticated) return '/'
  if (to.meta.admin && !auth.isAdmin) return '/'
})
export default router
