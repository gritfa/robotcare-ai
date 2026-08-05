import { computed, ref } from 'vue'
import { adminApi } from './api'
import { adminErrorMessage } from './adminDashboard'
import type {
  AdminContentGap,
  AdminContentGapReplay,
  AdminKnowledgeChunkPage,
  AdminKnowledgeDiffPreview,
  AdminKnowledgeDocument,
  AdminKnowledgeDocumentDetail,
  EntityId,
} from './types'

export interface KnowledgeConsoleApi {
  knowledgeDocuments(params?: { model_code?: string; status?: string }): Promise<AdminKnowledgeDocument[]>
  knowledgeDocument(id: EntityId): Promise<AdminKnowledgeDocumentDetail>
  knowledgeChunks(id: EntityId, params?: { offset?: number; limit?: number; page_number?: number }): Promise<AdminKnowledgeChunkPage>
  updateKnowledgeDocument(id: EntityId, body: { status?: string; title?: string }): Promise<AdminKnowledgeDocument>
  deleteKnowledgeDocument(id: EntityId): Promise<void>
  reindexKnowledgeDocument(id: EntityId): Promise<AdminKnowledgeDocumentDetail>
  rollbackKnowledgeDocument(id: EntityId, version: number): Promise<AdminKnowledgeDocumentDetail>
  previewKnowledgeUpload(form: FormData): Promise<AdminKnowledgeDiffPreview>
  downloadKnowledgeFile(id: EntityId): Promise<Blob>
  updateContentGap(body: { robot_model_id: EntityId; query_normalized: string; status?: string; linked_document_id?: EntityId; note?: string }): Promise<unknown>
  replayContentGap(body: { robot_model_id: EntityId; query_normalized: string; auto_resolve?: boolean }): Promise<AdminContentGapReplay>
}

export interface ActionOutcome {
  ok: boolean
  error?: string
}

export const CHUNK_PAGE_SIZE = 20

export function describeDiff(preview: AdminKnowledgeDiffPreview): string {
  if (preview.status === 'new') {
    return `新文档：共 ${preview.incoming_page_count} 页，将切分为 ${preview.incoming_chunk_count} 个片段。`
  }
  if (preview.status === 'identical') {
    return `内容与线上 v${preview.current_version} 完全一致（SHA256 相同），上传不会产生新版本。`
  }
  const parts = [`将覆盖线上 v${preview.current_version}`]
  if (preview.page_delta !== null && preview.page_delta !== 0) {
    parts.push(`页数 ${preview.page_delta > 0 ? '+' : ''}${preview.page_delta}`)
  }
  if (preview.chunk_delta !== null && preview.chunk_delta !== 0) {
    parts.push(`片段 ${preview.chunk_delta > 0 ? '+' : ''}${preview.chunk_delta}`)
  }
  if (preview.pages_comparable) {
    if (preview.changed_pages.length) parts.push(`修改第 ${preview.changed_pages.join('、')} 页`)
    if (preview.added_pages.length) parts.push(`新增第 ${preview.added_pages.join('、')} 页`)
    if (preview.removed_pages.length) parts.push(`删除第 ${preview.removed_pages.join('、')} 页`)
  } else if (preview.pages_incomparable_reason) {
    // 不能拿"没有差异"冒充"没法比对"——用户据此决定要不要覆盖线上资料
    parts.push(preview.pages_incomparable_reason)
  }
  return `${parts.join('；')}。`
}

export function useKnowledgeConsole(api: KnowledgeConsoleApi = adminApi) {
  const documents = ref<AdminKnowledgeDocument[]>([])
  const loading = ref(false)
  const error = ref('')
  const modelFilter = ref('')
  const statusFilter = ref('')

  const selected = ref<AdminKnowledgeDocumentDetail | null>(null)
  const chunkPage = ref<AdminKnowledgeChunkPage | null>(null)
  const chunkOffset = ref(0)
  const chunkPageFilter = ref<number | null>(null)
  const detailLoading = ref(false)
  const busyIds = ref<Set<EntityId>>(new Set())

  const preview = ref<AdminKnowledgeDiffPreview | null>(null)
  const previewing = ref(false)

  const replayResult = ref<AdminContentGapReplay | null>(null)
  const replayingKey = ref('')

  const disabledCount = computed(() => documents.value.filter((item) => item.status === 'disabled').length)
  const missingArchiveCount = computed(() => documents.value.filter((item) => !item.has_archived_file).length)

  function isBusy(id: EntityId) {
    return busyIds.value.has(id)
  }

  async function withBusy<T>(id: EntityId, action: () => Promise<T>): Promise<T | null> {
    if (busyIds.value.has(id)) return null
    const next = new Set(busyIds.value)
    next.add(id)
    busyIds.value = next
    try {
      return await action()
    } finally {
      const rest = new Set(busyIds.value)
      rest.delete(id)
      busyIds.value = rest
    }
  }

  async function loadDocuments() {
    loading.value = true
    error.value = ''
    try {
      const params: { model_code?: string; status?: string } = {}
      if (modelFilter.value) params.model_code = modelFilter.value
      if (statusFilter.value) params.status = statusFilter.value
      documents.value = await api.knowledgeDocuments(params)
    } catch (err) {
      error.value = adminErrorMessage(err, '知识文档列表加载失败，请稍后重试。')
    } finally {
      loading.value = false
    }
  }

  async function openDocument(id: EntityId) {
    detailLoading.value = true
    error.value = ''
    chunkOffset.value = 0
    chunkPageFilter.value = null
    try {
      selected.value = await api.knowledgeDocument(id)
      chunkPage.value = await api.knowledgeChunks(id, { offset: 0, limit: CHUNK_PAGE_SIZE })
    } catch (err) {
      error.value = adminErrorMessage(err, '文档详情加载失败，请稍后重试。')
    } finally {
      detailLoading.value = false
    }
  }

  function closeDocument() {
    selected.value = null
    chunkPage.value = null
  }

  async function loadChunks(offset: number, pageNumber: number | null = chunkPageFilter.value) {
    if (!selected.value) return
    detailLoading.value = true
    try {
      const params: { offset: number; limit: number; page_number?: number } = { offset, limit: CHUNK_PAGE_SIZE }
      if (pageNumber) params.page_number = pageNumber
      chunkPage.value = await api.knowledgeChunks(selected.value.id, params)
      chunkOffset.value = offset
      chunkPageFilter.value = pageNumber
    } catch (err) {
      error.value = adminErrorMessage(err, '分片加载失败，请稍后重试。')
    } finally {
      detailLoading.value = false
    }
  }

  function applyUpdated(updated: AdminKnowledgeDocument) {
    const index = documents.value.findIndex((item) => item.id === updated.id)
    if (index >= 0) documents.value[index] = { ...documents.value[index], ...updated }
  }

  async function setStatus(document: AdminKnowledgeDocument, status: 'active' | 'disabled'): Promise<ActionOutcome> {
    const result = await withBusy(document.id, async () => {
      try {
        applyUpdated(await api.updateKnowledgeDocument(document.id, { status }))
        return { ok: true }
      } catch (err) {
        return { ok: false, error: adminErrorMessage(err, '文档状态更新失败，请重试。') }
      }
    })
    return result ?? { ok: false }
  }

  async function rename(document: AdminKnowledgeDocument, title: string): Promise<ActionOutcome> {
    const result = await withBusy(document.id, async () => {
      try {
        applyUpdated(await api.updateKnowledgeDocument(document.id, { title }))
        return { ok: true }
      } catch (err) {
        return { ok: false, error: adminErrorMessage(err, '文档改名失败，请重试。') }
      }
    })
    return result ?? { ok: false }
  }

  async function remove(document: AdminKnowledgeDocument): Promise<ActionOutcome> {
    const result = await withBusy(document.id, async () => {
      try {
        await api.deleteKnowledgeDocument(document.id)
        documents.value = documents.value.filter((item) => item.id !== document.id)
        if (selected.value?.id === document.id) closeDocument()
        return { ok: true }
      } catch (err) {
        return { ok: false, error: adminErrorMessage(err, '文档删除失败，请重试。') }
      }
    })
    return result ?? { ok: false }
  }

  async function reindex(document: AdminKnowledgeDocument): Promise<ActionOutcome> {
    const result = await withBusy(document.id, async () => {
      try {
        const detail = await api.reindexKnowledgeDocument(document.id)
        applyUpdated(detail)
        if (selected.value?.id === detail.id) selected.value = detail
        return { ok: true }
      } catch (err) {
        return { ok: false, error: adminErrorMessage(err, '重新向量化失败，请重试。') }
      }
    })
    return result ?? { ok: false }
  }

  async function rollback(document: AdminKnowledgeDocument, version: number): Promise<ActionOutcome> {
    const result = await withBusy(document.id, async () => {
      try {
        const detail = await api.rollbackKnowledgeDocument(document.id, version)
        applyUpdated(detail)
        if (selected.value?.id === detail.id) selected.value = detail
        return { ok: true }
      } catch (err) {
        return { ok: false, error: adminErrorMessage(err, '版本回滚失败，请重试。') }
      }
    })
    return result ?? { ok: false }
  }

  async function runPreview(form: FormData): Promise<ActionOutcome> {
    previewing.value = true
    preview.value = null
    try {
      preview.value = await api.previewKnowledgeUpload(form)
      return { ok: true }
    } catch (err) {
      return { ok: false, error: adminErrorMessage(err, '差异预览失败，请检查型号、来源 URL 与 PDF 文件。') }
    } finally {
      previewing.value = false
    }
  }

  function clearPreview() {
    preview.value = null
  }

  async function download(document: AdminKnowledgeDocument): Promise<ActionOutcome> {
    try {
      const blob = await api.downloadKnowledgeFile(document.id)
      const url = URL.createObjectURL(blob)
      const anchor = window.document.createElement('a')
      anchor.href = url
      anchor.download = `${document.title || 'document'}.pdf`
      anchor.click()
      URL.revokeObjectURL(url)
      return { ok: true }
    } catch (err) {
      return { ok: false, error: adminErrorMessage(err, '原始文件下载失败，请重试。') }
    }
  }

  function gapKey(gap: AdminContentGap) {
    return `${gap.robot_model_id}::${gap.query_normalized}`
  }

  async function linkGapDocument(gap: AdminContentGap, documentId: EntityId): Promise<ActionOutcome> {
    try {
      await api.updateContentGap({
        robot_model_id: gap.robot_model_id,
        query_normalized: gap.query_normalized,
        linked_document_id: documentId,
        status: 'investigating',
      })
      return { ok: true }
    } catch (err) {
      return { ok: false, error: adminErrorMessage(err, '关联文档失败，请重试。') }
    }
  }

  async function setGapStatus(gap: AdminContentGap, status: string): Promise<ActionOutcome> {
    try {
      await api.updateContentGap({
        robot_model_id: gap.robot_model_id,
        query_normalized: gap.query_normalized,
        status,
      })
      return { ok: true }
    } catch (err) {
      return { ok: false, error: adminErrorMessage(err, '缺口状态更新失败，请重试。') }
    }
  }

  async function replayGap(gap: AdminContentGap): Promise<ActionOutcome> {
    if (replayingKey.value) return { ok: false }
    replayingKey.value = gapKey(gap)
    replayResult.value = null
    try {
      replayResult.value = await api.replayContentGap({
        robot_model_id: gap.robot_model_id,
        query_normalized: gap.query_normalized,
      })
      return { ok: true }
    } catch (err) {
      return { ok: false, error: adminErrorMessage(err, '缺口复测失败，请稍后重试。') }
    } finally {
      replayingKey.value = ''
    }
  }

  function isReplaying(gap: AdminContentGap) {
    return replayingKey.value === gapKey(gap)
  }

  return {
    documents,
    loading,
    error,
    modelFilter,
    statusFilter,
    selected,
    chunkPage,
    chunkOffset,
    chunkPageFilter,
    detailLoading,
    disabledCount,
    missingArchiveCount,
    preview,
    previewing,
    replayResult,
    isBusy,
    isReplaying,
    loadDocuments,
    openDocument,
    closeDocument,
    loadChunks,
    setStatus,
    rename,
    remove,
    reindex,
    rollback,
    runPreview,
    clearPreview,
    download,
    linkGapDocument,
    setGapStatus,
    replayGap,
    gapKey,
  }
}
