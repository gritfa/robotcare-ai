import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { TOKEN_KEY, authApi, diagnosticApi } from './api'
import type { Diagnostic, DiagnosticStep, User } from './types'
const USER_KEY = 'robotcare_user'

export const useAuthStore = defineStore('auth', () => {
  const token = ref(localStorage.getItem(TOKEN_KEY) || '')
  const savedUser = localStorage.getItem(USER_KEY)
  const user = ref<User | null>(savedUser ? JSON.parse(savedUser) as User : null)
  const isAuthenticated = computed(() => Boolean(token.value))
  const isAdmin = computed(() => user.value?.role === 'admin')
  function applyAuth(result: { access_token: string; user?: User }) {
    token.value = result.access_token; user.value = result.user || null
    localStorage.setItem(TOKEN_KEY, result.access_token)
    if (user.value) localStorage.setItem(USER_KEY, JSON.stringify(user.value))
  }
  async function login(email: string, password: string) { applyAuth(await authApi.login(email, password)); if (!user.value) await loadMe() }
  async function register(body: { email: string; password: string; name: string }) { const result = await authApi.register(body); if (result.access_token) applyAuth(result) }
  async function loadMe() { if (!token.value) return; try { user.value = await authApi.me() } catch { logout() } }
  function logout() { token.value = ''; user.value = null; localStorage.removeItem(TOKEN_KEY); localStorage.removeItem(USER_KEY) }
  return { token, user, isAuthenticated, isAdmin, login, register, loadMe, logout }
})

export const useDiagnosticStore = defineStore('diagnostics', () => {
  const active = ref<Diagnostic | null>(null)
  const currentStep = ref<DiagnosticStep | null>(null)
  const loading = ref(false)
  async function load(id: string) {
    loading.value = true
    currentStep.value = null
    try {
      const diagnostic = await diagnosticApi.get(id)
      active.value = diagnostic
      currentStep.value = diagnostic.status === 'in_progress'
        ? await diagnosticApi.currentStep(id)
        : null
    } finally {
      loading.value = false
    }
  }
  async function submitFeedback(resolved: boolean) {
    if (!active.value || !currentStep.value) throw new Error('当前诊断步骤不存在，请刷新页面后重试')
    loading.value = true
    try {
      const result = await diagnosticApi.feedback(active.value.id, currentStep.value.id, resolved)
      active.value = result.diagnostic
      currentStep.value = result.current_step
    } finally { loading.value = false }
  }
  function clear() { active.value = null; currentStep.value = null }
  return { active, currentStep, loading, load, submitFeedback, clear }
})
