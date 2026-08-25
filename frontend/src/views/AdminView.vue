<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import type { UploadFile, UploadInstance } from 'element-plus'
import {
  CircleCheckFilled,
  CircleCloseFilled,
  Connection,
  DataAnalysis,
  Document,
  Files,
  Plus,
  QuestionFilled,
  Refresh,
  Search,
  Upload,
  User,
  Warning,
} from '@element-plus/icons-vue'
import { useAdminDashboard } from '../adminDashboard'
import { adminApi, userFacingApiError } from '../api'
import { useAuthStore } from '../stores'
import { CHUNK_PAGE_SIZE, describeDiff, useKnowledgeConsole } from '../knowledgeConsole'
import type { AdminAIConfig, AdminContentGap, AdminKnowledgeDocument, AdminModel } from '../types'

const auth = useAuthStore()
const dashboard = useAdminDashboard()
const console_ = useKnowledgeConsole()

const overviewCards = [
  { key: 'user_count', label: '注册用户', icon: User },
  { key: 'active_model_count', label: '启用型号', icon: Connection },
  { key: 'published_flow_count', label: '已发布流程', icon: DataAnalysis },
  { key: 'knowledge_document_count', label: '知识文档', icon: Files },
  { key: 'knowledge_chunk_count', label: '知识分片', icon: Document },
  { key: 'safety_block_count', label: '安全阻断', icon: Warning },
  { key: 'unresolved_diagnostic_count', label: '未解决诊断', icon: CircleCloseFilled },
  { key: 'service_report_count', label: '售后报告', icon: CircleCheckFilled },
  { key: 'content_gap_count', label: '内容缺口（30天）', icon: QuestionFilled },
] as const

const refusalReasonLabels: Record<string, string> = {
  knowledge_gap: '资料缺口',
  model_refused: '模型拒答',
  citation_invalid: '引用校验失败',
  unsafe_answer: '不安全回答拦截',
}

function refusalReasonText(reason: string) {
  return refusalReasonLabels[reason] ?? reason
}

const refusalEntries = computed(() =>
  Object.entries(dashboard.overview.value.generation_stats.refusal_by_reason)
)

const uploadRef = ref<UploadInstance>()
const uploadModelCode = ref('')
const uploadSourceUrl = ref('')
const uploadFile = ref<File | null>(null)
const canUpload = computed(() =>
  Boolean(uploadModelCode.value && uploadSourceUrl.value.trim() && uploadFile.value)
)

function onUploadFileChange(file: UploadFile) {
  uploadFile.value = (file.raw as File | undefined) ?? null
}

function uploadForm() {
  const form = new FormData()
  form.append('model_code', uploadModelCode.value)
  form.append('source_url', uploadSourceUrl.value.trim())
  form.append('file', uploadFile.value as File)
  return form
}

async function submitUpload() {
  if (!canUpload.value || !uploadFile.value) return
  const outcome = await dashboard.uploadKnowledge(uploadForm())
  if (outcome.ok) {
    ElMessage.success(`知识已入库：${uploadModelCode.value} 新增 ${outcome.result?.chunk_count ?? 0} 个分片`)
    uploadFile.value = null
    uploadSourceUrl.value = ''
    uploadRef.value?.clearFiles()
    console_.clearPreview()
    await console_.loadDocuments()
  } else if (outcome.error) {
    ElMessage.error(outcome.error)
  }
}

async function submitPreview() {
  if (!canUpload.value || !uploadFile.value) return
  const outcome = await console_.runPreview(uploadForm())
  if (!outcome.ok && outcome.error) ElMessage.error(outcome.error)
}

const previewText = computed(() => (console_.preview.value ? describeDiff(console_.preview.value) : ''))

// --- 知识文档管理 ---

const changeKindLabels: Record<string, string> = {
  upload: '上传',
  reindex: '重新向量化',
  rollback: '版本回滚',
  release: '发布包',
}

function formatSize(bytes: number | null) {
  if (!bytes) return '—'
  return bytes >= 1024 * 1024 ? `${(bytes / 1024 / 1024).toFixed(1)} MB` : `${Math.round(bytes / 1024)} KB`
}

async function toggleDocument(document: AdminKnowledgeDocument) {
  const next = document.status === 'active' ? 'disabled' : 'active'
  if (next === 'disabled') {
    try {
      await ElMessageBox.confirm(
        `停用后《${document.title}》立即不再参与检索与回答，但内容和向量都会保留，可随时启用。`,
        '确认停用该文档？',
        { type: 'warning', confirmButtonText: '停用', cancelButtonText: '取消' },
      )
    } catch {
      return
    }
  }
  const result = await console_.setStatus(document, next)
  if (result.ok) {
    ElMessage.success(next === 'disabled' ? '已停用，该文档不再参与检索' : '已启用')
    await dashboard.load()
  } else if (result.error) {
    ElMessage.error(result.error)
  }
}

async function removeDocument(document: AdminKnowledgeDocument) {
  try {
    await ElMessageBox.confirm(
      `删除《${document.title}》会同时删除它的全部分片、向量与版本历史，且不可恢复。若只是想让它暂时不参与回答，请改用「停用」。`,
      '确认永久删除该文档？',
      { type: 'error', confirmButtonText: '永久删除', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  const result = await console_.remove(document)
  if (result.ok) {
    ElMessage.success('文档已删除')
    await dashboard.load()
  } else if (result.error) {
    ElMessage.error(result.error)
  }
}

async function reindexDocument(document: AdminKnowledgeDocument) {
  const result = await console_.reindex(document)
  if (result.ok) {
    ElMessage.success('已用存档原件重新向量化')
    await dashboard.load()
  } else if (result.error) {
    ElMessage.error(result.error)
  }
}

async function rollbackTo(version: number) {
  const document = console_.selected.value
  if (!document) return
  try {
    await ElMessageBox.confirm(
      `将用 v${version} 的原始文件重新入库并替换当前内容。版本号会继续向前（不会退回 v${version}），历史记录完整保留。`,
      `确认回滚到 v${version}？`,
      { type: 'warning', confirmButtonText: '回滚', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  const result = await console_.rollback(document, version)
  if (result.ok) {
    ElMessage.success(`已回滚到 v${version} 的内容`)
    await dashboard.load()
  } else if (result.error) {
    ElMessage.error(result.error)
  }
}

async function renameDocument(document: AdminKnowledgeDocument) {
  try {
    const { value } = await ElMessageBox.prompt('文档标题会展示在用户看到的引用来源里。', '修改标题', {
      inputValue: document.title,
      inputPattern: /\S+/,
      inputErrorMessage: '标题不能为空',
    })
    const result = await console_.rename(document, value.trim())
    if (result.ok) ElMessage.success('标题已更新')
    else if (result.error) ElMessage.error(result.error)
  } catch {
    /* 用户取消 */
  }
}

async function downloadDocument(document: AdminKnowledgeDocument) {
  const result = await console_.download(document)
  if (!result.ok && result.error) ElMessage.error(result.error)
}

const chunkRangeText = computed(() => {
  const page = console_.chunkPage.value
  if (!page || !page.total) return '暂无分片'
  const from = page.offset + 1
  const to = Math.min(page.offset + page.limit, page.total)
  return `第 ${from}–${to} 条，共 ${page.total} 条`
})

function nextChunks(step: number) {
  const page = console_.chunkPage.value
  if (!page) return
  const offset = Math.max(0, Math.min(page.offset + step * CHUNK_PAGE_SIZE, Math.max(0, page.total - 1)))
  console_.loadChunks(offset)
}

// --- 内容缺口闭环 ---

const gapStatusMeta: Record<string, { label: string; type: 'info' | 'warning' | 'success' | 'danger' }> = {
  open: { label: '待处理', type: 'danger' },
  investigating: { label: '处理中', type: 'warning' },
  resolved: { label: '已解决', type: 'success' },
  wont_fix: { label: '不处理', type: 'info' },
}

const replayStatusMeta: Record<string, { label: string; type: 'success' | 'danger' | 'warning' }> = {
  passed: { label: '复测通过', type: 'success' },
  failed: { label: '复测未通过', type: 'danger' },
  error: { label: '复测未完成', type: 'warning' },
}

function documentsForModel(modelId: AdminContentGap['robot_model_id']) {
  return console_.documents.value.filter((item) => item.robot_model_id === modelId)
}

async function linkGap(gap: AdminContentGap, documentId: number) {
  const result = await console_.linkGapDocument(gap, documentId)
  if (result.ok) {
    ElMessage.success('已关联文档，接下来可以点「复测」验证是否真的能答了')
    await dashboard.load()
  } else if (result.error) {
    ElMessage.error(result.error)
  }
}

async function replayGap(gap: AdminContentGap) {
  const result = await console_.replayGap(gap)
  if (!result.ok) {
    if (result.error) ElMessage.error(result.error)
    return
  }
  const outcome = console_.replayResult.value
  if (!outcome) return
  if (outcome.replay_status === 'passed') {
    ElMessage.success(`复测通过：已能引用 ${outcome.citation_count} 条资料作答，缺口标记为已解决`)
  } else if (outcome.replay_status === 'failed') {
    ElMessage.warning(`复测未通过：${outcome.detail}。资料可能未覆盖该问题，或切分后相似度不足`)
  } else {
    ElMessage.warning(`${outcome.detail}；这次没测成，缺口状态保持不变`)
  }
  await dashboard.load()
}

async function markGap(gap: AdminContentGap, status: string) {
  const result = await console_.setGapStatus(gap, status)
  if (result.ok) {
    ElMessage.success('缺口状态已更新')
    await dashboard.load()
  } else if (result.error) {
    ElMessage.error(result.error)
  }
}

function formatTime(value?: string) {
  if (!value) return '—'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString('zh-CN', { hour12: false })
}

function detailText(value: Record<string, unknown>) {
  const entries = Object.entries(value || {})
  if (!entries.length) return '—'
  return entries.map(([key, item]) => `${key}: ${typeof item === 'object' ? JSON.stringify(item) : String(item)}`).join('；')
}

const feedbackReasonLabels: Record<string, string> = {
  off_topic: '答非所问',
  unclear_steps: '步骤不清楚',
  wrong_citation: '引用不对',
  wrong_model: '型号不对',
  still_unresolved: '按做了仍没解决',
  unspecified: '未选原因',
}

function feedbackReasonText(reason: string) {
  return feedbackReasonLabels[reason] ?? reason
}

const conversationDialogVisible = ref(false)

async function searchConversations() {
  const result = await dashboard.loadConversations()
  if (!result.ok && result.error) ElMessage.error(result.error)
}

async function openConversation(id: string | number) {
  const result = await dashboard.openConversation(id)
  if (result.ok) conversationDialogVisible.value = true
  else if (result.error) ElMessage.error(result.error)
}

/** 从一条差评直接跳到那次对话——聚合数字要有落点才有用。 */
async function openFeedbackConversation(conversationId: string | number) {
  await openConversation(conversationId)
}

async function filterFeedback(reason: string) {
  dashboard.feedbackReason.value = dashboard.feedbackReason.value === reason ? '' : reason
  const result = await dashboard.loadFeedback()
  if (!result.ok && result.error) ElMessage.error(result.error)
}

const createModelVisible = ref(false)
const newModel = ref({ code: '', name: '', brand: '海尔' })

function openCreateModel() {
  newModel.value = { code: '', name: '', brand: '海尔' }
  createModelVisible.value = true
}

async function submitCreateModel() {
  const code = newModel.value.code.trim()
  const name = newModel.value.name.trim()
  const brand = newModel.value.brand.trim() || '海尔'
  if (!code || !name) {
    ElMessage.warning('型号编码与名称都要填')
    return
  }
  const result = await dashboard.createModel({ code, name, brand })
  if (result.ok) {
    createModelVisible.value = false
    ElMessage.success(`型号 ${code} 已创建，用户端立即可选；下一步给它上传说明书`)
  } else if (result.error) {
    ElMessage.error(result.error)
  }
}

async function updateModel(model: AdminModel, value: string | number | boolean) {
  const result = await dashboard.setModelActive(model, Boolean(value))
  if (result.ok) {
    ElMessage.success(`${model.code} 已${model.active ? '启用' : '停用'}`)
  } else if (result.error) {
    ElMessage.error(result.error)
  }
}

// --- AI 服务配置（仅 admin）---

const aiConfig = ref<AdminAIConfig | null>(null)
const aiLoading = ref(false)
const aiSaving = ref(false)
const aiTesting = ref(false)
const aiError = ref('')
const secureAISecretTransport = computed(() =>
  window.isSecureContext || ['localhost', '127.0.0.1', '::1'].includes(window.location.hostname),
)
const aiForm = reactive({
  llm_backend: 'openai-compat' as 'dashscope' | 'openai-compat',
  generation_model: '',
  generation_base_url: '',
  embedding_base_url: '',
  generation_api_key: '',
  embedding_api_key: '',
  reuse_generation_key_for_embedding: true,
})

function fillAIForm(value: AdminAIConfig) {
  aiConfig.value = value
  aiForm.llm_backend = value.llm_backend
  aiForm.generation_model = value.generation_model
  aiForm.generation_base_url = value.generation_base_url ?? ''
  aiForm.embedding_base_url = value.embedding_base_url ?? ''
  aiForm.generation_api_key = ''
  aiForm.embedding_api_key = ''
}

function aiConfigSourceLabel(source: AdminAIConfig['source'] | undefined) {
  if (source === 'database') return '管理员后台（含密钥）'
  if (source === 'mixed') return '后台参数 + 服务器密钥'
  return '服务器环境变量'
}

async function loadAIConfig() {
  if (!auth.isAdmin) return
  aiLoading.value = true
  aiError.value = ''
  try {
    fillAIForm(await adminApi.aiConfig())
  } catch (error) {
    aiError.value = userFacingApiError(error, 'AI 服务配置加载失败')
  } finally {
    aiLoading.value = false
  }
}

async function saveAIConfig() {
  if (!secureAISecretTransport.value) {
    ElMessage.warning('当前页面不是 HTTPS，禁止提交 API Key；请先配置域名和 HTTPS')
    return
  }
  if (!aiForm.generation_model.trim()) {
    ElMessage.warning('请填写生成模型 ID')
    return
  }
  if (aiForm.llm_backend === 'openai-compat' && !aiForm.generation_base_url.trim()) {
    ElMessage.warning('OpenAI 兼容模式必须填写生成 Base URL')
    return
  }
  aiSaving.value = true
  aiError.value = ''
  try {
    const value = await adminApi.updateAIConfig({
      llm_backend: aiForm.llm_backend,
      generation_model: aiForm.generation_model.trim(),
      generation_base_url: aiForm.generation_base_url.trim() || null,
      embedding_base_url: aiForm.embedding_base_url.trim() || null,
      generation_api_key: aiForm.generation_api_key.trim() || undefined,
      embedding_api_key: aiForm.reuse_generation_key_for_embedding
        ? undefined
        : (aiForm.embedding_api_key.trim() || undefined),
      reuse_generation_key_for_embedding: aiForm.reuse_generation_key_for_embedding,
    })
    fillAIForm(value)
    ElMessage.success('AI 配置已加密保存')
  } catch (error) {
    aiError.value = userFacingApiError(error, 'AI 配置保存失败')
  } finally {
    aiSaving.value = false
  }
}

async function toggleAIService() {
  if (!aiConfig.value) return
  const next = !aiConfig.value.enabled
  if (!next) {
    try {
      await ElMessageBox.confirm(
        '停用后，智能客服、智能回答、资料检索和知识库向量化会立即停止调用外部模型。',
        '确认停用 AI 服务？',
        { type: 'warning', confirmButtonText: '停用', cancelButtonText: '取消' },
      )
    } catch {
      return
    }
  }
  aiSaving.value = true
  aiError.value = ''
  try {
    fillAIForm(await adminApi.setAIEnabled(next))
    ElMessage.success(next ? 'AI 服务已启用，无需重启' : 'AI 服务已停用')
  } catch (error) {
    aiError.value = userFacingApiError(error, next ? 'AI 服务启用失败' : 'AI 服务停用失败')
  } finally {
    aiSaving.value = false
  }
}

async function testAIConnection() {
  try {
    await ElMessageBox.confirm(
      '连接测试会真实调用一次向量模型和一次生成模型，可能产生少量 API 费用。',
      '执行付费连接测试？',
      { type: 'warning', confirmButtonText: '开始测试', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  aiTesting.value = true
  aiError.value = ''
  try {
    const result = await adminApi.testAIConfig()
    if (result.embedding_service && result.generation_service) {
      ElMessage.success('连接成功：向量模型与生成模型均可用')
    } else {
      aiError.value = result.errors.join('；') || '连接测试未全部通过'
    }
  } catch (error) {
    aiError.value = userFacingApiError(error, 'AI 连接测试失败')
  } finally {
    aiTesting.value = false
  }
}

onMounted(async () => {
  await Promise.all([
    dashboard.load(),
    dashboard.loadFeedback(),
    dashboard.loadConversations(),
    console_.loadDocuments(),
    loadAIConfig(),
  ])
})
</script>

<template>
  <div class="page admin-page" v-loading="dashboard.loading.value">
    <div class="page-head">
      <div>
        <p class="eyebrow">ADMIN OPERATIONS</p>
        <h1>运营与安全控制台</h1>
        <p>数据来自后端管理员接口。前端路由只改善访问体验，最终权限由后端 RBAC 校验。</p>
      </div>
      <el-button :icon="Refresh" :loading="dashboard.loading.value" @click="dashboard.load">刷新数据</el-button>
    </div>

    <el-alert
      v-if="dashboard.loadError.value"
      class="load-alert"
      :title="dashboard.loadError.value"
      type="error"
      :closable="false"
      show-icon
    >
      <template #default>
        <el-button size="small" type="danger" plain @click="dashboard.load">重新加载</el-button>
      </template>
    </el-alert>

    <template v-if="dashboard.loaded.value">
      <section class="overview-grid" aria-label="运营概览">
        <article v-for="card in overviewCards" :key="card.key" class="panel metric-card">
          <el-icon><component :is="card.icon" /></el-icon>
          <span>
            <b>{{ dashboard.overview.value[card.key] }}</b>
            <small>{{ card.label }}</small>
          </span>
        </article>
      </section>

      <section v-if="auth.isAdmin" class="panel section-panel ai-config-panel" v-loading="aiLoading">
        <div class="section-head">
          <div>
            <h2>AI 服务配置</h2>
            <p>密钥加密保存在数据库中，保存后不会再次返回明文；启停立即生效，无需重启服务。</p>
          </div>
          <div class="section-actions">
            <el-tag :type="aiConfig?.runtime_ready ? 'success' : 'info'">
              {{ aiConfig?.runtime_ready ? '运行中' : '未运行' }}
            </el-tag>
            <el-button
              :type="aiConfig?.enabled ? 'danger' : 'primary'"
              plain
              :loading="aiSaving"
              :disabled="!aiConfig"
              @click="toggleAIService"
            >
              {{ aiConfig?.enabled ? '一键停用 API' : '一键启用 API' }}
            </el-button>
          </div>
        </div>

        <el-alert v-if="aiError" class="load-alert" :title="aiError" type="error" :closable="false" show-icon />
        <el-alert
          v-else-if="aiConfig && !aiConfig.encryption_ready"
          class="load-alert"
          title="服务器尚未配置 AI 密钥加密键，不能从后台保存新 API Key。"
          type="warning"
          :closable="false"
          show-icon
        />
        <el-alert
          v-else-if="!secureAISecretTransport"
          class="load-alert"
          title="当前页面不是 HTTPS，已禁止提交 API Key；请先配置域名和 HTTPS。"
          type="warning"
          :closable="false"
          show-icon
        />

        <el-form label-position="top" class="ai-config-form">
          <div class="ai-config-grid">
            <el-form-item label="生成接口协议">
              <el-select v-model="aiForm.llm_backend">
                <el-option label="OpenAI 兼容接口" value="openai-compat" />
                <el-option label="阿里云 DashScope SDK" value="dashscope" />
              </el-select>
            </el-form-item>
            <el-form-item label="生成模型 ID">
              <el-input v-model="aiForm.generation_model" placeholder="例如 qwen3.7-max" maxlength="100" />
            </el-form-item>
            <el-form-item label="生成 Base URL">
              <el-input v-model="aiForm.generation_base_url" placeholder="https://.../compatible-mode/v1" />
            </el-form-item>
            <el-form-item label="生成 API Key">
              <el-input
                v-model="aiForm.generation_api_key"
                type="password"
                show-password
                autocomplete="new-password"
                :placeholder="aiConfig?.generation_api_key_configured ? '已配置，留空保持不变' : '请输入 API Key'"
              />
            </el-form-item>
            <el-form-item label="向量模型">
              <el-input :model-value="aiConfig?.embedding_model ?? 'text-embedding-v4'" disabled />
            </el-form-item>
            <el-form-item label="向量 Base URL">
              <el-input v-model="aiForm.embedding_base_url" placeholder="https://.../api/v1" />
            </el-form-item>
            <el-form-item label="向量 API Key">
              <el-input
                v-model="aiForm.embedding_api_key"
                type="password"
                show-password
                autocomplete="new-password"
                :disabled="aiForm.reuse_generation_key_for_embedding"
                :placeholder="aiConfig?.embedding_api_key_configured ? '已配置，留空保持不变' : '请输入 API Key'"
              />
            </el-form-item>
            <el-form-item label="密钥复用">
              <el-checkbox v-model="aiForm.reuse_generation_key_for_embedding">向量模型使用本次填写的生成 API Key</el-checkbox>
            </el-form-item>
          </div>
          <div class="ai-config-actions">
            <span>
              配置来源：{{ aiConfigSourceLabel(aiConfig?.source) }}；保存配置不会自动执行付费测试。
            </span>
            <div class="section-actions">
              <el-button :loading="aiTesting" :disabled="!aiConfig" @click="testAIConnection">连接测试</el-button>
              <el-button type="primary" :loading="aiSaving" :disabled="!aiConfig?.encryption_ready || !secureAISecretTransport" @click="saveAIConfig">加密保存配置</el-button>
            </div>
          </div>
        </el-form>
      </section>

      <section class="panel section-panel generation-stats" aria-label="生成回答统计">
        <div class="section-head">
          <div>
            <h2>智能回答运营（近 30 天）</h2>
            <p>回答与拒答来自 generation_records 留痕；拒答原因帮助判断内容缺口和模型质量。</p>
          </div>
          <el-tag type="info">
            回答 {{ dashboard.overview.value.generation_stats.answered_count }} ·
            拒答 {{ dashboard.overview.value.generation_stats.refused_count }}
          </el-tag>
        </div>
        <div class="refusal-tags">
          <template v-if="refusalEntries.length">
            <el-tag v-for="[reason, reasonCount] in refusalEntries" :key="reason" type="warning" size="small">
              {{ refusalReasonText(reason) }}：{{ reasonCount }}
            </el-tag>
          </template>
          <span v-else class="refusal-empty">近 30 天没有拒答记录。</span>
        </div>
      </section>

      <!-- token 成本基于 provider 返回的 usage 数据统计。 -->
      <section class="panel section-panel cost-panel">
        <div class="section-head">
          <div>
            <h2>模型用量与成本（近 {{ dashboard.overview.value.token_cost.window_days }} 天）</h2>
            <p>
              按配置单价估算。用量覆盖 {{ dashboard.overview.value.token_cost.records_with_usage }}
              / {{ dashboard.overview.value.token_cost.total_records }} 次调用，
              <strong v-if="dashboard.overview.value.token_cost.records_with_usage < dashboard.overview.value.token_cost.total_records">
                未覆盖部分不计入，实际成本高于此处显示
              </strong>
              <span v-else>覆盖完整</span>。
            </p>
          </div>
        </div>
        <div class="cost-grid">
          <div><b>{{ dashboard.overview.value.token_cost.prompt_tokens.toLocaleString() }}</b><small>输入 token</small></div>
          <div><b>{{ dashboard.overview.value.token_cost.completion_tokens.toLocaleString() }}</b><small>输出 token</small></div>
          <div><b>¥{{ dashboard.overview.value.token_cost.estimated_cost.toFixed(4) }}</b><small>估算成本</small></div>
        </div>
        <div v-if="Object.keys(dashboard.overview.value.token_cost.by_model).length" class="refusal-tags">
          <el-tag v-for="(cost, model) in dashboard.overview.value.token_cost.by_model" :key="model" type="info" effect="plain">
            {{ model }}：¥{{ cost.toFixed(4) }}
          </el-tag>
        </div>
      </section>

      <div class="two-column">
        <!-- 反馈：reason 枚举本就是为按原因聚合设计的，此前全库没有读取入口 -->
        <section class="panel section-panel">
          <div class="section-head">
            <div>
              <h2>用户反馈（近 {{ dashboard.feedback.value?.window_days ?? 30 }} 天）</h2>
              <p>点原因可筛选；点一条差评直接查看那次对话。</p>
            </div>
            <el-tag :type="(dashboard.feedback.value?.unhelpful_count ?? 0) > 0 ? 'danger' : 'success'" effect="plain">
              有帮助 {{ dashboard.feedback.value?.helpful_count ?? 0 }} / 共 {{ dashboard.feedback.value?.total_count ?? 0 }}
            </el-tag>
          </div>
          <div class="refusal-tags">
            <el-tag
              v-for="(count, reason) in dashboard.feedback.value?.by_reason || {}"
              :key="reason"
              class="clickable-tag"
              :type="dashboard.feedbackReason.value === reason ? 'danger' : 'info'"
              :effect="dashboard.feedbackReason.value === reason ? 'dark' : 'plain'"
              @click="filterFeedback(String(reason))"
            >{{ feedbackReasonText(String(reason)) }} · {{ count }}</el-tag>
            <span v-if="!Object.keys(dashboard.feedback.value?.by_reason || {}).length" class="refusal-empty">
              暂无差评反馈
            </span>
          </div>
          <el-table
            class="table-section"
            :data="dashboard.feedback.value?.items || []"
            v-loading="dashboard.feedbackLoading.value"
            empty-text="暂无差评明细"
          >
            <el-table-column prop="model_code" label="型号" width="90" />
            <el-table-column label="原因" width="120">
              <template #default="{ row }">{{ feedbackReasonText(row.reason || 'unspecified') }}</template>
            </el-table-column>
            <el-table-column prop="answer_excerpt" label="回答摘要" min-width="200" show-overflow-tooltip />
            <el-table-column label="操作" width="90" align="center">
              <template #default="{ row }">
                <el-button link type="primary" @click="openFeedbackConversation(row.conversation_id)">查看对话</el-button>
              </template>
            </el-table-column>
          </el-table>
        </section>

        <!-- 会话检索：客诉"机器人让我拆电池"此前在后台根本找不到那次对话 -->
        <section class="panel section-panel">
          <div class="section-head">
            <div>
              <h2>会话检索</h2>
              <p>按回答正文或标题搜索；打开对话会写入审计。</p>
            </div>
            <div class="section-actions">
              <el-input
                v-model="dashboard.conversationSearch.value"
                size="small"
                placeholder="搜回答里的一句话"
                clearable
                class="filter-select"
                @keyup.enter="searchConversations"
              />
              <el-button size="small" :icon="Search" :loading="dashboard.conversationsLoading.value" @click="searchConversations">搜索</el-button>
            </div>
          </div>
          <el-table
            :data="dashboard.conversations.value"
            v-loading="dashboard.conversationsLoading.value"
            empty-text="没有匹配的会话"
          >
            <el-table-column prop="model_code" label="型号" width="90" />
            <el-table-column prop="title" label="标题" min-width="150" show-overflow-tooltip />
            <el-table-column prop="user_email_masked" label="用户" width="140" />
            <el-table-column prop="message_count" label="消息" width="70" align="center" />
            <el-table-column label="操作" width="80" align="center">
              <template #default="{ row }">
                <el-button link type="primary" @click="openConversation(row.id)">查看</el-button>
              </template>
            </el-table-column>
          </el-table>
        </section>
      </div>

      <div class="two-column">
        <section class="panel section-panel">
          <div class="section-head">
            <div>
              <h2>型号管理</h2>
              <p>新增型号立即对用户可见，无需重建镜像；停用后不再面向新诊断开放，历史数据仍保留。</p>
            </div>
            <div class="section-head-actions">
              <el-tag type="info">{{ dashboard.models.value.length }} 个型号</el-tag>
              <el-button
                class="brand-button"
                type="primary"
                size="small"
                :icon="Plus"
                @click="openCreateModel"
              >新增型号</el-button>
            </div>
          </div>
          <el-table :data="dashboard.models.value" empty-text="暂无型号数据">
            <el-table-column prop="code" label="型号" width="120" />
            <el-table-column prop="name" label="名称" min-width="180" />
            <el-table-column prop="brand" label="品牌" width="100" />
            <el-table-column label="状态" width="120" align="center">
              <template #default="{ row }">
                <el-switch
                  :model-value="row.active"
                  :loading="dashboard.isModelUpdating(row.id)"
                  :disabled="dashboard.isModelUpdating(row.id)"
                  inline-prompt
                  active-text="启用"
                  inactive-text="停用"
                  @change="(value: string | number | boolean) => updateModel(row as AdminModel, value)"
                />
              </template>
            </el-table-column>
          </el-table>
        </section>

        <section class="panel section-panel">
          <div class="section-head">
            <div>
              <h2>知识库健康</h2>
              <p>文档、分片和向量数量必须可追溯且保持一致。</p>
            </div>
            <el-tag :type="dashboard.knowledgeHealthy.value ? 'success' : 'danger'">
              {{ dashboard.knowledgeHealthy.value ? '健康' : '需检查' }}
            </el-tag>
          </div>
          <el-table :data="dashboard.knowledgeStatus.value" empty-text="未获取到知识状态">
            <el-table-column prop="model_code" label="型号" min-width="100" />
            <el-table-column prop="document_count" label="文档" width="75" align="right" />
            <el-table-column prop="chunk_count" label="分片" width="75" align="right" />
            <el-table-column prop="vector_count" label="向量" width="75" align="right" />
            <el-table-column label="一致性" width="90" align="center">
              <template #default="{ row }">
                <el-tag size="small" :type="row.chunk_count > 0 && row.chunk_count === row.vector_count ? 'success' : 'danger'">
                  {{ row.chunk_count > 0 && row.chunk_count === row.vector_count ? '一致' : '异常' }}
                </el-tag>
              </template>
            </el-table-column>
          </el-table>
          <div class="upload-box">
            <h3>上传知识 PDF</h3>
            <p>校验、切分与向量化由后端完成；同一来源 URL 重复上传会整体替换旧内容。</p>
            <div class="upload-row">
              <el-select v-model="uploadModelCode" placeholder="选择型号" size="small" class="upload-model">
                <el-option v-for="item in dashboard.models.value" :key="item.code" :label="item.code" :value="item.code" />
              </el-select>
              <el-input v-model="uploadSourceUrl" size="small" placeholder="来源 URL（该型号内唯一，如官方下载地址）" />
            </div>
            <div class="upload-row">
              <el-upload
                ref="uploadRef"
                :auto-upload="false"
                :limit="1"
                accept=".pdf"
                :on-change="onUploadFileChange"
                :on-remove="() => (uploadFile = null)"
              >
                <el-button size="small">选择 PDF 文件</el-button>
              </el-upload>
              <el-button
                size="small"
                :loading="console_.previewing.value"
                :disabled="!canUpload"
                @click="submitPreview"
              >
                预览差异
              </el-button>
              <el-button
                size="small"
                type="primary"
                :icon="Upload"
                :loading="dashboard.uploadingKnowledge.value"
                :disabled="!canUpload"
                @click="submitUpload"
              >
                上传并入库
              </el-button>
            </div>
            <el-alert
              v-if="console_.preview.value"
              class="preview-alert"
              :type="console_.preview.value.status === 'identical' ? 'info' : 'warning'"
              :closable="true"
              show-icon
              @close="console_.clearPreview()"
            >
              <template #title>
                {{ console_.preview.value.status === 'new' ? '新增文档' : console_.preview.value.status === 'identical' ? '内容无变化' : '将覆盖线上内容' }}
              </template>
              <p class="preview-text">{{ previewText }}</p>
              <p class="preview-meta">
                新文件 SHA256：{{ console_.preview.value.incoming_sha256.slice(0, 16) }}…
                <span v-if="console_.preview.value.current_sha256">
                  ／线上：{{ console_.preview.value.current_sha256.slice(0, 16) }}…
                </span>
              </p>
            </el-alert>
          </div>
        </section>
      </div>

      <section class="panel section-panel table-section">
        <div class="section-head">
          <div>
            <h2>知识文档管理</h2>
            <p>停用只是把文档挡在检索之外（内容保留、可恢复）；删除会连同分片、向量与版本历史一并清除，不可恢复。</p>
          </div>
          <div class="section-actions">
            <el-select v-model="console_.modelFilter.value" placeholder="全部型号" size="small" clearable class="filter-select" @change="console_.loadDocuments()">
              <el-option v-for="item in dashboard.models.value" :key="item.code" :label="item.code" :value="item.code" />
            </el-select>
            <el-select v-model="console_.statusFilter.value" placeholder="全部状态" size="small" clearable class="filter-select" @change="console_.loadDocuments()">
              <el-option label="启用中" value="active" />
              <el-option label="已停用" value="disabled" />
            </el-select>
            <el-button size="small" :icon="Refresh" :loading="console_.loading.value" @click="console_.loadDocuments()">刷新</el-button>
          </div>
        </div>
        <el-alert
          v-if="console_.missingArchiveCount.value > 0"
          type="info"
          :closable="false"
          show-icon
          class="archive-hint"
          :title="`有 ${console_.missingArchiveCount.value} 份文档没有原件存档（存档功能上线前入库），无法重新向量化、回滚或下载原件；重新上传同一份 PDF 即可补齐。`"
        />
        <el-table :data="console_.documents.value" empty-text="暂无知识文档" v-loading="console_.loading.value">
          <el-table-column prop="model_code" label="型号" width="100" />
          <el-table-column prop="title" label="标题" min-width="180" show-overflow-tooltip />
          <el-table-column label="版本" width="80" align="center">
            <template #default="{ row }">v{{ (row as AdminKnowledgeDocument).version }}</template>
          </el-table-column>
          <el-table-column label="发布时间" width="170">
            <template #default="{ row }">{{ formatTime((row as AdminKnowledgeDocument).updated_at) }}</template>
          </el-table-column>
          <el-table-column label="页/片/量" width="120" align="center">
            <template #default="{ row }">
              {{ (row as AdminKnowledgeDocument).page_count }}/{{ (row as AdminKnowledgeDocument).chunk_count }}/{{ (row as AdminKnowledgeDocument).vector_count }}
            </template>
          </el-table-column>
          <el-table-column label="SHA256" width="130">
            <template #default="{ row }">
              <span class="mono" :title="(row as AdminKnowledgeDocument).sha256">{{ (row as AdminKnowledgeDocument).sha256.slice(0, 12) }}…</span>
            </template>
          </el-table-column>
          <el-table-column label="状态" width="95" align="center">
            <template #default="{ row }">
              <el-tag size="small" :type="(row as AdminKnowledgeDocument).status === 'active' ? 'success' : 'info'">
                {{ (row as AdminKnowledgeDocument).status === 'active' ? '启用中' : '已停用' }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="操作" min-width="300" align="right">
            <template #default="{ row }">
              <el-button link type="primary" size="small" @click="console_.openDocument((row as AdminKnowledgeDocument).id)">分片/版本</el-button>
              <el-button link size="small" :disabled="console_.isBusy((row as AdminKnowledgeDocument).id)" @click="renameDocument(row as AdminKnowledgeDocument)">改名</el-button>
              <el-button
                link
                size="small"
                :disabled="!(row as AdminKnowledgeDocument).has_archived_file"
                :title="(row as AdminKnowledgeDocument).has_archived_file ? '' : '无原件存档'"
                @click="downloadDocument(row as AdminKnowledgeDocument)"
              >
                原件
              </el-button>
              <el-button
                v-if="auth.canManageKnowledge"
                link
                size="small"
                :loading="console_.isBusy((row as AdminKnowledgeDocument).id)"
                :disabled="!(row as AdminKnowledgeDocument).has_archived_file"
                @click="reindexDocument(row as AdminKnowledgeDocument)"
              >
                重新向量化
              </el-button>
              <el-button v-if="auth.canManageKnowledge" link size="small" :disabled="console_.isBusy((row as AdminKnowledgeDocument).id)" @click="toggleDocument(row as AdminKnowledgeDocument)">
                {{ (row as AdminKnowledgeDocument).status === 'active' ? '停用' : '启用' }}
              </el-button>
              <!-- 删除与回滚不可逆，只给 admin；viewer/operator 看得到内容但动不了 -->
              <el-button v-if="auth.isAdmin" link type="danger" size="small" :disabled="console_.isBusy((row as AdminKnowledgeDocument).id)" @click="removeDocument(row as AdminKnowledgeDocument)">删除</el-button>
            </template>
          </el-table-column>
        </el-table>
      </section>

      <section class="panel section-panel table-section">
        <div class="section-head">
          <div>
            <h2>内容缺口榜（近 30 天）</h2>
            <p>检索无结果与资料缺口拒答的高频查询聚合；只含归一化查询与型号，不含用户信息。</p>
          </div>
          <el-tag type="warning">Top {{ dashboard.contentGaps.value.length }}</el-tag>
        </div>
        <p class="gap-loop-hint">
          处理闭环：关联刚上传的官方资料 → 点「复测」把原问题原样再问一遍 → 能引用作答才自动标记为已解决。
          复测不通过会把「已解决」打回处理中，避免缺口榜自欺欺人。
        </p>
        <el-table :data="dashboard.contentGaps.value" empty-text="近 30 天没有内容缺口记录">
          <el-table-column prop="query_normalized" label="查询" min-width="220" show-overflow-tooltip />
          <el-table-column prop="count" label="次数" width="70" align="right" />
          <el-table-column prop="model_code" label="型号" width="100" />
          <el-table-column label="状态" width="100" align="center">
            <template #default="{ row }">
              <el-tag size="small" :type="gapStatusMeta[(row as AdminContentGap).status].type">
                {{ gapStatusMeta[(row as AdminContentGap).status].label }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="关联资料" min-width="170">
            <template #default="{ row }">
              <span v-if="(row as AdminContentGap).linked_document_title" class="linked-doc">
                {{ (row as AdminContentGap).linked_document_title }}
              </span>
              <el-select
                v-else
                size="small"
                placeholder="选择该型号文档"
                class="gap-doc-select"
                @change="(value: number) => linkGap(row as AdminContentGap, value)"
              >
                <el-option
                  v-for="item in documentsForModel((row as AdminContentGap).robot_model_id)"
                  :key="item.id"
                  :label="item.title"
                  :value="item.id"
                />
              </el-select>
            </template>
          </el-table-column>
          <el-table-column label="复测" min-width="150">
            <template #default="{ row }">
              <div v-if="(row as AdminContentGap).replay_status" class="replay-cell">
                <el-tag size="small" :type="replayStatusMeta[(row as AdminContentGap).replay_status!].type">
                  {{ replayStatusMeta[(row as AdminContentGap).replay_status!].label }}
                </el-tag>
                <span class="replay-meta">
                  {{ (row as AdminContentGap).replay_citation_count ?? 0 }} 条引用 ·
                  {{ formatTime((row as AdminContentGap).replay_checked_at ?? undefined) }}
                </span>
                <p v-if="(row as AdminContentGap).replay_answer_excerpt" class="replay-excerpt">
                  {{ (row as AdminContentGap).replay_answer_excerpt }}
                </p>
              </div>
              <span v-else class="muted">未复测</span>
            </template>
          </el-table-column>
          <el-table-column label="最近发生" width="160">
            <template #default="{ row }">{{ formatTime((row as AdminContentGap).last_seen_at) }}</template>
          </el-table-column>
          <el-table-column label="操作" width="170" align="right">
            <template #default="{ row }">
              <el-button
                link
                type="primary"
                size="small"
                :loading="console_.isReplaying(row as AdminContentGap)"
                @click="replayGap(row as AdminContentGap)"
              >
                复测
              </el-button>
              <el-button
                v-if="(row as AdminContentGap).status !== 'wont_fix'"
                link
                size="small"
                @click="markGap(row as AdminContentGap, 'wont_fix')"
              >
                不处理
              </el-button>
              <el-button v-else link size="small" @click="markGap(row as AdminContentGap, 'open')">重新打开</el-button>
            </template>
          </el-table-column>
        </el-table>
      </section>

      <el-drawer
        :model-value="console_.selected.value !== null"
        :title="console_.selected.value ? `${console_.selected.value.title}（v${console_.selected.value.version}）` : ''"
        size="640px"
        @close="console_.closeDocument()"
      >
        <div v-if="console_.selected.value" v-loading="console_.detailLoading.value" class="doc-detail">
          <dl class="doc-meta">
            <div><dt>型号</dt><dd>{{ console_.selected.value.model_code }}</dd></div>
            <div><dt>来源</dt><dd class="wrap">{{ console_.selected.value.source_url }}</dd></div>
            <div><dt>SHA256</dt><dd class="mono wrap">{{ console_.selected.value.sha256 }}</dd></div>
            <div><dt>页数 / 分片</dt><dd>{{ console_.selected.value.page_count }} 页 · {{ console_.selected.value.chunk_count }} 片</dd></div>
            <div><dt>向量模型</dt><dd>{{ console_.selected.value.embedding_model ?? '—' }}</dd></div>
            <div><dt>文件大小</dt><dd>{{ formatSize(console_.selected.value.file_size) }}</dd></div>
          </dl>

          <h4>版本历史</h4>
          <el-table :data="console_.selected.value.versions" size="small" empty-text="暂无版本记录">
            <el-table-column label="版本" width="70">
              <template #default="{ row }">v{{ row.version }}</template>
            </el-table-column>
            <el-table-column label="类型" width="100">
              <template #default="{ row }">{{ changeKindLabels[row.change_kind] ?? row.change_kind }}</template>
            </el-table-column>
            <el-table-column label="页/片" width="80">
              <template #default="{ row }">{{ row.page_count }}/{{ row.chunk_count }}</template>
            </el-table-column>
            <el-table-column label="时间" width="150">
              <template #default="{ row }">{{ formatTime(row.created_at) }}</template>
            </el-table-column>
            <el-table-column label="操作" width="90" align="right">
              <template #default="{ row }">
                <el-button
                  v-if="row.sha256 !== console_.selected.value?.sha256 && auth.isAdmin"
                  link
                  type="primary"
                  size="small"
                  :disabled="!row.has_archived_file"
                  :title="row.has_archived_file ? '' : '该版本没有留存原件，无法回滚'"
                  @click="rollbackTo(row.version)"
                >
                  回滚
                </el-button>
                <el-tag v-else size="small" type="success">当前</el-tag>
              </template>
            </el-table-column>
          </el-table>

          <div class="chunk-head">
            <h4>分片内容</h4>
            <div class="chunk-controls">
              <el-input-number
                :model-value="console_.chunkPageFilter.value ?? undefined"
                size="small"
                :min="1"
                :max="console_.selected.value.page_count"
                placeholder="按页码"
                controls-position="right"
                class="page-filter"
                @change="(value: number | undefined) => console_.loadChunks(0, value ?? null)"
              />
              <span class="chunk-range">{{ chunkRangeText }}</span>
              <el-button size="small" :disabled="console_.chunkOffset.value === 0" @click="nextChunks(-1)">上一页</el-button>
              <el-button
                size="small"
                :disabled="!console_.chunkPage.value || console_.chunkOffset.value + CHUNK_PAGE_SIZE >= console_.chunkPage.value.total"
                @click="nextChunks(1)"
              >
                下一页
              </el-button>
            </div>
          </div>
          <ul class="chunk-list">
            <li v-for="chunk in console_.chunkPage.value?.items ?? []" :key="chunk.chunk_index">
              <div class="chunk-head-row">
                <span class="chunk-index">#{{ chunk.chunk_index }}</span>
                <el-tag size="small">第 {{ chunk.page_number }} 页</el-tag>
                <el-tag v-if="!chunk.has_embedding" size="small" type="danger">无向量</el-tag>
              </div>
              <p class="chunk-text">{{ chunk.content }}</p>
            </li>
          </ul>
        </div>
      </el-drawer>

      <section class="panel section-panel table-section">
        <div class="section-head">
          <div>
            <h2>最近安全阻断</h2>
            <p>这里只展示结构化阻断原因，不展示用户原始描述或图片内容。</p>
          </div>
          <el-tag type="danger">最近 {{ dashboard.safetyBlocks.value.length }} 条</el-tag>
        </div>
        <el-table :data="dashboard.safetyBlocks.value" empty-text="暂无安全阻断记录">
          <el-table-column prop="created_at" label="时间" width="170">
            <template #default="{ row }">{{ formatTime(row.created_at) }}</template>
          </el-table-column>
          <el-table-column prop="model_code" label="型号" width="110" />
          <el-table-column prop="category" label="类别" width="130" />
          <el-table-column prop="risk_level" label="风险" width="90">
            <template #default="{ row }"><el-tag type="danger" size="small">{{ row.risk_level }}</el-tag></template>
          </el-table-column>
          <el-table-column label="敏感详情" width="120" align="center">
            <template #default="{ row }">
              <el-button link type="primary" @click="dashboard.openSafetyBlock(row.id)">查看详情</el-button>
            </template>
          </el-table-column>
        </el-table>
      </section>

      <section class="panel section-panel table-section">
        <div class="section-head">
          <div>
            <h2>未解决诊断与售后报告</h2>
            <p>帮助运营人员确认未解决会话是否已经形成可交付报告。</p>
          </div>
          <el-tag type="warning">最近 {{ dashboard.unresolvedReports.value.length }} 条</el-tag>
        </div>
        <el-table :data="dashboard.unresolvedReports.value" empty-text="暂无未解决报告">
          <el-table-column prop="report.created_at" label="报告时间" width="170">
            <template #default="{ row }">{{ formatTime(row.report.created_at) }}</template>
          </el-table-column>
          <el-table-column prop="report.report_number" label="报告编号" min-width="150" />
          <el-table-column prop="model.code" label="型号" width="110" />
          <el-table-column label="敏感详情" min-width="180" align="center">
            <template #default="{ row }">
              <el-button link type="primary" @click="dashboard.openDiagnostic(row.diagnostic.id)">诊断详情</el-button>
              <el-button link type="primary" @click="dashboard.openReport(row.report.id)">报告详情</el-button>
            </template>
          </el-table-column>
          <el-table-column prop="user.email_masked" label="用户（脱敏）" min-width="190" show-overflow-tooltip />
        </el-table>
      </section>

      <section class="panel section-panel table-section">
        <div class="section-head">
          <div>
            <h2>管理员审计日志</h2>
            <p>型号启停等管理操作由后端写入审计记录。</p>
          </div>
          <el-tag type="info">最近 {{ dashboard.auditLogs.value.length }} 条</el-tag>
        </div>
        <el-table :data="dashboard.auditLogs.value" empty-text="暂无审计日志">
          <el-table-column prop="created_at" label="时间" width="170">
            <template #default="{ row }">{{ formatTime(row.created_at) }}</template>
          </el-table-column>
          <el-table-column prop="actor_user_id" label="操作人 ID" width="105" />
          <el-table-column prop="action" label="动作" width="140" />
          <el-table-column prop="resource_type" label="资源类型" width="130" />
          <el-table-column prop="resource_id" label="资源 ID" width="100" />
          <el-table-column label="安全详情" min-width="280" show-overflow-tooltip>
            <template #default="{ row }">{{ detailText(row.details_json) }}</template>
          </el-table-column>
        </el-table>
      </section>

      <el-dialog
        :model-value="Boolean(dashboard.selectedSafetyBlock.value)"
        title="安全阻断详情（访问已审计）"
        width="620px"
        @close="dashboard.selectedSafetyBlock.value = null"
      >
        <div v-if="dashboard.selectedSafetyBlock.value" class="sensitive-detail">
          <p><b>型号：</b>{{ dashboard.selectedSafetyBlock.value.model_code }}</p>
          <p><b>阻断原因：</b>{{ dashboard.selectedSafetyBlock.value.reason }}</p>
          <p><b>安全建议：</b>{{ dashboard.selectedSafetyBlock.value.advice }}</p>
        </div>
      </el-dialog>

      <el-dialog
        :model-value="Boolean(dashboard.selectedDiagnostic.value)"
        title="诊断详情（访问已审计）"
        width="620px"
        @close="dashboard.selectedDiagnostic.value = null"
      >
        <div v-if="dashboard.selectedDiagnostic.value" class="sensitive-detail">
          <p><b>问题描述：</b>{{ dashboard.selectedDiagnostic.value.issue_description }}</p>
          <p><b>错误码：</b>{{ dashboard.selectedDiagnostic.value.error_code || '—' }}</p>
          <p><b>状态：</b>{{ dashboard.selectedDiagnostic.value.status }}</p>
        </div>
      </el-dialog>

      <el-dialog
        :model-value="Boolean(dashboard.selectedReport.value)"
        title="售后报告详情（访问已审计）"
        width="720px"
        @close="dashboard.selectedReport.value = null"
      >
        <div v-if="dashboard.selectedReport.value" class="sensitive-detail">
          <p><b>报告编号：</b>{{ dashboard.selectedReport.value.report_number }}</p>
          <pre>{{ dashboard.selectedReport.value.content }}</pre>
        </div>
      </el-dialog>

      <el-alert
        v-if="dashboard.detailError.value"
        :title="dashboard.detailError.value"
        type="error"
        :closable="false"
        show-icon
      />
    </template>

    <div v-else-if="!dashboard.loading.value && !dashboard.loadError.value" class="panel empty">
      暂无可显示的管理员数据。
    </div>

    <el-drawer v-model="conversationDialogVisible" title="对话内容（只读）" size="520px">
      <div v-if="dashboard.selectedConversation.value" class="conversation-detail">
        <p class="muted">
          {{ dashboard.selectedConversation.value.model_code }} ·
          {{ dashboard.selectedConversation.value.user_email_masked }} ·
          查看行为已记入审计
        </p>
        <div
          v-for="message in dashboard.selectedConversation.value.messages"
          :key="message.id"
          class="conversation-message"
          :class="message.role"
        >
          <span class="role-tag">{{ message.role === 'user' ? '用户' : '助手' }}</span>
          <p>{{ message.content }}</p>
          <small v-if="message.refusal_reason" class="muted">拒答原因：{{ message.refusal_reason }}</small>
        </div>
      </div>
    </el-drawer>

    <el-dialog v-model="createModelVisible" title="新增型号" width="440px">
      <el-form label-position="top">
        <el-form-item label="型号编码">
          <el-input v-model="newModel.code" placeholder="如 DEMO-X1；仅字母数字与 - _，创建后不可改" />
        </el-form-item>
        <el-form-item label="型号名称">
          <el-input v-model="newModel.name" placeholder="如 DEMO-X1 扫拖一体机" />
        </el-form-item>
        <el-form-item label="品牌">
          <el-input v-model="newModel.brand" placeholder="如示例品牌" />
        </el-form-item>
      </el-form>
      <p class="dialog-hint">
        编码是知识文档与已发出报告的对外标识，创建后不可修改；要换编码请另建型号。
        建好后在下方「知识库上传」给它上传说明书，才能开始作答。
      </p>
      <template #footer>
        <el-button @click="createModelVisible = false">取消</el-button>
        <el-button
          class="brand-button"
          type="primary"
          :loading="dashboard.creatingModel.value"
          @click="submitCreateModel"
        >创建</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.admin-page{max-width:1600px}.load-alert{margin-bottom:20px}.generation-stats{margin-bottom:18px}.refusal-tags{display:flex;flex-wrap:wrap;gap:8px;align-items:center}.refusal-empty{color:var(--muted);font-size:12px}.upload-box{margin-top:16px;padding-top:14px;border-top:1px solid #edf1ef}.upload-box h3{margin:0;font-size:14px}.upload-box p{margin:6px 0 10px;color:var(--muted);font-size:12px;line-height:1.5}.upload-row{display:flex;align-items:flex-start;gap:10px;margin-bottom:10px}.upload-model{width:140px;flex-shrink:0}.overview-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:14px;margin-bottom:18px}.metric-card{padding:19px 20px;display:flex;align-items:center;gap:14px}.metric-card>.el-icon{box-sizing:content-box;padding:11px;border-radius:11px;background:var(--soft);color:var(--brand);font-size:22px}.metric-card b,.metric-card small{display:block}.metric-card b{font-size:24px;line-height:1}.metric-card small{margin-top:7px;color:var(--muted);font-size:12px}.two-column{display:grid;grid-template-columns:1fr 1fr;gap:18px}.section-panel{padding:22px;overflow:hidden}.table-section{margin-top:18px}.section-head{display:flex;align-items:flex-start;justify-content:space-between;gap:20px;margin-bottom:16px}.section-head h2{margin:0;font-size:17px}.section-head p{margin:6px 0 0;color:var(--muted);font-size:12px;line-height:1.5}.sensitive-detail{line-height:1.7}.sensitive-detail pre{white-space:pre-wrap;word-break:break-word;padding:14px;background:#f7faf8;border-radius:8px;max-height:55vh;overflow:auto}:deep(.el-table){--el-table-border-color:#edf1ef;--el-table-header-bg-color:#f7faf8;font-size:12px}:deep(.el-table th.el-table__cell){color:#52635d;font-weight:700}:deep(.el-alert__content){width:100%}:deep(.el-alert__description){display:flex;justify-content:flex-end}.preview-alert{margin-top:10px}.preview-text{margin:4px 0 2px;font-size:12px;line-height:1.6}.preview-meta{margin:0;font-size:11px;color:var(--muted);font-family:ui-monospace,SFMono-Regular,Menlo,monospace}.section-actions{display:flex;align-items:center;gap:8px;flex-shrink:0}.section-head-actions{display:flex;align-items:center;gap:10px;flex-shrink:0}.cost-panel{margin-bottom:18px}.cost-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:14px;margin-bottom:14px}.cost-grid div{padding:14px 16px;background:#f7faf8;border-radius:10px}.cost-grid b{display:block;font-size:20px}.cost-grid small{display:block;margin-top:5px;color:var(--muted);font-size:12px}.clickable-tag{cursor:pointer}.conversation-detail{padding:0 4px}.conversation-message{margin-bottom:14px;padding:12px;border-radius:10px;background:#f7faf8}.conversation-message.user{background:#eef6f2}.conversation-message p{margin:6px 0 0;font-size:13px;line-height:1.7;white-space:pre-wrap;word-break:break-word}.role-tag{font-size:11px;font-weight:700;color:var(--brand)}.dialog-hint{margin:4px 0 0;font-size:12px;line-height:1.6;color:var(--muted)}.filter-select{width:130px}.archive-hint{margin-bottom:12px}.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:11px}.gap-loop-hint{margin:-6px 0 14px;padding:10px 12px;background:#f7faf8;border-radius:8px;color:var(--muted);font-size:12px;line-height:1.6}.linked-doc{font-size:12px}.gap-doc-select{width:100%}.replay-cell{display:flex;flex-direction:column;gap:3px}.replay-meta{font-size:11px;color:var(--muted)}.replay-excerpt{margin:2px 0 0;font-size:11px;color:#52635d;line-height:1.5;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}.muted{color:var(--muted);font-size:12px}.doc-detail{padding:0 4px}.doc-meta{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px 18px;margin:0 0 18px}.doc-meta div{display:flex;gap:8px;font-size:12px}.doc-meta dt{color:var(--muted);flex-shrink:0}.doc-meta dd{margin:0}.wrap{word-break:break-all}.doc-detail h4{margin:18px 0 10px;font-size:14px}.chunk-head{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap}.chunk-controls{display:flex;align-items:center;gap:8px}.page-filter{width:110px}.chunk-range{font-size:11px;color:var(--muted)}.chunk-list{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:10px}.chunk-list li{padding:12px;background:#f7faf8;border-radius:8px}.chunk-head-row{display:flex;align-items:center;gap:8px;margin-bottom:6px}.chunk-index{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:11px;color:var(--muted)}.chunk-text{margin:0;font-size:12px;line-height:1.7;white-space:pre-wrap;word-break:break-word}@media(max-width:1250px){.overview-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.two-column{grid-template-columns:1fr}.doc-meta{grid-template-columns:1fr}}
.ai-config-panel{margin-bottom:18px}.ai-config-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:0 18px}.ai-config-actions{display:flex;align-items:center;justify-content:space-between;gap:18px;padding-top:12px;border-top:1px solid #edf1ef}.ai-config-actions>span{color:var(--muted);font-size:12px;line-height:1.6}
/* 640px 以下：概览卡与成本卡退成单列（原 cost-grid 三列在任何宽度都没有断点），
   section-head 的标题与按钮改上下排，上传行的固定 140px 型号选择器改整行。 */
@media(max-width:640px){.overview-grid,.cost-grid{grid-template-columns:minmax(0,1fr)}.section-panel{padding:16px 14px}.section-head{flex-direction:column;gap:12px}.section-actions,.section-head-actions{flex-wrap:wrap}.upload-row{flex-direction:column}.upload-model,.filter-select{width:100%}.chunk-head{gap:8px}.chunk-controls{flex-wrap:wrap}}
@media(max-width:640px){.ai-config-grid{grid-template-columns:minmax(0,1fr)}.ai-config-actions{align-items:flex-start;flex-direction:column}}
</style>
