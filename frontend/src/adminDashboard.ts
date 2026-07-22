import axios from 'axios'
import { computed, reactive, ref } from 'vue'
import { adminApi, apiError } from './api'
import type { AdminAuditLog, AdminDiagnosticDetail, AdminModel, AdminOverview, AdminSafetyBlock, AdminSafetyBlockDetail, AdminServiceReportDetail, AdminUnresolvedReport, EntityId, KnowledgeModelStatus } from './types'

export interface AdminDashboardApi {
  overview(): Promise<AdminOverview>
  models(): Promise<AdminModel[]>
  setModelActive(id: EntityId, active: boolean): Promise<AdminModel>
  knowledgeStatus(): Promise<KnowledgeModelStatus[]>
  safetyBlocks(limit?: number): Promise<AdminSafetyBlock[]>
  safetyBlockDetail(id: EntityId): Promise<AdminSafetyBlockDetail>
  unresolvedReports(limit?: number): Promise<AdminUnresolvedReport[]>
  diagnosticDetail(id: EntityId): Promise<AdminDiagnosticDetail>
  reportDetail(id: EntityId): Promise<AdminServiceReportDetail>
  auditLogs(limit?: number): Promise<AdminAuditLog[]>
}

export interface ModelUpdateResult {
  ok: boolean
  error?: string
}

const emptyOverview = (): AdminOverview => ({
  user_count: 0,
  active_model_count: 0,
  published_flow_count: 0,
  knowledge_document_count: 0,
  knowledge_chunk_count: 0,
  safety_block_count: 0,
  unresolved_diagnostic_count: 0,
  service_report_count: 0,
})

export function adminErrorMessage(error: unknown, fallback = '管理员数据加载失败，请稍后重试。') {
  if (axios.isAxiosError(error) && error.response?.status === 403) {
    return '当前账号没有管理员权限（403），请使用管理员账号重新登录。'
  }
  return apiError(error, fallback)
}

export function useAdminDashboard(api: AdminDashboardApi = adminApi) {
  const loading = ref(false)
  const loaded = ref(false)
  const loadError = ref('')
  const overview = ref<AdminOverview>(emptyOverview())
  const models = ref<AdminModel[]>([])
  const knowledgeStatus = ref<KnowledgeModelStatus[]>([])
  const safetyBlocks = ref<AdminSafetyBlock[]>([])
  const unresolvedReports = ref<AdminUnresolvedReport[]>([])
  const auditLogs = ref<AdminAuditLog[]>([])
  const selectedSafetyBlock = ref<AdminSafetyBlockDetail | null>(null)
  const selectedDiagnostic = ref<AdminDiagnosticDetail | null>(null)
  const selectedReport = ref<AdminServiceReportDetail | null>(null)
  const detailLoading = ref(false)
  const detailError = ref('')
  const updatingModelIds = reactive(new Set<EntityId>())

  const knowledgeHealthy = computed(() => knowledgeStatus.value.length > 0 && knowledgeStatus.value.every((item) => (
    item.document_count > 0
    && item.chunk_count > 0
    && item.vector_count === item.chunk_count
  )))

  async function load() {
    if (loading.value) return
    loading.value = true
    loadError.value = ''
    try {
      const [overviewData, modelData, knowledgeData, safetyData, reportData, auditData] = await Promise.all([
        api.overview(),
        api.models(),
        api.knowledgeStatus(),
        api.safetyBlocks(20),
        api.unresolvedReports(20),
        api.auditLogs(20),
      ])
      overview.value = overviewData
      models.value = modelData
      knowledgeStatus.value = knowledgeData
      safetyBlocks.value = safetyData
      unresolvedReports.value = reportData
      auditLogs.value = auditData
      loaded.value = true
    } catch (error) {
      loadError.value = adminErrorMessage(error)
    } finally {
      loading.value = false
    }
  }

  async function setModelActive(model: AdminModel, active: boolean): Promise<ModelUpdateResult> {
    if (updatingModelIds.has(model.id)) return { ok: false }
    const previousActive = model.active
    model.active = active
    updatingModelIds.add(model.id)
    try {
      const updated = await api.setModelActive(model.id, active)
      Object.assign(model, updated)
      overview.value.active_model_count = models.value.filter((item) => item.active).length
      return { ok: true }
    } catch (error) {
      model.active = previousActive
      return { ok: false, error: adminErrorMessage(error, `型号 ${model.code} 状态更新失败，请重试。`) }
    } finally {
      updatingModelIds.delete(model.id)
    }
  }

  function isModelUpdating(id: EntityId) {
    return updatingModelIds.has(id)
  }

  async function loadSensitiveDetail<T>(loader: () => Promise<T>): Promise<T | null> {
    detailLoading.value = true
    detailError.value = ''
    try {
      return await loader()
    } catch (error) {
      detailError.value = adminErrorMessage(error, '敏感详情加载失败，请稍后重试。')
      return null
    } finally {
      detailLoading.value = false
    }
  }

  async function openSafetyBlock(id: EntityId) {
    selectedSafetyBlock.value = null
    selectedSafetyBlock.value = await loadSensitiveDetail(() => api.safetyBlockDetail(id))
  }

  async function openDiagnostic(id: EntityId) {
    selectedDiagnostic.value = null
    selectedDiagnostic.value = await loadSensitiveDetail(() => api.diagnosticDetail(id))
  }

  async function openReport(id: EntityId) {
    selectedReport.value = null
    selectedReport.value = await loadSensitiveDetail(() => api.reportDetail(id))
  }

  return {
    loading,
    loaded,
    loadError,
    overview,
    models,
    knowledgeStatus,
    knowledgeHealthy,
    safetyBlocks,
    unresolvedReports,
    auditLogs,
    selectedSafetyBlock,
    selectedDiagnostic,
    selectedReport,
    detailLoading,
    detailError,
    load,
    setModelActive,
    isModelUpdating,
    openSafetyBlock,
    openDiagnostic,
    openReport,
  }
}
