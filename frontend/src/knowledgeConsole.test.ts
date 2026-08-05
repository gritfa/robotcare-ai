import { describe, expect, it, vi } from 'vitest'
import { describeDiff, useKnowledgeConsole } from './knowledgeConsole'
import type { KnowledgeConsoleApi } from './knowledgeConsole'
import type { AdminContentGap, AdminKnowledgeDiffPreview, AdminKnowledgeDocument } from './types'

function document(overrides: Partial<AdminKnowledgeDocument> = {}): AdminKnowledgeDocument {
  return {
    id: 1,
    robot_model_id: 1,
    model_code: 'JH69U1',
    title: '官方说明书',
    source_url: 'https://example.com/manual.pdf',
    sha256: 'a'.repeat(64),
    page_count: 20,
    chunk_count: 31,
    vector_count: 31,
    status: 'active',
    version: 1,
    embedding_model: 'text-embedding-v4',
    file_size: 2048,
    has_archived_file: true,
    created_at: '2026-08-01T00:00:00Z',
    updated_at: '2026-08-01T00:00:00Z',
    ...overrides,
  }
}

function gap(overrides: Partial<AdminContentGap> = {}): AdminContentGap {
  return {
    query_normalized: '滤网 多久换一次',
    count: 3,
    robot_model_id: 1,
    model_code: 'JH69U1',
    last_seen_at: '2026-08-01T00:00:00Z',
    status: 'open',
    linked_document_id: null,
    linked_document_title: null,
    replay_status: null,
    replay_citation_count: null,
    replay_answer_excerpt: null,
    replay_checked_at: null,
    resolved_at: null,
    note: null,
    ...overrides,
  }
}

function preview(overrides: Partial<AdminKnowledgeDiffPreview> = {}): AdminKnowledgeDiffPreview {
  return {
    status: 'changed',
    model_code: 'JH69U1',
    source_url: 'https://example.com/manual.pdf',
    incoming_title: '说明书',
    incoming_sha256: 'b'.repeat(64),
    incoming_page_count: 21,
    incoming_chunk_count: 33,
    document_id: 1,
    current_version: 2,
    current_title: '说明书',
    current_sha256: 'a'.repeat(64),
    current_page_count: 20,
    current_chunk_count: 31,
    current_updated_at: '2026-08-01T00:00:00Z',
    page_delta: 1,
    chunk_delta: 2,
    pages_comparable: true,
    pages_incomparable_reason: null,
    changed_pages: [5],
    added_pages: [21],
    removed_pages: [],
    ...overrides,
  }
}

function createApi(overrides: Partial<KnowledgeConsoleApi> = {}): KnowledgeConsoleApi {
  return {
    knowledgeDocuments: vi.fn().mockResolvedValue([document()]),
    knowledgeDocument: vi.fn().mockResolvedValue({ ...document(), versions: [] }),
    knowledgeChunks: vi.fn().mockResolvedValue({
      document_id: 1,
      total: 31,
      offset: 0,
      limit: 20,
      items: [{ chunk_index: 0, page_number: 1, content: '片段内容', has_embedding: true }],
    }),
    updateKnowledgeDocument: vi.fn().mockImplementation(async (_id, body) => document(body as Partial<AdminKnowledgeDocument>)),
    deleteKnowledgeDocument: vi.fn().mockResolvedValue(undefined),
    reindexKnowledgeDocument: vi.fn().mockResolvedValue({ ...document(), version: 2, versions: [] }),
    rollbackKnowledgeDocument: vi.fn().mockResolvedValue({ ...document(), version: 3, versions: [] }),
    previewKnowledgeUpload: vi.fn().mockResolvedValue(preview()),
    downloadKnowledgeFile: vi.fn().mockResolvedValue(new Blob(['pdf'])),
    updateContentGap: vi.fn().mockResolvedValue({}),
    replayContentGap: vi.fn().mockResolvedValue({
      replay_status: 'passed',
      citation_count: 3,
      answer_excerpt: '滤网建议每三个月更换。',
      detail: '已能基于知识库引用作答',
      gap_status: 'resolved',
      checked_at: '2026-08-05T00:00:00Z',
    }),
    ...overrides,
  }
}

describe('knowledge console', () => {
  it('loads documents with the active filters', async () => {
    const api = createApi()
    const console_ = useKnowledgeConsole(api)
    console_.modelFilter.value = 'JH69U1'
    console_.statusFilter.value = 'disabled'
    await console_.loadDocuments()

    expect(api.knowledgeDocuments).toHaveBeenCalledWith({ model_code: 'JH69U1', status: 'disabled' })
    expect(console_.documents.value).toHaveLength(1)
  })

  it('reflects a disable action in the list without dropping the row', async () => {
    const api = createApi()
    const console_ = useKnowledgeConsole(api)
    await console_.loadDocuments()

    const result = await console_.setStatus(console_.documents.value[0], 'disabled')

    expect(result.ok).toBe(true)
    expect(api.updateKnowledgeDocument).toHaveBeenCalledWith(1, { status: 'disabled' })
    // 停用是可逆的，文档必须还在列表里，只是状态变了
    expect(console_.documents.value).toHaveLength(1)
    expect(console_.documents.value[0].status).toBe('disabled')
  })

  it('removes the row only after the delete request succeeds', async () => {
    const api = createApi({ deleteKnowledgeDocument: vi.fn().mockRejectedValue(new Error('boom')) })
    const console_ = useKnowledgeConsole(api)
    await console_.loadDocuments()

    const failed = await console_.remove(console_.documents.value[0])
    expect(failed.ok).toBe(false)
    expect(console_.documents.value).toHaveLength(1)

    const ok = useKnowledgeConsole(createApi())
    await ok.loadDocuments()
    await ok.remove(ok.documents.value[0])
    expect(ok.documents.value).toHaveLength(0)
  })

  it('keeps a second action from firing while one is in flight for the same document', async () => {
    let release!: () => void
    const pending = new Promise<void>((resolve) => { release = resolve })
    const api = createApi({
      updateKnowledgeDocument: vi.fn().mockImplementation(async () => {
        await pending
        return document({ status: 'disabled' })
      }),
    })
    const console_ = useKnowledgeConsole(api)
    await console_.loadDocuments()
    const target = console_.documents.value[0]

    const first = console_.setStatus(target, 'disabled')
    expect(console_.isBusy(target.id)).toBe(true)
    const second = await console_.setStatus(target, 'active')
    expect(second.ok).toBe(false)
    expect(api.updateKnowledgeDocument).toHaveBeenCalledTimes(1)

    release()
    await first
    expect(console_.isBusy(target.id)).toBe(false)
  })

  it('paginates chunks and keeps the page filter across requests', async () => {
    const api = createApi()
    const console_ = useKnowledgeConsole(api)
    await console_.openDocument(1)
    expect(api.knowledgeChunks).toHaveBeenCalledWith(1, { offset: 0, limit: 20 })

    await console_.loadChunks(20, 5)
    expect(api.knowledgeChunks).toHaveBeenLastCalledWith(1, { offset: 20, limit: 20, page_number: 5 })
    expect(console_.chunkPageFilter.value).toBe(5)

    await console_.loadChunks(40)
    expect(api.knowledgeChunks).toHaveBeenLastCalledWith(1, { offset: 40, limit: 20, page_number: 5 })
  })

  it('replays a gap and surfaces the verdict', async () => {
    const api = createApi()
    const console_ = useKnowledgeConsole(api)

    const result = await console_.replayGap(gap())

    expect(result.ok).toBe(true)
    expect(api.replayContentGap).toHaveBeenCalledWith({ robot_model_id: 1, query_normalized: '滤网 多久换一次' })
    expect(console_.replayResult.value?.replay_status).toBe('passed')
    expect(console_.replayResult.value?.gap_status).toBe('resolved')
  })

  it('marks a gap as investigating when a document is linked', async () => {
    const api = createApi()
    const console_ = useKnowledgeConsole(api)

    await console_.linkGapDocument(gap(), 7)

    // 只是挂上资料还不算解决，必须复测通过才行
    expect(api.updateContentGap).toHaveBeenCalledWith({
      robot_model_id: 1,
      query_normalized: '滤网 多久换一次',
      linked_document_id: 7,
      status: 'investigating',
    })
  })
})

describe('diff description', () => {
  it('describes a brand new document', () => {
    expect(describeDiff(preview({ status: 'new', document_id: null, current_version: null })))
      .toContain('新文档')
  })

  it('says an identical upload produces no new version', () => {
    expect(describeDiff(preview({ status: 'identical' }))).toContain('完全一致')
  })

  it('lists page level changes for a modified document', () => {
    const text = describeDiff(preview())
    expect(text).toContain('将覆盖线上 v2')
    expect(text).toContain('页数 +1')
    expect(text).toContain('修改第 5 页')
    expect(text).toContain('新增第 21 页')
  })

  it('states why the page diff is unavailable instead of implying no changes', () => {
    const text = describeDiff(preview({
      pages_comparable: false,
      pages_incomparable_reason: '线上版本没有留存原始文件，无法逐页比对',
      changed_pages: [],
      added_pages: [],
    }))
    expect(text).toContain('无法逐页比对')
    expect(text).not.toContain('修改第')
  })
})
