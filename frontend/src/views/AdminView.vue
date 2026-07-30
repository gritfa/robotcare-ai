<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import type { UploadFile, UploadInstance } from 'element-plus'
import {
  CircleCheckFilled,
  CircleCloseFilled,
  Connection,
  DataAnalysis,
  Document,
  Files,
  QuestionFilled,
  Refresh,
  Upload,
  User,
  Warning,
} from '@element-plus/icons-vue'
import { useAdminDashboard } from '../adminDashboard'
import type { AdminModel } from '../types'

const dashboard = useAdminDashboard()

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

async function submitUpload() {
  if (!canUpload.value || !uploadFile.value) return
  const form = new FormData()
  form.append('model_code', uploadModelCode.value)
  form.append('source_url', uploadSourceUrl.value.trim())
  form.append('file', uploadFile.value)
  const outcome = await dashboard.uploadKnowledge(form)
  if (outcome.ok) {
    ElMessage.success(`知识已入库：${uploadModelCode.value} 新增 ${outcome.result?.chunk_count ?? 0} 个分片`)
    uploadFile.value = null
    uploadSourceUrl.value = ''
    uploadRef.value?.clearFiles()
  } else if (outcome.error) {
    ElMessage.error(outcome.error)
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

async function updateModel(model: AdminModel, value: string | number | boolean) {
  const result = await dashboard.setModelActive(model, Boolean(value))
  if (result.ok) {
    ElMessage.success(`${model.code} 已${model.active ? '启用' : '停用'}`)
  } else if (result.error) {
    ElMessage.error(result.error)
  }
}

onMounted(dashboard.load)
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

      <div class="two-column">
        <section class="panel section-panel">
          <div class="section-head">
            <div>
              <h2>型号启停</h2>
              <p>停用后不再面向新诊断开放；历史数据仍由后端保留。</p>
            </div>
            <el-tag type="info">{{ dashboard.models.value.length }} 个型号</el-tag>
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
                  @change="(value: string | number | boolean) => updateModel(row, value)"
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
                type="primary"
                :icon="Upload"
                :loading="dashboard.uploadingKnowledge.value"
                :disabled="!canUpload"
                @click="submitUpload"
              >
                上传并入库
              </el-button>
            </div>
          </div>
        </section>
      </div>

      <section class="panel section-panel table-section">
        <div class="section-head">
          <div>
            <h2>内容缺口榜（近 30 天）</h2>
            <p>检索无结果与资料缺口拒答的高频查询聚合；只含归一化查询与型号，不含用户信息。</p>
          </div>
          <el-tag type="warning">Top {{ dashboard.contentGaps.value.length }}</el-tag>
        </div>
        <el-table :data="dashboard.contentGaps.value" empty-text="近 30 天没有内容缺口记录">
          <el-table-column prop="query_normalized" label="查询" min-width="260" show-overflow-tooltip />
          <el-table-column prop="count" label="次数" width="80" align="right" />
          <el-table-column label="涉及型号" min-width="150">
            <template #default="{ row }">{{ row.model_codes.join('、') }}</template>
          </el-table-column>
          <el-table-column label="最近发生" width="170">
            <template #default="{ row }">{{ formatTime(row.last_seen_at) }}</template>
          </el-table-column>
        </el-table>
      </section>

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
  </div>
</template>

<style scoped>
.admin-page{max-width:1600px}.load-alert{margin-bottom:20px}.generation-stats{margin-bottom:18px}.refusal-tags{display:flex;flex-wrap:wrap;gap:8px;align-items:center}.refusal-empty{color:var(--muted);font-size:12px}.upload-box{margin-top:16px;padding-top:14px;border-top:1px solid #edf1ef}.upload-box h3{margin:0;font-size:14px}.upload-box p{margin:6px 0 10px;color:var(--muted);font-size:12px;line-height:1.5}.upload-row{display:flex;align-items:flex-start;gap:10px;margin-bottom:10px}.upload-model{width:140px;flex-shrink:0}.overview-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:14px;margin-bottom:18px}.metric-card{padding:19px 20px;display:flex;align-items:center;gap:14px}.metric-card>.el-icon{box-sizing:content-box;padding:11px;border-radius:11px;background:var(--soft);color:var(--brand);font-size:22px}.metric-card b,.metric-card small{display:block}.metric-card b{font-size:24px;line-height:1}.metric-card small{margin-top:7px;color:var(--muted);font-size:12px}.two-column{display:grid;grid-template-columns:1fr 1fr;gap:18px}.section-panel{padding:22px;overflow:hidden}.table-section{margin-top:18px}.section-head{display:flex;align-items:flex-start;justify-content:space-between;gap:20px;margin-bottom:16px}.section-head h2{margin:0;font-size:17px}.section-head p{margin:6px 0 0;color:var(--muted);font-size:12px;line-height:1.5}.sensitive-detail{line-height:1.7}.sensitive-detail pre{white-space:pre-wrap;word-break:break-word;padding:14px;background:#f7faf8;border-radius:8px;max-height:55vh;overflow:auto}:deep(.el-table){--el-table-border-color:#edf1ef;--el-table-header-bg-color:#f7faf8;font-size:12px}:deep(.el-table th.el-table__cell){color:#52635d;font-weight:700}:deep(.el-alert__content){width:100%}:deep(.el-alert__description){display:flex;justify-content:flex-end}@media(max-width:1250px){.overview-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.two-column{grid-template-columns:1fr}}
</style>
