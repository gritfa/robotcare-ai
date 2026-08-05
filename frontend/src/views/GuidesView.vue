<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { Aim, CircleCheck, CircleClose, Document, InfoFilled, Link, Reading, Refresh, Search } from '@element-plus/icons-vue'
import { apiError, deviceApi, knowledgeApi, modelApi, parseApiError, userFacingApiError, type ApiErrorInfo } from '../api'
import { refusalPresentation } from '../answerDisplay'
import SafetyBlockCard from '../components/SafetyBlockCard.vue'
import type { Device, KnowledgeAnswer, KnowledgeHealth, KnowledgeModelStatus, KnowledgeSearchResult, RobotModel } from '../types'

const TOP_K = 3
const devices = ref<Device[]>([])
const models = ref<RobotModel[]>([])
const statuses = ref<KnowledgeModelStatus[]>([])
const selectedDeviceId = ref('')
const selectedModelId = ref('')
const query = ref('')
const results = ref<KnowledgeSearchResult[]>([])
const initialLoading = ref(true)
const searching = ref(false)
const statusLoading = ref(false)
const searchAttempted = ref(false)
const searchError = ref('')
const statusError = ref('')
const safetyError = ref<ApiErrorInfo | null>(null)
const knowledgeHealth = ref<KnowledgeHealth | null>(null)
const probing = ref(false)
const probeError = ref('')
const answering = ref(false)
const answerResult = ref<KnowledgeAnswer | null>(null)
const answerError = ref('')

const selectedModel = computed(() => models.value.find(model => String(model.id) === selectedModelId.value))
const selectedStatus = computed(() => statuses.value.find(status => String(status.robot_model_id) === selectedModelId.value))
const statusByModelId = computed(() => new Map(statuses.value.map(status => [String(status.robot_model_id), status])))
const canSearch = computed(() => Boolean(selectedModelId.value && query.value.trim().length >= 2 && !searching.value))
const healthPresentation = computed(() => {
  if (!knowledgeHealth.value) return null
  if (knowledgeHealth.value.status === 'normal') {
    return { title: '知识检索正常', type: 'success' as const, description: '资料索引与外部向量模型均已就绪。' }
  }
  if (knowledgeHealth.value.status === 'knowledge_degraded') {
    return { title: '知识库降级', type: 'warning' as const, description: '部分型号资料尚未完整就绪，检索结果可能为空或不完整。' }
  }
  return { title: '外部模型不可用', type: 'error' as const, description: '向量模型尚未配置或实际调用失败，系统不会生成替代结果；请稍后重试或使用安全诊断流程。' }
})

// 四项深度探测结果：前三项由真实外部调用证明，最后一项来自服务端索引状态
const probeItems = computed(() => {
  const probe = knowledgeHealth.value?.probe
  if (!probe || !knowledgeHealth.value) return []
  const models = knowledgeHealth.value.models
  return [
    { key: 'embedding', label: '向量模型', ok: probe.embedding_service, note: '实际发起一次 embedding 调用' },
    { key: 'retrieval', label: '检索链路', ok: probe.retrieval_end_to_end, note: '向量库端到端检索一次' },
    { key: 'generation', label: '生成模型', ok: probe.generation_service, note: '实际发起一次生成调用' },
    {
      key: 'index',
      label: '知识库索引',
      ok: models.length > 0 && models.every(item => item.ready),
      note: `${models.filter(item => item.ready).length}/${models.length} 个型号资料就绪`,
    },
  ]
})

onMounted(async () => {
  try {
    const [deviceList, modelList] = await Promise.all([deviceApi.list(), modelApi.list()])
    devices.value = deviceList
    models.value = modelList.filter(model => model.enabled !== false)
    if (devices.value.length) {
      selectedDeviceId.value = String(devices.value[0].id)
      selectedModelId.value = String(devices.value[0].robot_model_id)
    } else if (models.value.length) {
      selectedModelId.value = String(models.value[0].id)
    }
  } catch (error) {
    ElMessage.error(apiError(error, '设备与型号加载失败，请稍后重试'))
  } finally {
    initialLoading.value = false
  }
  await loadStatus()
})

function chooseDevice(value: string) {
  selectedDeviceId.value = value
  const device = devices.value.find(item => String(item.id) === value)
  if (device) selectedModelId.value = String(device.robot_model_id)
  clearSearch()
}

function chooseModel(value: string) {
  selectedModelId.value = value
  const deviceMatchesModel = devices.value.some(device => String(device.id) === selectedDeviceId.value && String(device.robot_model_id) === value)
  if (!deviceMatchesModel) selectedDeviceId.value = ''
  clearSearch()
}

function clearSearch() {
  results.value = []
  searchAttempted.value = false
  searchError.value = ''
  safetyError.value = null
  answerResult.value = null
  answerError.value = ''
}

async function askAnswer() {
  const robotModelId = Number(selectedModelId.value)
  const normalizedQuery = query.value.trim()
  if (!Number.isFinite(robotModelId) || !normalizedQuery) return
  answering.value = true
  answerError.value = ''
  answerResult.value = null
  safetyError.value = null
  try {
    answerResult.value = await knowledgeApi.answer({ robot_model_id: robotModelId, query: normalizedQuery })
  } catch (error) {
    const parsed = parseApiError(error, '智能回答服务暂不可用，请稍后重试')
    if (parsed.code === 'SAFETY_BLOCKED') {
      safetyError.value = parsed
    } else {
      answerError.value = userFacingApiError(error, '智能回答服务暂不可用，请稍后重试')
    }
  } finally {
    answering.value = false
  }
}

async function runProbe() {
  probing.value = true
  probeError.value = ''
  try {
    knowledgeHealth.value = await knowledgeApi.health(true)
    statuses.value = knowledgeHealth.value.models
    statusError.value = ''
  } catch (error) {
    probeError.value = userFacingApiError(error, '深度探测失败，请稍后重试')
  } finally {
    probing.value = false
  }
}

async function loadStatus() {
  statusLoading.value = true
  statusError.value = ''
  probeError.value = ''
  try {
    knowledgeHealth.value = await knowledgeApi.health()
    statuses.value = knowledgeHealth.value.models
  } catch (error) {
    knowledgeHealth.value = null
    try {
      statuses.value = await knowledgeApi.status()
    } catch {
      statuses.value = []
      statusError.value = apiError(error, '暂时无法读取知识库状态')
    }
  } finally {
    statusLoading.value = false
  }
}

async function searchKnowledge() {
  const robotModelId = Number(selectedModelId.value)
  const normalizedQuery = query.value.trim()
  if (!Number.isFinite(robotModelId) || !normalizedQuery) return

  searching.value = true
  searchAttempted.value = true
  searchError.value = ''
  safetyError.value = null
  results.value = []
  try {
    const found = await knowledgeApi.search({ robot_model_id: robotModelId, query: normalizedQuery, top_k: TOP_K })
    results.value = [...found].sort((a, b) => b.score - a.score)
  } catch (error) {
    const parsed = parseApiError(error, '资料检索服务暂不可用，请稍后重试')
    if (parsed.code === 'SAFETY_BLOCKED') {
      safetyError.value = parsed
    } else {
      searchError.value = userFacingApiError(error, '资料检索服务暂不可用，请稍后重试')
    }
  } finally {
    searching.value = false
  }
}

function scoreText(score: number) {
  if (!Number.isFinite(score)) return '未知'
  const normalized = score >= 0 && score <= 1 ? score * 100 : score
  return `${normalized.toFixed(1)}%`
}

function safeSourceUrl(value: string) {
  try {
    const url = new URL(value)
    return ['http:', 'https:'].includes(url.protocol) ? url.href : ''
  } catch {
    return ''
  }
}
</script>

<template>
  <div class="page guide-page" v-loading="initialLoading">
    <div class="page-head">
      <div>
        <p class="eyebrow">MODEL-SPECIFIC KNOWLEDGE</p>
        <h1>型号级使用指导</h1>
        <p>从已收录的说明书与官方资料中查找使用方法和故障线索，所有结果严格匹配所选型号。</p>
      </div>
    </div>

    <el-alert class="disclaimer-alert" type="warning" :closable="false" show-icon>
      <template #title>检索结果不是官方诊断</template>
      本页仅展示第三方系统检索到的资料片段，不能替代海尔官方售后判断。涉及拆机、异味、冒烟、异常高温、电池鼓包或进液时，请立即停止操作并联系官方售后。
    </el-alert>

    <SafetyBlockCard v-if="safetyError" :error="safetyError" />

    <div class="workspace-grid">
      <section class="panel search-panel">
        <div class="section-title">
          <span class="title-icon"><el-icon><Reading /></el-icon></span>
          <div><h2>查找使用与故障资料</h2><p>先确认设备型号，再用一句完整的话描述你的问题。</p></div>
        </div>

        <el-form label-position="top" size="large" @submit.prevent="searchKnowledge">
          <div class="selector-grid">
            <el-form-item label="我的设备（可选）">
              <el-select :model-value="selectedDeviceId" clearable placeholder="选择已绑定设备" style="width:100%" @update:model-value="chooseDevice">
                <el-option v-for="device in devices" :key="device.id" :value="String(device.id)" :label="`${device.nickname} · ${device.robot_model?.code || '未知型号'}`" />
              </el-select>
            </el-form-item>
            <el-form-item label="资料型号" required>
              <el-select :model-value="selectedModelId" placeholder="选择型号" style="width:100%" @update:model-value="chooseModel">
                <el-option v-for="model in models" :key="model.id" :value="String(model.id)" :label="`${model.code} · ${model.name}`">
                  <span>{{ model.code }} · {{ model.name }}</span>
                  <small v-if="statusByModelId.get(String(model.id))" class="option-count">{{ statusByModelId.get(String(model.id))?.chunk_count }} 个片段</small>
                </el-option>
              </el-select>
            </el-form-item>
          </div>

          <el-form-item label="你想了解什么？" required>
            <el-input v-model="query" type="textarea" :rows="4" maxlength="500" show-word-limit placeholder="例如：机器人无法连接 Wi-Fi，路由器已重启，下一步应该检查什么？" @keydown.ctrl.enter="searchKnowledge" />
          </el-form-item>
          <div class="search-actions">
            <span>按 Ctrl + Enter 也可检索；最多返回 {{ TOP_K }} 条相关资料。</span>
            <div class="action-buttons">
              <el-button size="large" :loading="answering" :disabled="!canSearch" @click="askAnswer">智能回答（附引用）</el-button>
              <el-button class="brand-button" type="primary" size="large" :icon="Search" :loading="searching" :disabled="!canSearch" @click="searchKnowledge">检索官方资料</el-button>
            </div>
          </div>
        </el-form>
      </section>

      <aside class="panel status-panel">
        <div class="status-head"><div><h2>知识库状态</h2><p>当前所选型号</p></div><el-button text circle :icon="Refresh" :loading="statusLoading" aria-label="刷新知识库状态" @click="loadStatus" /></div>
        <el-alert
          v-if="healthPresentation"
          class="health-alert"
          :title="healthPresentation.title"
          :description="healthPresentation.description"
          :type="healthPresentation.type"
          :closable="false"
          show-icon
        />
        <div class="probe-block">
          <el-button class="probe-button" size="small" :icon="Aim" :loading="probing" @click="runProbe">深度探测</el-button>
          <p class="probe-hint">对向量模型、检索链路、生成模型各发一次真实请求，确认"配置正常"之外是否真的可用（约需数秒）。</p>
          <ul v-if="probeItems.length" class="probe-list">
            <li v-for="item in probeItems" :key="item.key" :class="item.ok ? 'probe-ok' : 'probe-fail'">
              <el-icon><component :is="item.ok ? CircleCheck : CircleClose" /></el-icon>
              <div><strong>{{ item.label }}</strong><span>{{ item.note }}</span></div>
            </li>
          </ul>
          <el-alert
            v-for="(message, index) in knowledgeHealth?.probe?.errors ?? []"
            :key="index"
            class="probe-error"
            :title="message"
            type="error"
            :closable="false"
          />
          <el-alert v-if="probeError" class="probe-error" :title="probeError" type="error" :closable="false" show-icon />
        </div>
        <template v-if="selectedModel">
          <div class="model-code">{{ selectedModel.code }}</div>
          <strong>{{ selectedModel.name }}</strong>
          <div v-if="selectedStatus" class="status-stats">
            <div><b>{{ selectedStatus.document_count }}</b><span>文档</span></div>
            <div><b>{{ selectedStatus.chunk_count }}</b><span>分片</span></div>
            <div><b>{{ selectedStatus.vector_count }}</b><span>向量</span></div>
          </div>
          <el-alert v-else-if="!statusLoading && !statusError" title="该型号尚未报告知识库状态" type="info" :closable="false" show-icon />
        </template>
        <el-alert v-if="statusError" :title="statusError" type="error" :closable="false" show-icon />
        <p class="status-note"><el-icon><InfoFilled /></el-icon>数量来自服务端实时状态，不代表资料已覆盖所有问题。</p>
      </aside>
    </div>

    <section v-if="answerError || answerResult" class="results-section">
      <el-alert v-if="answerError" :title="answerError" type="error" :closable="false" show-icon />
      <template v-else-if="answerResult">
        <el-alert
          v-if="answerResult.status === 'refused'"
          :title="refusalPresentation(answerResult.refusal_reason).title"
          :description="refusalPresentation(answerResult.refusal_reason).description"
          :type="refusalPresentation(answerResult.refusal_reason).type"
          :closable="false"
          show-icon
        />
        <article v-else class="panel answer-card">
          <div class="answer-head">
            <h2>智能回答</h2>
            <el-tag effect="plain" type="success">每条论断均标注资料来源</el-tag>
          </div>
          <p class="answer-body">{{ answerResult.answer }}</p>
          <div class="answer-citations">
            <span class="citations-title">引用来源：</span>
            <el-tag v-for="citation in answerResult.citations" :key="citation.index" effect="light" class="citation-tag">
              [{{ citation.index }}] 说明书第 {{ citation.page_number }} 页
            </el-tag>
          </div>
          <p class="answer-note">回答只依据已收录资料生成，不代表官方诊断；涉及安全问题请联系官方售后。</p>
        </article>
      </template>
    </section>

    <section class="results-section">
      <div class="results-head">
        <div><h2>资料片段</h2><p v-if="searchAttempted && !searchError">按相关度从高到低排列，共 {{ results.length }} 条</p><p v-else>检索后将在这里展示可追溯来源</p></div>
        <el-tag v-if="selectedModel" effect="plain">{{ selectedModel.code }}</el-tag>
      </div>

      <el-alert v-if="searchError" :title="searchError" description="系统没有生成或补全任何替代结果。请稍后重试，或前往故障排查页面使用已配置的安全流程。" type="error" :closable="false" show-icon />
      <div v-else-if="results.length" class="result-list">
        <article v-for="(result, index) in results" :key="`${result.document_title}-${result.page_number}-${index}`" class="panel result-card">
          <div class="result-rank">{{ index + 1 }}</div>
          <div class="result-content">
            <div class="result-meta">
              <span><el-icon><Document /></el-icon>{{ result.document_title || '未命名资料' }}</span>
              <el-tag type="success" effect="light">相关度 {{ scoreText(result.score) }}</el-tag>
            </div>
            <p>{{ result.content }}</p>
            <div class="source-row">
              <span>页码：{{ result.page_number || '未标注' }}</span>
              <a v-if="safeSourceUrl(result.source_url)" :href="safeSourceUrl(result.source_url)" target="_blank" rel="noopener noreferrer"><el-icon><Link /></el-icon>查看来源</a>
              <span v-else class="missing-source">未提供公开来源链接</span>
            </div>
          </div>
        </article>
      </div>
      <div v-else-if="searchAttempted && !searching" class="panel empty-result">
        <el-icon><Search /></el-icon><h3>没有找到匹配资料</h3><p>请确认型号是否正确，尝试补充错误码、异常现象或具体操作。系统不会在没有资料时编造答案。</p>
      </div>
      <div v-else class="panel result-placeholder">
        <el-icon><Reading /></el-icon><div><h3>等待检索</h3><p>推荐描述“想完成的操作 + 当前现象 + 已尝试步骤”，更容易命中准确片段。</p></div>
      </div>
    </section>
  </div>
</template>

<style scoped>
.guide-page{max-width:1320px}.disclaimer-alert{margin-bottom:20px}.workspace-grid{display:grid;grid-template-columns:minmax(0,1fr) 310px;gap:18px}.search-panel{padding:28px}.section-title{display:flex;gap:14px;align-items:center;margin-bottom:25px}.section-title h2,.status-head h2,.results-head h2{margin:0;font-size:19px}.section-title p,.status-head p,.results-head p{margin:5px 0 0;color:var(--muted);font-size:12px}.title-icon{width:45px;height:45px;border-radius:12px;background:var(--soft);color:var(--brand);display:grid;place-items:center;font-size:22px}.selector-grid{display:grid;grid-template-columns:1fr 1fr;gap:15px}.option-count{float:right;margin-left:24px;color:var(--muted)}.search-actions{display:flex;align-items:center;justify-content:space-between;gap:20px}.action-buttons{display:flex;gap:10px}.answer-card{padding:26px}.answer-head{display:flex;align-items:center;justify-content:space-between;margin-bottom:12px}.answer-head h2{margin:0;font-size:18px}.answer-body{margin:0 0 16px;line-height:1.9;color:#2d3f38;white-space:pre-wrap}.answer-citations{display:flex;flex-wrap:wrap;align-items:center;gap:8px;padding-top:14px;border-top:1px solid var(--line)}.citations-title{font-size:12px;color:var(--muted)}.citation-tag{font-size:12px}.answer-note{margin:12px 0 0;font-size:11px;color:var(--muted)}.search-actions>span{font-size:11px;color:var(--muted)}.status-panel{padding:24px;align-self:start}.status-head{display:flex;justify-content:space-between;align-items:start;padding-bottom:19px;border-bottom:1px solid var(--line)}.health-alert{margin-top:16px}.probe-block{margin-top:14px;padding-top:14px;border-top:1px dashed var(--line)}.probe-button{width:100%}.probe-hint{margin:9px 0 0;font-size:11px;line-height:1.6;color:var(--muted)}.probe-list{list-style:none;margin:12px 0 0;padding:0;display:grid;gap:8px}.probe-list li{display:flex;align-items:flex-start;gap:8px;font-size:12px}.probe-list li .el-icon{margin-top:2px;font-size:15px;flex:none}.probe-list strong{display:block;font-size:12px}.probe-list span{display:block;color:var(--muted);font-size:11px;margin-top:2px}.probe-ok .el-icon{color:var(--brand)}.probe-fail .el-icon{color:#c45656}.probe-fail strong{color:#c45656}.probe-error{margin-top:10px;word-break:break-all}.model-code{display:inline-block;margin:20px 0 8px;padding:4px 9px;border-radius:6px;background:var(--soft);color:var(--brand);font-size:12px;font-weight:800;letter-spacing:.6px}.status-panel>strong{display:block;font-size:14px}.status-stats{display:grid;grid-template-columns:repeat(3,1fr);gap:7px;margin:18px 0}.status-stats div{background:#f6f9f8;border-radius:10px;padding:12px 5px;text-align:center}.status-stats b,.status-stats span{display:block}.status-stats b{font-size:19px;color:var(--brand)}.status-stats span{font-size:10px;color:var(--muted);margin-top:4px}.status-note{display:flex;align-items:flex-start;gap:7px;color:var(--muted);font-size:11px;line-height:1.6;margin:18px 0 0}.status-note .el-icon{margin-top:2px;color:var(--brand);flex:none}.results-section{margin-top:25px}.results-head{display:flex;align-items:center;justify-content:space-between;margin-bottom:13px}.result-list{display:grid;gap:12px}.result-card{display:grid;grid-template-columns:42px 1fr;padding:22px;gap:15px}.result-rank{width:34px;height:34px;border-radius:10px;background:#153e33;color:#fff;display:grid;place-items:center;font-weight:800}.result-meta,.source-row{display:flex;align-items:center;justify-content:space-between;gap:14px}.result-meta>span{display:flex;align-items:center;gap:7px;font-weight:750;font-size:13px}.result-meta .el-icon{color:var(--brand)}.result-content>p{margin:14px 0;color:#40534b;line-height:1.85;white-space:pre-wrap}.source-row{justify-content:flex-start;padding-top:13px;border-top:1px solid var(--line);font-size:11px;color:var(--muted)}.source-row a{display:flex;align-items:center;gap:4px;color:var(--brand);font-weight:700}.missing-source{color:#a26a13}.empty-result,.result-placeholder{min-height:150px;display:flex;align-items:center;justify-content:center;gap:17px;padding:28px;color:var(--muted);text-align:left}.empty-result{flex-direction:column;text-align:center}.empty-result>.el-icon,.result-placeholder>.el-icon{font-size:34px;color:#8eb6a7}.empty-result h3,.result-placeholder h3{margin:0;color:var(--ink);font-size:16px}.empty-result p,.result-placeholder p{margin:6px 0 0;font-size:12px;line-height:1.7;max-width:650px}@media(max-width:1000px){.workspace-grid{grid-template-columns:1fr}.status-panel{width:100%}}@media(max-width:760px){.selector-grid{grid-template-columns:1fr}.search-actions{align-items:flex-start;flex-direction:column}.search-actions .el-button{width:100%}}
</style>
