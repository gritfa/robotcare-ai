import { beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import type { Diagnostic, DiagnosticStep, DiagnosticStatus } from './types'

const apiMocks = vi.hoisted(() => ({
  get: vi.fn(),
  currentStep: vi.fn(),
  feedback: vi.fn(),
}))
const authMocks = vi.hoisted(() => ({
  login: vi.fn(),
  register: vi.fn(),
  me: vi.fn(),
  logout: vi.fn(),
}))

vi.mock('./api', () => ({
  TOKEN_KEY: 'robotcare_access_token',
  authApi: authMocks,
  diagnosticApi: apiMocks,
}))

import { useAuthStore, useDiagnosticStore } from './stores'

class MemoryStorage implements Storage {
  private values = new Map<string, string>()
  get length() { return this.values.size }
  clear() { this.values.clear() }
  getItem(key: string) { return this.values.get(key) ?? null }
  key(index: number) { return [...this.values.keys()][index] ?? null }
  removeItem(key: string) { this.values.delete(key) }
  setItem(key: string, value: string) { this.values.set(key, String(value)) }
}

beforeAll(() => {
  Object.defineProperty(globalThis, 'localStorage', { configurable: true, value: new MemoryStorage() })
})

const step: DiagnosticStep = {
  id: 31,
  position: 1,
  title: '检查主刷',
  instruction: '关闭电源后清理缠绕物。',
  source_label: 'JH69U1 说明书第 15 页',
}

function diagnostic(status: DiagnosticStatus): Diagnostic {
  return {
    id: 7,
    device_id: 2,
    issue_description: '主刷无法转动',
    status,
  }
}

describe('diagnostic store resume behavior', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    localStorage.clear()
    vi.clearAllMocks()
  })

  it('loads the detail first and only requests a current step for an in-progress diagnostic', async () => {
    apiMocks.get.mockResolvedValue(diagnostic('in_progress'))
    apiMocks.currentStep.mockResolvedValue(step)

    const store = useDiagnosticStore()
    await store.load('7')

    expect(apiMocks.get).toHaveBeenCalledWith('7')
    expect(apiMocks.currentStep).toHaveBeenCalledWith('7')
    expect(apiMocks.get.mock.invocationCallOrder[0]).toBeLessThan(apiMocks.currentStep.mock.invocationCallOrder[0])
    expect(store.currentStep).toEqual(step)
  })

  it.each<DiagnosticStatus>(['resolved', 'unresolved', 'report_ready'])('does not request a current step when reopening a %s diagnostic', async (status) => {
    apiMocks.get.mockResolvedValue(diagnostic(status))

    const store = useDiagnosticStore()
    await store.load('7')

    expect(apiMocks.currentStep).not.toHaveBeenCalled()
    expect(store.active?.status).toBe(status)
    expect(store.currentStep).toBeNull()
  })

  it('clears a stale step when a refreshed diagnostic has already ended', async () => {
    apiMocks.get
      .mockResolvedValueOnce(diagnostic('in_progress'))
      .mockResolvedValueOnce(diagnostic('resolved'))
    apiMocks.currentStep.mockResolvedValue(step)

    const store = useDiagnosticStore()
    await store.load('7')
    await store.load('7')

    expect(apiMocks.currentStep).toHaveBeenCalledTimes(1)
    expect(store.active?.status).toBe('resolved')
    expect(store.currentStep).toBeNull()
  })

  it('submits feedback with the loaded step id and applies the returned terminal state', async () => {
    apiMocks.get.mockResolvedValue(diagnostic('in_progress'))
    apiMocks.currentStep.mockResolvedValue(step)
    apiMocks.feedback.mockResolvedValue({ diagnostic: diagnostic('resolved'), current_step: null })

    const store = useDiagnosticStore()
    await store.load('7')
    await store.submitFeedback(true)

    expect(apiMocks.feedback).toHaveBeenCalledWith(7, 31, true)
    expect(store.active?.status).toBe('resolved')
    expect(store.currentStep).toBeNull()
  })
})

describe('auth store session lifecycle', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    localStorage.clear()
    vi.clearAllMocks()
  })

  it('calls the backend logout endpoint before clearing local state', async () => {
    localStorage.setItem('robotcare_access_token', 'access-token')
    localStorage.setItem('robotcare_user', JSON.stringify({ id: 1, email: 'user@example.com' }))
    authMocks.logout.mockResolvedValue(undefined)
    const store = useAuthStore()

    await store.logout()

    expect(authMocks.logout).toHaveBeenCalledTimes(1)
    expect(store.token).toBe('')
    expect(store.user).toBeNull()
    expect(localStorage.getItem('robotcare_access_token')).toBeNull()
  })

  it('passes an optional beta invite code to registration', async () => {
    authMocks.register.mockResolvedValue({
      access_token: 'registered-access',
      user: { id: 1, email: 'invite@example.com', role: 'user' },
    })
    const store = useAuthStore()

    await store.register({
      name: '测试用户',
      email: 'invite@example.com',
      password: 'StrongPass123',
      invite_code: 'beta-invite-code',
    })

    expect(authMocks.register).toHaveBeenCalledWith({
      name: '测试用户',
      email: 'invite@example.com',
      password: 'StrongPass123',
      invite_code: 'beta-invite-code',
    })
    expect(store.isAuthenticated).toBe(true)
  })

  it('reloads a persisted user profile instead of trusting stale local data', async () => {
    localStorage.setItem('robotcare_access_token', 'access-token')
    localStorage.setItem('robotcare_user', JSON.stringify({ id: 1, email: 'old@example.com', role: 'admin' }))
    authMocks.me.mockResolvedValue({ id: 1, email: 'current@example.com', role: 'user' })
    const store = useAuthStore()

    expect(store.profileLoaded).toBe(false)
    await store.loadMe()

    expect(authMocks.me).toHaveBeenCalledTimes(1)
    expect(store.profileLoaded).toBe(true)
    expect(store.user?.email).toBe('current@example.com')
    expect(store.isAdmin).toBe(false)
    expect(localStorage.getItem('robotcare_user')).toContain('current@example.com')
  })

  it('drives admin access from backend capabilities, not from the role string', () => {
    // 2026-08-06 体检 #9：前端此前写死 role === 'admin'，于是后端四级角色
    // 全线放行、测试全绿，viewer/operator 登录后侧栏却没有入口、
    // 手敲 /admin 还被路由守卫弹回——"给运营看一眼看板"根本达不成。
    const store = useAuthStore()

    store.user = {
      id: 1, email: 'viewer@example.com', role: 'viewer',
      capabilities: ['read_operations'],
    }
    expect(store.canViewOperations).toBe(true)   // 能进后台
    expect(store.canManageKnowledge).toBe(false) // 但不能改知识库
    expect(store.isAdmin).toBe(false)            // 更不能删/回滚

    store.user = {
      id: 2, email: 'operator@example.com', role: 'operator',
      capabilities: ['read_operations', 'manage_knowledge'],
    }
    expect(store.canViewOperations).toBe(true)
    expect(store.canManageKnowledge).toBe(true)
    expect(store.isAdmin).toBe(false)

    store.user = {
      id: 3, email: 'admin@example.com', role: 'admin',
      capabilities: ['administer', 'manage_knowledge', 'read_operations'],
    }
    expect(store.isAdmin).toBe(true)
  })

  it('grants nothing when the backend sends no capabilities', () => {
    const store = useAuthStore()
    store.user = { id: 4, email: 'plain@example.com', role: 'user' }

    expect(store.capabilities).toEqual([])
    expect(store.canViewOperations).toBe(false)
    expect(store.isAdmin).toBe(false)
  })

  it('still clears local state when the backend logout request fails', async () => {
    localStorage.setItem('robotcare_access_token', 'access-token')
    authMocks.logout.mockRejectedValue(new Error('network unavailable'))
    const store = useAuthStore()

    await expect(store.logout()).rejects.toThrow('network unavailable')

    expect(store.isAuthenticated).toBe(false)
    expect(localStorage.getItem('robotcare_access_token')).toBeNull()
  })
})
