<script setup lang="ts">
import { onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import {
  CircleCheckFilled,
  CircleCloseFilled,
  Connection,
  DataAnalysis,
  Document,
  Files,
  Refresh,
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
] as const

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
        </section>
      </div>

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
          <el-table-column prop="reason" label="停止原因" min-width="230" show-overflow-tooltip />
          <el-table-column prop="advice" label="处理建议" min-width="230" show-overflow-tooltip />
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
          <el-table-column prop="diagnostic.issue_description" label="故障描述" min-width="250" show-overflow-tooltip />
          <el-table-column prop="diagnostic.error_code" label="错误码" width="100">
            <template #default="{ row }">{{ row.diagnostic.error_code || '—' }}</template>
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
    </template>

    <div v-else-if="!dashboard.loading.value && !dashboard.loadError.value" class="panel empty">
      暂无可显示的管理员数据。
    </div>
  </div>
</template>

<style scoped>
.admin-page{max-width:1600px}.load-alert{margin-bottom:20px}.overview-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:14px;margin-bottom:18px}.metric-card{padding:19px 20px;display:flex;align-items:center;gap:14px}.metric-card>.el-icon{box-sizing:content-box;padding:11px;border-radius:11px;background:var(--soft);color:var(--brand);font-size:22px}.metric-card b,.metric-card small{display:block}.metric-card b{font-size:24px;line-height:1}.metric-card small{margin-top:7px;color:var(--muted);font-size:12px}.two-column{display:grid;grid-template-columns:1fr 1fr;gap:18px}.section-panel{padding:22px;overflow:hidden}.table-section{margin-top:18px}.section-head{display:flex;align-items:flex-start;justify-content:space-between;gap:20px;margin-bottom:16px}.section-head h2{margin:0;font-size:17px}.section-head p{margin:6px 0 0;color:var(--muted);font-size:12px;line-height:1.5}:deep(.el-table){--el-table-border-color:#edf1ef;--el-table-header-bg-color:#f7faf8;font-size:12px}:deep(.el-table th.el-table__cell){color:#52635d;font-weight:700}:deep(.el-alert__content){width:100%}:deep(.el-alert__description){display:flex;justify-content:flex-end}@media(max-width:1250px){.overview-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.two-column{grid-template-columns:1fr}}
</style>
