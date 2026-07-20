import axios, { type AxiosError, type InternalAxiosRequestConfig } from 'axios'
import type { AdminAuditLog, AdminModel, AdminOverview, AdminSafetyBlock, AdminUnresolvedReport, Attachment, AuthResult, Device, Diagnostic, DiagnosticOption, DiagnosticStep, EntityId, KnowledgeModelStatus, KnowledgeSearchResult, ReportPdf, RobotModel, ServiceReport, User } from './types'
import { applyAuthResult, clearAuthState, getAccessToken, notifyAuthenticationLost } from './authSession'

export { TOKEN_KEY } from './authSession'
export const http = axios.create({ baseURL: '/api/v1', timeout: 20000, withCredentials: true })

type RetryableRequest = InternalAxiosRequestConfig & { _authRetry?: boolean }
const AUTH_ENDPOINTS_WITHOUT_REFRESH = ['/auth/login', '/auth/register', '/auth/refresh', '/auth/logout']
const AUTH_REFRESH_LOCK = 'robotcare-auth-refresh'
const CONCURRENT_REFRESH_RETRIES = 2
let refreshRequest: Promise<AuthResult> | null = null

type LockManagerLike = {
  request<T>(name: string, callback: () => Promise<T>): Promise<T>
}

type RefreshOutcome = {
  result: AuthResult
  applyResult: boolean
}

function isRefreshExcluded(url = '') {
  try {
    const pathname = new URL(url, 'http://robotcare.local').pathname
    return AUTH_ENDPOINTS_WITHOUT_REFRESH.some((path) => pathname.endsWith(path))
  } catch {
    return false
  }
}

function requestBearerToken(request: RetryableRequest) {
  const authorization = request.headers?.get('Authorization')
  return typeof authorization === 'string' && authorization.startsWith('Bearer ')
    ? authorization.slice('Bearer '.length)
    : ''
}

function concurrentRefreshConflict(error: unknown) {
  return axios.isAxiosError(error) && error.response?.status === 409
}

function concurrentRefreshDelay(error: unknown, attempt: number) {
  if (!axios.isAxiosError(error)) return 100 * (attempt + 1)
  const headers = error.response?.headers as
    | { get?: (name: string) => unknown; [name: string]: unknown }
    | undefined
  const rawValue = headers?.get?.('retry-after') ?? headers?.['retry-after']
  const seconds = Number(rawValue)
  if (Number.isFinite(seconds) && seconds >= 0) {
    return Math.max(50, seconds * 1000)
  }
  return 100 * (attempt + 1)
}

function accountDisabled(error: AxiosError) {
  const body = error.response?.data as { detail?: unknown } | undefined
  return error.response?.status === 403 && body?.detail === 'Account disabled'
}

function pause(milliseconds: number) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds))
}

async function requestRefreshedAuthentication(): Promise<AuthResult> {
  for (let attempt = 0; ; attempt += 1) {
    try {
      return payload<AuthResult>((await http.post('/auth/refresh')).data)
    } catch (error) {
      if (!concurrentRefreshConflict(error) || attempt >= CONCURRENT_REFRESH_RETRIES) throw error
      // Another tab may have rotated the shared HttpOnly cookie just before this
      // response arrived. Give the browser a moment to apply that Set-Cookie,
      // then retry with the current cookie rather than treating it as logout.
      await pause(concurrentRefreshDelay(error, attempt))
    }
  }
}

function browserLockManager(): LockManagerLike | null {
  if (typeof navigator === 'undefined') return null
  return (navigator as Navigator & { locks?: LockManagerLike }).locks || null
}

async function refreshSession(failedAccessToken = getAccessToken()) {
  if (!refreshRequest) {
    const refreshWithinLock = async (): Promise<RefreshOutcome> => {
      const currentAccessToken = getAccessToken()
      if (failedAccessToken && currentAccessToken && currentAccessToken !== failedAccessToken) {
        // A different tab already refreshed while this tab waited for the
        // same-origin Web Lock. Reuse the shared access token and do not rotate
        // the shared refresh cookie again.
        return { result: { access_token: currentAccessToken }, applyResult: false }
      }
      return { result: await requestRefreshedAuthentication(), applyResult: true }
    }
    const locks = browserLockManager()
    const pending = locks
      ? locks.request(AUTH_REFRESH_LOCK, refreshWithinLock)
      : refreshWithinLock()
    refreshRequest = pending
      .then(({ result, applyResult }) => {
        if (applyResult) applyAuthResult(result)
        return result
      })
      .catch(async (error) => {
        clearAuthState()
        await notifyAuthenticationLost()
        throw error
      })
      .finally(() => {
        refreshRequest = null
      })
  }
  return refreshRequest
}

http.interceptors.request.use((config) => {
  const token = getAccessToken()
  if (token) config.headers.Authorization = `Bearer ${token}`
  return config
})
http.interceptors.response.use(undefined, async (error: AxiosError) => {
  const request = error.config as RetryableRequest | undefined
  if (accountDisabled(error) && !isRefreshExcluded(request?.url)) {
    clearAuthState()
    await notifyAuthenticationLost()
    return Promise.reject(error)
  }
  if (error.response?.status !== 401 || !request || request._authRetry || isRefreshExcluded(request.url)) {
    return Promise.reject(error)
  }

  request._authRetry = true
  try {
    const result = await refreshSession(requestBearerToken(request))
    request.headers.Authorization = `Bearer ${result.access_token}`
    return await http.request(request)
  } catch {
    return Promise.reject(error)
  }
})

export function apiError(error: unknown, fallback = '请求失败，请稍后重试') {
  if (axios.isAxiosError(error)) {
    const data = error.response?.data as { detail?: string; message?: string } | undefined
    return data?.detail || data?.message || fallback
  }
  return error instanceof Error ? error.message : fallback
}
function payload<T>(value: unknown): T {
  const body = value as { data?: unknown }
  return (body && typeof body === 'object' && 'data' in body ? body.data : value) as T
}
function listPayload<T>(value: unknown): T[] {
  const body = payload<unknown>(value)
  if (Array.isArray(body)) return body as T[]
  if (body && typeof body === 'object' && Array.isArray((body as { items?: unknown }).items)) return (body as { items: T[] }).items
  return []
}

export const authApi = {
  register: async (body: { email: string; password: string; name?: string }) => payload<AuthResult>((await http.post('/auth/register', { email: body.email, password: body.password })).data),
  login: async (email: string, password: string) => {
    try {
      return payload<AuthResult>((await http.post('/auth/login', { email, password })).data)
    } catch (error) {
      if (!axios.isAxiosError(error) || error.response?.status !== 422) throw error
      const form = new URLSearchParams({ username: email, password })
      return payload<AuthResult>((await http.post('/auth/login', form, { headers: { 'Content-Type': 'application/x-www-form-urlencoded' } })).data)
    }
  },
  refresh: refreshSession,
  logout: async () => { await http.post('/auth/logout') },
  me: async () => payload<User>((await http.get('/auth/me')).data),
}
export const modelApi = {
  list: async () => listPayload<RobotModel>((await http.get('/models')).data),
  diagnosticOptions: async (modelId: EntityId) => listPayload<DiagnosticOption>((await http.get(`/models/${modelId}/diagnostic-options`)).data),
}
export const deviceApi = {
  list: async () => listPayload<Device>((await http.get('/devices')).data),
  create: async (body: { robot_model_id: EntityId; nickname: string; serial_number?: string }) => payload<Device>((await http.post('/devices', body)).data),
  update: async (id: EntityId, body: { nickname?: string; serial_number?: string }) => payload<Device>((await http.patch(`/devices/${id}`, body)).data),
  remove: async (id: EntityId) => { await http.delete(`/devices/${id}`) },
}
export const diagnosticApi = {
  list: async () => listPayload<Diagnostic>((await http.get('/diagnostics')).data),
  get: async (id: EntityId) => payload<Diagnostic>((await http.get(`/diagnostics/${id}`)).data),
  create: async (body: { device_id: EntityId; issue_category_code: string; issue_description: string; error_code?: string }) => payload<Diagnostic>((await http.post('/diagnostics', body)).data),
  currentStep: async (id: EntityId) => payload<DiagnosticStep | null>((await http.get(`/diagnostics/${id}/steps/current`)).data),
  feedback: async (id: EntityId, stepId: EntityId, resolved: boolean) => payload<{diagnostic:Diagnostic;current_step:DiagnosticStep|null}>((await http.post(`/diagnostics/${id}/feedback`, { step_id: stepId, outcome: resolved ? 'resolved' : 'not_resolved' })).data),
  attachments: async (id: EntityId) => listPayload<Attachment>((await http.get(`/diagnostics/${id}/attachments`)).data),
  uploadAttachment: async (id: EntityId, file: File) => {
    const form = new FormData()
    form.append('file', file, file.name)
    return payload<Attachment>((await http.post(`/diagnostics/${id}/attachments`, form)).data)
  },
  deleteAttachment: async (id: EntityId, attachmentId: EntityId) => {
    await http.delete(`/diagnostics/${id}/attachments/${attachmentId}`)
  },
  report: async (id: EntityId) => payload<ServiceReport>((await http.get(`/diagnostics/${id}/report`)).data),
  createReport: async (id: EntityId) => payload<ServiceReport>((await http.post(`/diagnostics/${id}/report`)).data),
  createPdfReport: async (id: EntityId) => payload<ReportPdf>((await http.post(`/diagnostics/${id}/report/pdf`)).data),
  downloadPdfReport: async (id: EntityId) => (await http.get<Blob>(`/diagnostics/${id}/report/pdf`, { responseType: 'blob' })).data,
}

export const knowledgeApi = {
  search: async (body: { robot_model_id: number; query: string; top_k?: number }) => {
    const response = await http.post('/knowledge/search', body)
    return listPayload<KnowledgeSearchResult>(response.data)
  },
  status: async () => listPayload<KnowledgeModelStatus>((await http.get('/knowledge/status')).data),
}

export const adminApi = {
  overview: async () => payload<AdminOverview>((await http.get('/admin/overview')).data),
  models: async () => listPayload<AdminModel>((await http.get('/admin/models')).data),
  setModelActive: async (id: EntityId, active: boolean) => payload<AdminModel>((await http.patch(`/admin/models/${id}`, { active })).data),
  knowledgeStatus: async () => listPayload<KnowledgeModelStatus>((await http.get('/admin/knowledge/status')).data),
  safetyBlocks: async (limit = 20) => listPayload<AdminSafetyBlock>((await http.get('/admin/safety-blocks', { params: { limit } })).data),
  unresolvedReports: async (limit = 20) => listPayload<AdminUnresolvedReport>((await http.get('/admin/unresolved-reports', { params: { limit } })).data),
  auditLogs: async (limit = 20) => listPayload<AdminAuditLog>((await http.get('/admin/audit-logs', { params: { limit } })).data),
}
