import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { authApi, diagnosticApi } from './api'
import { applyAuthResult, bindAuthState, clearAuthState, persistUser, readStoredAccessToken, readStoredUser } from './authSession'
import type { Diagnostic, DiagnosticStep, User } from './types'

export const useAuthStore = defineStore('auth', () => {
  const token = ref(readStoredAccessToken())
  const user = ref<User | null>(readStoredUser())
  const profileLoaded = ref(false)
  const isAuthenticated = computed(() => Boolean(token.value))
  const isAdmin = computed(() => user.value?.role === 'admin')

  bindAuthState({
    getAccessToken: () => token.value,
    applyAuth: (result) => {
      token.value = result.access_token
      user.value = result.user || null
      profileLoaded.value = Boolean(result.user)
    },
    clearAuth: () => {
      token.value = ''
      user.value = null
      profileLoaded.value = false
    },
  })

  async function login(email: string, password: string) {
    applyAuthResult(await authApi.login(email, password))
    if (!profileLoaded.value) await loadMe()
  }
  async function register(body: { email: string; password: string; name: string; invite_code?: string }) {
    const result = await authApi.register(body)
    if (result.access_token) {
      applyAuthResult(result)
      if (!profileLoaded.value) await loadMe()
    }
  }
  async function loadMe() {
    if (!token.value) return
    const loadedUser = await authApi.me()
    user.value = loadedUser
    profileLoaded.value = true
    persistUser(loadedUser)
  }
  async function logout() {
    try {
      await authApi.logout()
    } finally {
      clearAuthState()
    }
  }
  return { token, user, profileLoaded, isAuthenticated, isAdmin, login, register, loadMe, logout }
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
