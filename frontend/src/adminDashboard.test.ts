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
}

const model = (): AdminModel => ({ id: 1, code: 'JH69U1', name: 'JH69U1 扫地机器人', brand: 'Haier', active: true })

function createApi(overrides: Partial<AdminDashboardApi> = {}): AdminDashboardApi {
  return {
    overview: vi.fn().mockResolvedValue(overview),
    models: vi.fn().mockResolvedValue([model()]),
    setModelActive: vi.fn().mockImplementation(async (_id, active) => ({ ...model(), active })),
    knowledgeStatus: vi.fn().mockResolvedValue([{ robot_model_id: 1, model_code: 'JH69U1', document_count: 1, chunk_count: 31, vector_count: 31 }]),
    safetyBlocks: vi.fn().mockResolvedValue([]),
    unresolvedReports: vi.fn().mockResolvedValue([]),
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
    expect(dashboard.models.value[0].code).toBe('JH69U1')
    expect(dashboard.knowledgeHealthy.value).toBe(true)
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
})
