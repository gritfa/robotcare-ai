<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { Search } from '@element-plus/icons-vue'
import { apiError, diagnosticApi } from '../api'
import type { Diagnostic } from '../types'

const rows = ref<Diagnostic[]>([])
const loading = ref(true)
const search = ref('')
const router = useRouter()

const filteredRows = computed(() => rows.value.filter(row => !search.value || row.issue_description.includes(search.value)))
const label = (status: string) => ({ resolved: '已解决', unresolved: '未解决', in_progress: '进行中', report_ready: '报告已生成' }[status] || status)
const hasReport = (diagnostic: Diagnostic) => diagnostic.status === 'report_ready' || Boolean(diagnostic.report_available)

onMounted(async () => {
  try {
    rows.value = await diagnosticApi.list()
  } catch (error) {
    ElMessage.error(apiError(error, '诊断历史加载失败'))
  } finally {
    loading.value = false
  }
})

function open(diagnostic: Diagnostic) {
  router.push(hasReport(diagnostic) ? `/reports/${diagnostic.id}` : `/diagnostics/${diagnostic.id}`)
}
</script>

<template>
  <div class="page">
    <div class="page-head">
      <div><h1>诊断历史</h1><p>查看所有安全排查过程、最终结果和售后报告。</p></div>
      <el-input v-model="search" :prefix-icon="Search" placeholder="搜索问题描述" style="width:280px" clearable />
    </div>
    <section class="panel table-panel">
      <el-table v-loading="loading" :data="filteredRows" @row-click="open">
        <el-table-column label="问题描述" min-width="330"><template #default="{ row }"><div class="issue"><b>{{ row.issue_description }}</b><small v-if="row.error_code">错误码：{{ row.error_code }}</small></div></template></el-table-column>
        <el-table-column prop="device_id" label="设备 ID" width="110" />
        <el-table-column label="已执行步骤" width="120"><template #default="{ row }">{{ row.executions?.length || 0 }} 步</template></el-table-column>
        <el-table-column label="创建时间" width="170"><template #default="{ row }">{{ row.created_at?.replace('T', ' ').slice(0, 16) || '-' }}</template></el-table-column>
        <el-table-column label="状态" width="120"><template #default="{ row }"><el-tag :type="row.status === 'resolved' ? 'success' : hasReport(row as Diagnostic) ? 'primary' : row.status === 'unresolved' ? 'danger' : 'warning'">{{ hasReport(row as Diagnostic) ? '报告已生成' : label(row.status) }}</el-tag></template></el-table-column>
        <el-table-column width="120"><template #default="{ row }"><el-button link type="primary" @click.stop="open(row as Diagnostic)">{{ hasReport(row as Diagnostic) ? '查看报告' : '查看详情' }}</el-button></template></el-table-column>
        <template #empty><div class="empty">暂无诊断记录</div></template>
      </el-table>
    </section>
  </div>
</template>

<style scoped>
.table-panel{padding:8px 20px 20px}.issue b,.issue small{display:block}.issue b{font-size:13px;max-width:430px;white-space:nowrap;text-overflow:ellipsis;overflow:hidden}.issue small{color:var(--muted);font-size:11px;margin-top:5px}:deep(.el-table__row){cursor:pointer}:deep(.el-table__row:hover b){color:var(--brand)}
</style>
