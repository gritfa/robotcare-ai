<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { CircleCheck, Delete, Document, InfoFilled, Picture, Upload, Warning } from '@element-plus/icons-vue'
import { apiError, diagnosticApi, parseApiError, userFacingApiError } from '../api'
import { useDiagnosticStore } from '../stores'
import { uploadAttachmentQueue } from '../attachmentUploadQueue'
import type { Attachment } from '../types'

const store = useDiagnosticStore()
const route = useRoute()
const router = useRouter()
const id = String(route.params.id)
const attachments = ref<Attachment[]>([])
const attachmentsLoading = ref(false)
const attachmentUploading = ref(false)
const attachmentInput = ref<HTMLInputElement>()
const pendingUploads = ref<File[]>([])
const attachmentError = ref('')
const reportError = ref('')
const reportAvailable = ref(false)
const finished = computed(() => ['resolved', 'unresolved', 'report_ready'].includes(store.active?.status || ''))
const displayStatus = computed(() => reportAvailable.value && store.active?.status === 'unresolved' ? 'report_ready' : store.active?.status)
const statusLabels: Record<string, string> = {
  resolved: '已解决',
  unresolved: '未解决',
  report_ready: '报告已生成',
  in_progress: '诊断中',
  collecting: '待开始',
  cancelled: '已取消',
}
const statusLabel = computed(() => statusLabels[displayStatus.value || ''] || displayStatus.value)
const statusType = computed(() => displayStatus.value === 'resolved' ? 'success' : displayStatus.value === 'unresolved' ? 'danger' : displayStatus.value === 'report_ready' ? 'primary' : 'warning')
const pageLoading = computed(() => store.loading || attachmentsLoading.value)

onMounted(async () => {
  try {
    await store.load(id)
    reportAvailable.value = Boolean(store.active?.report_available || store.active?.status === 'report_ready')
    await loadAttachments()
    const retryNotice = sessionStorage.getItem(`robotcare-attachment-retry-${id}`)
    if (retryNotice) {
      attachmentError.value = retryNotice
      sessionStorage.removeItem(`robotcare-attachment-retry-${id}`)
    }
  } catch (error) {
    ElMessage.error(apiError(error, '无法加载诊断记录'))
    router.push('/history')
  }
})

async function loadAttachments() {
  attachmentsLoading.value = true
  try {
    attachments.value = await diagnosticApi.attachments(id)
  } catch (error) {
    attachmentError.value = userFacingApiError(error, '附件列表加载失败，请稍后重试')
  } finally {
    attachmentsLoading.value = false
  }
}

function attachmentName(item: Attachment) {
  return item.original_filename || `附件 ${item.id}`
}

function attachmentSize(item: Attachment) {
  const bytes = item.size_bytes
  if (!bytes) return ''
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`
}

async function removeAttachment(item: Attachment) {
  try {
    await ElMessageBox.confirm(`确认删除附件“${attachmentName(item)}”？`, '删除附件', { type: 'warning' })
    await diagnosticApi.deleteAttachment(id, item.id)
    attachments.value = attachments.value.filter(attachment => String(attachment.id) !== String(item.id))
    ElMessage.success('附件已删除')
  } catch (error) {
    if (error !== 'cancel') ElMessage.error(apiError(error, '附件删除失败'))
  }
}

function openAttachmentPicker() {
  attachmentInput.value?.click()
}

async function selectAttachments(event: Event) {
  const input = event.target as HTMLInputElement
  const available = Math.max(0, 5 - attachments.value.length)
  const inputFiles = Array.from(input.files || [])
  const selected = inputFiles.slice(0, available)
  input.value = ''
  if (inputFiles.length > available) ElMessage.warning(`本次只能再上传 ${available} 张图片`)
  if (!selected.length) {
    attachmentError.value = available ? '请选择 JPG、PNG 或 WebP 图片。' : '每次诊断最多保存 5 张图片。'
    return
  }
  const acceptedTypes = new Set(['image/jpeg', 'image/png', 'image/webp'])
  if (selected.some(file => !acceptedTypes.has(file.type) || file.size > 5 * 1024 * 1024)) {
    attachmentError.value = '仅支持不超过 5 MB 的 JPG、PNG 或 WebP 图片。'
    return
  }
  pendingUploads.value = selected
  await uploadPendingAttachments()
}

async function uploadPendingAttachments() {
  if (!pendingUploads.value.length || attachmentUploading.value) return
  attachmentUploading.value = true
  attachmentError.value = ''
  try {
    const result = await uploadAttachmentQueue(
      pendingUploads.value.slice(0, Math.max(0, 5 - attachments.value.length)),
      file => diagnosticApi.uploadAttachment(id, file),
    )
    attachments.value.push(...result.uploaded)
    pendingUploads.value = result.pending
    if (result.error) {
      const parsed = parseApiError(result.error, '图片上传失败，请稍后重试')
      attachmentError.value = parsed.code === 'RATE_LIMITED'
        ? userFacingApiError(result.error, '图片上传过于频繁')
        : parsed.message
      return
    }
    if (!pendingUploads.value.length) ElMessage.success('图片附件已上传')
  } finally {
    attachmentUploading.value = false
  }
}

async function feedback(resolved: boolean) {
  try {
    if (resolved) {
      await ElMessageBox.confirm('确认当前问题已经解决？提交后本次诊断将结束。', '确认结果', { type: 'success' })
    } else {
      await ElMessageBox.confirm('确认已按说明完成此步骤，但问题仍未解决？', '进入下一步', { type: 'warning' })
    }
    await store.submitFeedback(resolved)
    ElMessage.success(resolved ? '很高兴问题已经解决' : '已记录，正在进入下一步')
  } catch (error) {
    if (error !== 'cancel') ElMessage.error(apiError(error, '反馈提交失败'))
  }
}

async function report() {
  reportError.value = ''
  try {
    if (!reportAvailable.value) {
      await diagnosticApi.createReport(id)
      reportAvailable.value = true
    }
    router.push(`/reports/${id}`)
  } catch (error) {
    reportError.value = userFacingApiError(error, '报告生成失败，请稍后重试')
  }
}
</script>

<template>
  <div class="page session-page" v-loading="pageLoading">
    <template v-if="store.active">
      <div class="page-head">
        <div>
          <p class="eyebrow">DIAGNOSTIC #{{ store.active.id }}</p>
          <h1>{{ finished ? '诊断已结束' : '正在安全排查' }}</h1>
          <p>{{ store.active.issue_description }}<span v-if="store.active.error_code"> · 错误码 {{ store.active.error_code }}</span></p>
        </div>
        <el-tag :type="statusType" size="large">{{ statusLabel }}</el-tag>
      </div>

      <section class="panel attachments" v-loading="attachmentsLoading || attachmentUploading">
        <div class="attachments-head">
          <div><h3>故障图片附件</h3><p>这些图片仅作为故障证据保存，可随时删除。</p></div>
          <div class="attachment-actions">
            <el-tag type="info">{{ attachments.length }}/5 张</el-tag>
            <el-button v-if="pendingUploads.length" type="primary" plain :icon="Upload" :loading="attachmentUploading" @click="uploadPendingAttachments">重试上传（{{ pendingUploads.length }}）</el-button>
            <el-button v-else-if="attachments.length < 5" type="primary" plain :icon="Upload" @click="openAttachmentPicker">添加照片</el-button>
            <input ref="attachmentInput" class="hidden-input" type="file" multiple accept="image/jpeg,image/png,image/webp,.jpg,.jpeg,.png,.webp" @change="selectAttachments" />
          </div>
        </div>
        <el-alert v-if="attachmentError" :title="attachmentError" type="error" :closable="false" show-icon />
        <div class="attachment-grid">
          <div v-for="item in attachments" :key="item.id" class="attachment-item">
            <div class="attachment-fallback"><el-icon><Picture /></el-icon></div>
            <div class="attachment-meta">
              <b :title="attachmentName(item)">{{ attachmentName(item) }}</b>
              <small>{{ attachmentSize(item) || item.content_type || '图片附件' }}</small>
            </div>
            <el-button class="attachment-delete" link type="danger" :icon="Delete" aria-label="删除附件" @click="removeAttachment(item)" />
          </div>
        </div>
      </section>

      <div v-if="!finished && store.currentStep" class="step-layout">
        <section class="panel step-card">
          <div class="progress-row"><span>当前步骤</span><b>{{ store.currentStep.position }}</b></div>
          <el-progress :percentage="Math.min(store.currentStep.position * 20, 90)" :show-text="false" color="#087f5b" />
          <div class="step-body">
            <span class="step-number">{{ store.currentStep.position }}</span>
            <p class="eyebrow">ONE STEP AT A TIME</p>
            <h2>{{ store.currentStep.title }}</h2>
            <div class="instruction">{{ store.currentStep.instruction }}</div>
            <div class="source"><el-icon><Document /></el-icon><span><b>资料依据</b>{{ store.currentStep.source_label }}</span></div>
          </div>
          <div class="feedback">
            <p>完成这个步骤后，问题是否已经解决？</p>
            <div>
              <el-button size="large" @click="feedback(false)">仍未解决，继续下一步</el-button>
              <el-button class="brand-button" type="primary" size="large" :icon="CircleCheck" @click="feedback(true)">问题已解决</el-button>
            </div>
          </div>
        </section>
        <aside>
          <div class="panel guard"><el-icon><InfoFilled /></el-icon><h3>操作原则</h3><p>只执行当前显示的步骤。操作完成前不要连续提交反馈，以确保诊断记录准确。</p></div>
          <div class="warning"><el-icon><Warning /></el-icon><p>如果操作中出现冒烟、焦味、漏液或异常高温，请立即断电并停止排查。</p></div>
        </aside>
      </div>

      <section v-else-if="finished" class="panel result">
        <div :class="['result-icon', store.active.status === 'resolved' ? 'success' : 'failed']"><el-icon><component :is="store.active.status === 'resolved' ? CircleCheck : Warning" /></el-icon></div>
        <h2>{{ store.active.status === 'resolved' ? '问题已解决' : reportAvailable ? '售后诊断报告已生成' : '安全排查步骤已全部完成' }}</h2>
        <p v-if="store.active.status === 'resolved'">本次结果已保存到诊断历史。若问题再次出现，可以重新发起一次诊断。</p>
        <p v-else-if="reportAvailable">问题仍未解决，诊断过程已整理为售后报告。你可以随时重新进入报告页并下载正式 PDF。</p>
        <p v-else>问题仍未解决，建议停止自行处理。现在可以生成一份诊断报告，交给海尔官方售后人员参考。</p>
        <div class="result-actions">
          <el-button @click="router.push('/history')">返回诊断历史</el-button>
          <el-button v-if="store.active.status !== 'resolved'" class="brand-button" type="primary" :icon="Document" @click="report">{{ reportAvailable ? '进入售后诊断报告' : '生成售后诊断报告' }}</el-button>
        </div>
        <el-alert v-if="reportError" :title="reportError" type="error" :closable="false" show-icon />
        <div v-if="store.active.executions?.length" class="timeline">
          <h3>已执行步骤</h3>
          <div v-for="execution in store.active.executions" :key="String(execution.id)"><span></span><p><b>{{ execution.step.title }}</b><small>{{ execution.outcome === 'resolved' ? '已解决' : '完成后未解决' }}</small></p></div>
        </div>
      </section>
      <section v-else class="panel result">
        <div class="result-icon failed"><el-icon><Warning /></el-icon></div>
        <h2>暂时无法恢复当前步骤</h2>
        <p>诊断记录已加载，但没有可执行步骤。请刷新页面；若仍然出现此提示，请重新发起诊断。</p>
        <div class="result-actions"><el-button @click="router.push('/history')">返回诊断历史</el-button></div>
      </section>
    </template>
  </div>
</template>

<style scoped>
.attachment-actions{display:flex;align-items:center;gap:10px}.hidden-input{display:none}.attachments .el-alert{margin-bottom:12px}.result .el-alert{margin-top:18px;text-align:left}
.session-page{max-width:1220px}.attachments{padding:20px 22px;margin-bottom:20px}.attachments-head{display:flex;align-items:center;justify-content:space-between;margin-bottom:14px}.attachments-head h3{margin:0 0 4px;font-size:15px}.attachments-head p{margin:0;color:var(--muted);font-size:11px}.attachment-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px}.attachment-item{display:grid;grid-template-columns:58px minmax(0,1fr) 28px;gap:10px;align-items:center;border:1px solid var(--line);border-radius:10px;padding:8px;background:#fbfcfc}.attachment-item>a,.attachment-fallback{width:58px;height:58px;border-radius:7px;overflow:hidden;background:#edf3f0;display:grid;place-items:center;color:var(--brand)}.attachment-item img{width:100%;height:100%;object-fit:cover;display:block}.attachment-fallback .el-icon{font-size:24px}.attachment-meta{min-width:0}.attachment-meta b,.attachment-meta small{display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.attachment-meta b{font-size:12px}.attachment-meta small{font-size:10px;color:var(--muted);margin-top:5px}.attachment-delete{justify-self:end}.step-layout{display:grid;grid-template-columns:1fr 280px;gap:20px}.step-card{overflow:hidden}.progress-row{display:flex;justify-content:space-between;padding:18px 28px 10px;color:var(--muted);font-size:12px}.progress-row b{color:var(--brand)}.step-card :deep(.el-progress-bar__outer){border-radius:0;height:3px!important}.step-body{padding:45px 58px;position:relative}.step-number{position:absolute;right:50px;top:35px;font-size:72px;font-weight:850;color:#edf3f0}.step-body h2{font-size:27px;margin:13px 0 22px;position:relative}.instruction{background:#f4f8f6;border-left:4px solid var(--brand);padding:22px 24px;font-size:16px;line-height:1.9;white-space:pre-wrap}.source{display:flex;gap:10px;align-items:center;margin-top:22px;color:var(--muted);font-size:12px}.source b{display:block;color:var(--ink);margin-bottom:3px}.feedback{background:#fbfcfc;border-top:1px solid var(--line);padding:22px 28px;display:flex;align-items:center;justify-content:space-between}.feedback p{font-size:13px;color:var(--muted)}.guard{padding:22px}.guard>.el-icon{font-size:23px;color:var(--brand)}.guard h3{margin:13px 0 8px}.guard p,.warning p{font-size:12px;line-height:1.7;color:var(--muted)}.warning{display:flex;gap:10px;margin-top:14px;padding:16px;border-radius:11px;background:#fff4e6;color:#d57600}.warning p{color:#97662c;margin:0}.result{text-align:center;padding:55px;max-width:760px;margin:20px auto}.result-icon{width:74px;height:74px;border-radius:50%;display:grid;place-items:center;margin:auto;font-size:36px}.result-icon.success{background:#e8f8f1;color:#087f5b}.result-icon.failed{background:#fff4e6;color:#e67700}.result h2{font-size:25px}.result>p{color:var(--muted);line-height:1.8;max-width:570px;margin:0 auto 25px}.timeline{text-align:left;margin:38px auto 0;max-width:550px;border-top:1px solid var(--line);padding-top:22px}.timeline>div{display:flex;gap:12px;margin:15px 0}.timeline>div>span{width:9px;height:9px;background:var(--brand);border-radius:50%;margin-top:5px}.timeline p,.timeline small{margin:0;display:block}.timeline small{color:var(--muted);margin-top:4px}@media(max-width:900px){.attachment-grid{grid-template-columns:repeat(2,minmax(0,1fr))}}@media(max-width:650px){.attachment-grid{grid-template-columns:1fr}.step-body{padding:34px 24px}.feedback{align-items:flex-start;flex-direction:column;gap:14px}}
</style>
