import axios, { type AxiosError, type InternalAxiosRequestConfig } from 'axios'
import type { AdminAuditLog, AdminContentGap, AdminDiagnosticDetail, AdminKnowledgeUploadResult, AdminModel, AdminOverview, AdminSafetyBlock, AdminSafetyBlockDetail, AdminServiceReportDetail, AdminUnresolvedReport, Attachment, AuthResult, ChatMessagePair, Conversation, ConversationDetail, Device, Diagnostic, DiagnosticOption, DiagnosticStep, EntityId, KnowledgeHealth, KnowledgeModelStatus, KnowledgeAnswer,
  KnowledgeSearchResult, ReportPdf, RobotModel, ServiceReport, User } from './types'
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

export type ApiErrorCode =
  | 'SAFETY_BLOCKED'
  | 'ISSUE_CATEGORY_MISMATCH'
  | 'STALE_DIAGNOSTIC_STEP'
  | 'FEEDBACK_CONFLICT'
  | 'AUTHENTICATION_REQUIRED'
  | 'RATE_LIMITED'
  | (string & {})

export interface ApiValidationIssue {
  location: Array<string | number>
  message: string
  type?: string
}

export interface ApiErrorInfo {
  code?: ApiErrorCode
  message: string
  status?: number
  validationIssues: ApiValidationIssue[]
  selectedCategory?: string
  suggestedCategories: string[]
  requiresConfirmation: boolean
  blocked: boolean
  category?: string
  riskLevel?: string
  reason?: string
  officialServiceAdvice?: string
  shouldPowerOff?: boolean
  retryAfterSeconds?: number
  raw: unknown
}

type ErrorObject = Record<string, unknown>

function record(value: unknown): ErrorObject | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as ErrorObject
    : null
}

function text(value: unknown) {
  return typeof value === 'string' && value.trim() ? value.trim() : undefined
}

function validationIssues(value: unknown): ApiValidationIssue[] {
  if (!Array.isArray(value)) return []
  return value.flatMap((item) => {
    const issue = record(item)
    const message = text(issue?.msg) || text(issue?.message)
    if (!issue || !message) return []
    return [{
      location: Array.isArray(issue.loc) ? issue.loc.filter(part => ['string', 'number'].includes(typeof part)) as Array<string | number> : [],
      message,
      type: text(issue.type),
    }]
  })
}

function suggestedCategoryCodes(value: unknown): string[] {
  if (!Array.isArray(value)) return []
  return [...new Set(value.flatMap((item) => {
    if (typeof item === 'string' && item.trim()) return [item.trim()]
    const candidate = record(item)
    const code = text(candidate?.code) || text(candidate?.issue_category_code) || text(candidate?.category)
    return code ? [code] : []
  }))]
}

function retryAfterSeconds(error: unknown) {
  if (!axios.isAxiosError(error)) return undefined
  const headers = error.response?.headers as { get?: (name: string) => unknown; [name: string]: unknown } | undefined
  const raw = headers?.get?.('retry-after') ?? headers?.['retry-after']
  const seconds = Number(raw)
  if (Number.isFinite(seconds) && seconds >= 0) return Math.ceil(seconds)
  if (typeof raw === 'string') {
    const retryAt = Date.parse(raw)
    if (Number.isFinite(retryAt)) return Math.max(0, Math.ceil((retryAt - Date.now()) / 1000))
  }
  return undefined
}

export function parseApiError(error: unknown, fallback = '请求失败，请稍后重试'): ApiErrorInfo {
  const status = axios.isAxiosError(error) ? error.response?.status : undefined
  const responseBody = axios.isAxiosError(error) ? error.response?.data : undefined
  const body = record(responseBody)
  const detail = body && 'detail' in body ? body.detail : responseBody
  const business = record(detail) || body
  const issues = validationIssues(detail)
  const explicitCode = text(business?.code)
  const code: ApiErrorCode | undefined = explicitCode
    || (status === 401 ? 'AUTHENTICATION_REQUIRED' : status === 429 ? 'RATE_LIMITED' : undefined)
  const objectMessage = text(business?.message) || text(business?.reason) || text(body?.message)
  const arrayMessage = issues.map(issue => issue.message).join('；')
  const message = text(detail)
    || objectMessage
    || arrayMessage
    || (error instanceof Error && !axios.isAxiosError(error) ? error.message : undefined)
    || fallback

  return {
    code,
    message,
    status,
    validationIssues: issues,
    selectedCategory: text(business?.selected_category) || text(business?.selected),
    suggestedCategories: suggestedCategoryCodes(business?.suggested_categories).length
      ? suggestedCategoryCodes(business?.suggested_categories)
      : suggestedCategoryCodes([business?.suggested]),
    requiresConfirmation: business?.requires_confirmation === true,
    blocked: business?.blocked === true || code === 'SAFETY_BLOCKED',
    category: text(business?.category),
    riskLevel: text(business?.risk_level),
    reason: text(business?.reason),
    officialServiceAdvice: text(business?.official_service_advice) || text(business?.advice),
    shouldPowerOff: typeof business?.should_power_off === 'boolean' ? business.should_power_off : undefined,
    retryAfterSeconds: retryAfterSeconds(error),
    raw: detail,
  }
}

export function apiError(error: unknown, fallback = '请求失败，请稍后重试') {
  return userFacingApiError(error, fallback)
}

export function userFacingApiError(error: unknown, fallback = '请求失败，请稍后重试') {
  const parsed = parseApiError(error, fallback)
  if (parsed.code !== 'RATE_LIMITED') return parsed.message
  const retryHint = parsed.retryAfterSeconds === undefined
    ? '请稍后重试'
    : parsed.retryAfterSeconds === 0
      ? '现在可以重试'
      : `请在 ${parsed.retryAfterSeconds} 秒后重试`
  return `${parsed.message}；${retryHint}`
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
  register: async (body: { email: string; password: string; name?: string; invite_code?: string }) => payload<AuthResult>((await http.post('/auth/register', {
    email: body.email,
    password: body.password,
    invite_code: body.invite_code || undefined,
  })).data),
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
  create: async (body: { device_id: EntityId; issue_category_code: string; issue_description: string; error_code?: string; confirm_category_mismatch?: boolean; source_conversation_id?: number }) => payload<Diagnostic>((await http.post('/diagnostics', body)).data),
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

export const conversationApi = {
  create: async (robotModelId: EntityId) => payload<Conversation>((await http.post('/conversations', { robot_model_id: Number(robotModelId) })).data),
  list: async () => listPayload<Conversation>((await http.get('/conversations')).data),
  get: async (id: EntityId) => payload<ConversationDetail>((await http.get(`/conversations/${id}`)).data),
  sendMessage: async (id: EntityId, content: string) => payload<ChatMessagePair>((await http.post(`/conversations/${id}/messages`, { content })).data),
}

export const knowledgeApi = {
  search: async (body: { robot_model_id: number; query: string; top_k?: number }) => {
    const response = await http.post('/knowledge/search', body)
    return listPayload<KnowledgeSearchResult>(response.data)
  },
  answer: async (body: { robot_model_id: number; query: string; top_k?: number }) =>
    payload<KnowledgeAnswer>((await http.post('/knowledge/answer', body)).data),
  health: async () => payload<KnowledgeHealth>((await http.get('/knowledge/health')).data),
  status: async () => listPayload<KnowledgeModelStatus>((await http.get('/knowledge/status')).data),
}

export const adminApi = {
  overview: async () => payload<AdminOverview>((await http.get('/admin/overview')).data),
  models: async () => listPayload<AdminModel>((await http.get('/admin/models')).data),
  setModelActive: async (id: EntityId, active: boolean) => payload<AdminModel>((await http.patch(`/admin/models/${id}`, { active })).data),
  knowledgeStatus: async () => listPayload<KnowledgeModelStatus>((await http.get('/admin/knowledge/status')).data),
  contentGaps: async (days = 30, limit = 20) => listPayload<AdminContentGap>((await http.get('/admin/content-gaps', { params: { days, limit } })).data),
  uploadKnowledge: async (form: FormData) => payload<AdminKnowledgeUploadResult>((await http.post('/admin/knowledge/upload', form)).data),
  safetyBlocks: async (limit = 20) => listPayload<AdminSafetyBlock>((await http.get('/admin/safety-blocks', { params: { limit } })).data),
  safetyBlockDetail: async (id: EntityId) => payload<AdminSafetyBlockDetail>((await http.get(`/admin/safety-blocks/${id}`)).data),
  unresolvedReports: async (limit = 20) => listPayload<AdminUnresolvedReport>((await http.get('/admin/unresolved-reports', { params: { limit } })).data),
  diagnosticDetail: async (id: EntityId) => payload<AdminDiagnosticDetail>((await http.get(`/admin/diagnostics/${id}`)).data),
  reportDetail: async (id: EntityId) => payload<AdminServiceReportDetail>((await http.get(`/admin/reports/${id}`)).data),
  auditLogs: async (limit = 20) => listPayload<AdminAuditLog>((await http.get('/admin/audit-logs', { params: { limit } })).data),
}
