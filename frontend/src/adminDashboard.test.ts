import { describe, expect, it, vi } from 'vitest'
import type { AxiosError } from 'axios'
import { adminErrorMessage, useAdminDashboard, type AdminDashboardApi } from './adminDashboard'
import type { AdminModel, AdminOverview } from './types'

const overview: AdminOverview = {
  user_count: 12,
  active_model_count: 2,
  published_flow_count: 4,
  knowledge_document_count: 2,
  knowledge_chunk_count: 53,
  safety_block_count: 3,
  unresolved_diagnostic_count: 1,
  service_report_count: 5,
  generation_stats: { answered_count: 7, refused_count: 2, refusal_by_reason: { knowledge_gap: 2 } },
  token_cost: {
    window_days: 30, prompt_tokens: 1200, completion_tokens: 300, estimated_cost: 0.0012,
    records_with_usage: 3, total_records: 4, by_model: { 'qwen-plus': 0.0012 },
  },
  content_gap_count: 2,
}

const model = (): AdminModel => ({ id: 1, code: 'JH69U1', name: 'JH69U1 扫地机器人', brand: 'Haier', active: true })

function createApi(overrides: Partial<AdminDashboardApi> = {}): AdminDashboardApi {
  return {
    overview: vi.fn().mockResolvedValue(overview),
    models: vi.fn().mockResolvedValue([model()]),
    setModelActive: vi.fn().mockImplementation(async (_id, active) => ({ ...model(), active })),
    createModel: vi.fn().mockImplementation(async (body) => ({ id: 99, ...body, active: true })),
    feedback: vi.fn().mockResolvedValue({
      window_days: 30, total_count: 3, helpful_count: 1, unhelpful_count: 2,
      by_reason: { unclear_steps: 2 }, by_model: { JH69U1: 2 },
      items: [{
        id: 1, conversation_id: 7, message_id: 11, model_code: 'JH69U1', helpful: false,
        reason: 'unclear_steps', refusal_reason: null, answer_excerpt: '按第 3 页操作',
        created_at: '2026-08-05T00:00:00Z',
      }],
    }),
    conversations: vi.fn().mockResolvedValue([{
      id: 7, title: '回充失败', model_code: 'JH69U1', user_email_masked: 'c***@example.com',
      message_count: 4, resolved: null, updated_at: '2026-08-05T00:00:00Z',
    }]),
    conversationDetail: vi.fn().mockResolvedValue({
      id: 7, title: '回充失败', model_code: 'JH69U1', user_email_masked: 'c***@example.com',
      resolved: null, created_at: '2026-08-05T00:00:00Z', updated_at: '2026-08-05T00:00:00Z',
      messages: [{ id: 11, role: 'assistant', content: '按第 3 页操作 [1]。', refusal_reason: null, intent: null, created_at: '2026-08-05T00:00:00Z' }],
    }),
    knowledgeStatus: vi.fn().mockResolvedValue([{ robot_model_id: 1, model_code: 'JH69U1', document_count: 1, chunk_count: 31, vector_count: 31 }]),
    contentGaps: vi.fn().mockResolvedValue([
      {
        query_normalized: '石头卡住 怎么办', count: 3, robot_model_id: 1, model_code: 'JH69U1',
        last_seen_at: '2026-07-30T00:00:00Z', status: 'open', linked_document_id: null,
        linked_document_title: null, replay_status: null, replay_citation_count: null,
        replay_answer_excerpt: null, replay_checked_at: null, resolved_at: null, note: null,
      },
    ]),
    uploadKnowledge: vi.fn().mockResolvedValue({ document_id: 9, created: true, changed: false, chunk_count: 12, sha256: 'a'.repeat(64) }),
    safetyBlocks: vi.fn().mockResolvedValue([]),
    safetyBlockDetail: vi.fn().mockResolvedValue({
      id: 3, user_id: 4, device_id: 5, model_code: 'JH69U1', category: 'smoke',
      risk_level: 'critical', reason: '检测到高风险', advice: '立即断电', created_at: '2026-07-22T00:00:00Z',
    }),
    unresolvedReports: vi.fn().mockResolvedValue([]),
    diagnosticDetail: vi.fn().mockResolvedValue({
      id: 6, user_id: 4, device_id: 5, flow_id: 7, status: 'unresolved',
      issue_description: '机器人无法回充', error_code: 'E1', created_at: '2026-07-22T00:00:00Z',
    }),
    reportDetail: vi.fn().mockResolvedValue({
      id: 8, session_id: 6, report_number: 'RC-8', content: '完整售后报告', created_at: '2026-07-22T00:00:00Z',
    }),
    auditLogs: vi.fn().mockResolvedValue([]),
    ...overrides,
  }
}

describe('admin dashboard state', () => {
  it('exposes a real loading state and applies all admin responses together', async () => {
    let resolveOverview!: (value: AdminOverview) => void
    const pendingOverview = new Promise<AdminOverview>((resolve) => { resolveOverview = resolve })
    const api = createApi({ overview: vi.fn().mockReturnValue(pendingOverview) })
    const dashboard = useAdminDashboard(api)

    const request = dashboard.load()
    expect(dashboard.loading.value).toBe(true)
    expect(dashboard.loaded.value).toBe(false)

    resolveOverview(overview)
    await request

    expect(dashboard.loading.value).toBe(false)
    expect(dashboard.loaded.value).toBe(true)
    expect(dashboard.overview.value.user_count).toBe(12)
    expect(dashboard.overview.value.generation_stats.answered_count).toBe(7)
    expect(dashboard.overview.value.content_gap_count).toBe(2)
    expect(dashboard.models.value[0].code).toBe('JH69U1')
    expect(dashboard.knowledgeHealthy.value).toBe(true)
    expect(dashboard.contentGaps.value[0].query_normalized).toBe('石头卡住 怎么办')
    expect(dashboard.contentGaps.value[0].model_code).toBe('JH69U1')
    expect(dashboard.contentGaps.value[0].status).toBe('open')
  })

  it('uploads a knowledge PDF, refreshes the dashboard, and surfaces failures', async () => {
    const api = createApi()
    const dashboard = useAdminDashboard(api)
    await dashboard.load()

    const form = new FormData()
    const outcome = await dashboard.uploadKnowledge(form)

    expect(outcome.ok).toBe(true)
    expect(outcome.result?.chunk_count).toBe(12)
    expect(api.uploadKnowledge).toHaveBeenCalledWith(form)
    // 上传成功后整体刷新（知识状态 + 概览一起更新）
    expect(api.knowledgeStatus).toHaveBeenCalledTimes(2)
    expect(api.overview).toHaveBeenCalledTimes(2)
    expect(dashboard.uploadingKnowledge.value).toBe(false)

    const failing = useAdminDashboard(createApi({
      uploadKnowledge: vi.fn().mockRejectedValue(new Error('not a pdf')),
    }))
    const failure = await failing.uploadKnowledge(new FormData())
    expect(failure.ok).toBe(false)
    expect(failure.error).toBe('not a pdf')
  })

  it('persists a model switch and prevents a duplicate request while it is pending', async () => {
    let resolveUpdate!: (value: AdminModel) => void
    const update = vi.fn().mockReturnValue(new Promise<AdminModel>((resolve) => { resolveUpdate = resolve }))
    const dashboard = useAdminDashboard(createApi({ setModelActive: update }))
    await dashboard.load()
    const selected = dashboard.models.value[0]

    const first = dashboard.setModelActive(selected, false)
    const duplicate = await dashboard.setModelActive(selected, true)

    expect(selected.active).toBe(false)
    expect(dashboard.isModelUpdating(selected.id)).toBe(true)
    expect(update).toHaveBeenCalledTimes(1)
    expect(duplicate.ok).toBe(false)

    resolveUpdate({ ...selected, active: false })
    expect((await first).ok).toBe(true)
    expect(dashboard.isModelUpdating(selected.id)).toBe(false)
  })

  it('rolls back the optimistic model state when the server rejects the update', async () => {
    const api = createApi({ setModelActive: vi.fn().mockRejectedValue(new Error('network down')) })
    const dashboard = useAdminDashboard(api)
    await dashboard.load()
    const selected = dashboard.models.value[0]

    const result = await dashboard.setModelActive(selected, false)

    expect(result.ok).toBe(false)
    expect(result.error).toBe('network down')
    expect(selected.active).toBe(true)
  })

  it('shows an explicit permission message for a backend 403 instead of reporting success', async () => {
    const forbidden = { isAxiosError: true, response: { status: 403 } } as AxiosError
    const dashboard = useAdminDashboard(createApi({ overview: vi.fn().mockRejectedValue(forbidden) }))

    await dashboard.load()

    expect(dashboard.loaded.value).toBe(false)
    expect(dashboard.loadError.value).toContain('没有管理员权限')
    expect(adminErrorMessage(forbidden)).toContain('403')
  })

  it('loads sensitive content only after an explicit detail action', async () => {
    const api = createApi()
    const dashboard = useAdminDashboard(api)

    await dashboard.load()
    expect(api.safetyBlockDetail).not.toHaveBeenCalled()
    expect(api.diagnosticDetail).not.toHaveBeenCalled()
    expect(api.reportDetail).not.toHaveBeenCalled()

    await dashboard.openSafetyBlock(3)
    await dashboard.openDiagnostic(6)
    await dashboard.openReport(8)

    expect(api.safetyBlockDetail).toHaveBeenCalledWith(3)
    expect(api.diagnosticDetail).toHaveBeenCalledWith(6)
    expect(api.reportDetail).toHaveBeenCalledWith(8)
    expect(dashboard.selectedSafetyBlock.value?.reason).toBe('检测到高风险')
    expect(dashboard.selectedDiagnostic.value?.issue_description).toBe('机器人无法回充')
    expect(dashboard.selectedReport.value?.content).toBe('完整售后报告')
  })
})

describe('createModel', () => {
  it('创建成功后立刻并入列表并按型号码排序，无需重新拉取', async () => {
    const dashboard = useAdminDashboard(createApi())
    await dashboard.load()

    const result = await dashboard.createModel({ code: 'AA-01', name: '新接入型号', brand: '示例品牌' })

    expect(result.ok).toBe(true)
    expect(dashboard.models.value.map((item) => item.code)).toEqual(['AA-01', 'JH69U1'])
    expect(dashboard.overview.value.active_model_count).toBe(2)
  })

  it('重复型号码报错时不污染列表', async () => {
    const conflict = Object.assign(new Error('conflict'), {
      isAxiosError: true,
      response: { status: 409, data: { detail: 'Robot model code already exists' } },
    }) as AxiosError
    const dashboard = useAdminDashboard(createApi({ createModel: vi.fn().mockRejectedValue(conflict) }))
    await dashboard.load()

    const result = await dashboard.createModel({ code: 'JH69U1', name: '重复型号', brand: '海尔' })

    expect(result.ok).toBe(false)
    expect(result.error).toBeTruthy()
    expect(dashboard.models.value).toHaveLength(1)
  })
})

describe('反馈与会话检索', () => {
  it('按原因筛选时把 reason 透传给接口', async () => {
    const api = createApi()
    const dashboard = useAdminDashboard(api)
    dashboard.feedbackReason.value = 'unclear_steps'

    await dashboard.loadFeedback()

    expect(api.feedback).toHaveBeenCalledWith({ reason: 'unclear_steps' })
    expect(dashboard.feedback.value?.by_reason.unclear_steps).toBe(2)
  })

  it('搜索关键词去空白后透传，空搜索不传参', async () => {
    const api = createApi()
    const dashboard = useAdminDashboard(api)

    dashboard.conversationSearch.value = '  拆电池  '
    await dashboard.loadConversations()
    expect(api.conversations).toHaveBeenCalledWith({ search: '拆电池', model_code: undefined })

    dashboard.conversationSearch.value = '   '
    await dashboard.loadConversations()
    expect(api.conversations).toHaveBeenLastCalledWith({ search: undefined, model_code: undefined })
  })

  it('打开对话拉取完整内容', async () => {
    const dashboard = useAdminDashboard(createApi())

    const result = await dashboard.openConversation(7)

    expect(result.ok).toBe(true)
    expect(dashboard.selectedConversation.value?.messages).toHaveLength(1)
  })
})
