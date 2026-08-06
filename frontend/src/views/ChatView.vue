<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ChatDotRound, CopyDocument, Delete, Edit, FirstAidKit, Plus, Promotion, Refresh, Search, VideoPause } from '@element-plus/icons-vue'
import { apiError, conversationApi, deviceApi, knowledgeApi, modelApi, parseApiError, userFacingApiError, type ApiErrorInfo } from '../api'
import { ChatStreamAborted, streamChatMessage } from '../chatStream'
import { SKIPPED, createLatestOnly, createSingleFlight } from '../requestGuard'
import { refusalPresentation } from '../answerDisplay'
import { resolveComposerKey } from '../composerKeys'
import { OFFICIAL_SUPPORT_NAME, OFFICIAL_SUPPORT_URL, supportHandoffHint } from '../supportChannels'
import SafetyBlockCard from '../components/SafetyBlockCard.vue'
import type { AnswerCitation, ChatMessage, Conversation, Device, FeedbackReason, MessageActionCode, QuickAction, RobotModel, SuggestedQuestion } from '../types'

const route = useRoute()
const router = useRouter()

const conversations = ref<Conversation[]>([])
const devices = ref<Device[]>([])
const models = ref<RobotModel[]>([])
const initialLoading = ref(true)
const detailLoading = ref(false)

const activeId = ref<number | null>(null)
const activeModelCode = ref('')
const messages = ref<ChatMessage[]>([])

const draft = ref('')
// 输入法组词状态：组词期间的回车归输入法，不能当成发送（见 composerKeys.ts）
const composing = ref(false)
const streaming = ref(false)
const streamingText = ref('')
// 真实阶段文案：后端现在发 retrieving/retrieved/generating 三个点
const streamStage = ref('')
const STAGE_LABELS: Record<string, string> = {
  retrieving: '正在检索该型号资料…',
  retrieved: '资料已就位，正在组织回答…',
  generating: '正在生成回答…',
}
const stageLabel = computed(() => STAGE_LABELS[streamStage.value] || '正在处理…')
const safetyError = ref<ApiErrorInfo | null>(null)

// 停止生成：中断信号。改真流式后增量是边生成边下发的，落库在全文校验之后，
// 所以中断＝这一轮整体回滚，不能再声称"回答已保存"
const abortController = ref<AbortController | null>(null)
// 发送失败时暂存原文，供"重试"一键重发（输入框内容同时保留）
const failedContent = ref('')
const searchKeyword = ref('')
const evidence = ref<AnswerCitation | null>(null)
const evidenceVisible = ref(false)
// 每条回答的反馈状态：id → helpful，用于按钮高亮，避免重复点击看不出效果
const feedbackGiven = ref<Record<string, boolean>>({})

// 建议问题此前是写死的 5 条，不分型号，且只在"已建会话且为空"时才显示——
// 首次进聊天页只有一句"选择或新建一个会话"，冷启动没有任何抓手。
// 现在按型号从后端取（真实问过且答得上来 > 有诊断流程 > 通用兜底），
// 并且在还没建会话时也显示，点击即自动建会话并发问。
const suggestions = ref<SuggestedQuestion[]>([])
const suggestionsLoading = ref(false)

const dialogVisible = ref(false)
const dialogSelection = ref('')
const creating = ref(false)

const messageArea = ref<HTMLElement | null>(null)
// 后端只回最近一页消息（D5 分页）。截断时要如实告诉用户，
// 否则一条长会话看起来像是「前面的记录被吞了」。
const historyTruncated = ref(false)
const totalMessages = ref(0)

// 连点两个会话时，慢的那次不许覆盖快的那次（详情与建议问题各一条时间线）
const detailRequest = createLatestOnly()
const suggestionRequest = createLatestOnly()
// 冷启动连点建议问题会各建一个空会话——建会话入口同一时刻只放行一次
const conversationCreation = createSingleFlight()
// 组件卸载后不再做任何 UI 反馈：SSE 已中断，这一轮的收尾提示没有观众
let disposed = false

onUnmounted(() => {
  disposed = true
  // 不 abort 的话：用户离开聊天页后 SSE 连接仍开着，后端继续生成、继续计费，
  // 而增量只会被写进一个再也不会渲染的 ref。
  abortController.value?.abort()
  detailRequest.invalidate()
  suggestionRequest.invalidate()
})

const activeConversation = computed(() => conversations.value.find(item => item.id === activeId.value) || null)
const canSend = computed(() => Boolean(activeId.value && draft.value.trim() && !streaming.value))
const modelOptions = computed(() => models.value.filter(model => model.enabled !== false))
// 普通用户记不住型号，优先显示自己起的设备昵称，型号作为副标题
const activeDevice = computed(() => {
  const conversation = activeConversation.value
  if (!conversation) return null
  return devices.value.find(item => Number(item.robot_model_id) === Number(conversation.robot_model_id)) || null
})

onMounted(async () => {
  try {
    const [conversationList, deviceList, modelList] = await Promise.all([
      conversationApi.list(),
      deviceApi.list(),
      modelApi.list(),
    ])
    conversations.value = conversationList
    devices.value = deviceList
    models.value = modelList
    // 首屏就要有抓手：不等用户建会话
    await loadSuggestions(suggestionModelId.value)
  } catch (error) {
    ElMessage.error(apiError(error, '会话列表加载失败，请稍后重试'))
  } finally {
    initialLoading.value = false
  }
  const routeId = Number(route.params.id)
  if (Number.isFinite(routeId) && routeId > 0) await openConversation(routeId, { syncRoute: false })
})

watch(() => route.params.id, async (value) => {
  const routeId = Number(value)
  if (Number.isFinite(routeId) && routeId > 0 && routeId !== activeId.value) {
    await openConversation(routeId, { syncRoute: false })
  }
})

async function openConversation(id: number, { syncRoute = true } = {}) {
  if (streaming.value) return
  detailLoading.value = true
  safetyError.value = null
  const { stale, value: detail, error } = await detailRequest.run(() => conversationApi.get(id))
  // 已被后点的会话超越：既不能渲染这份详情，也不能弹它的错误、不能收 loading
  // （新的那次请求还在飞，loading 归它管）
  if (stale) return
  detailLoading.value = false
  if (error || !detail) {
    ElMessage.error(apiError(error, '会话加载失败，请稍后重试'))
    return
  }
  activeId.value = detail.id
  activeModelCode.value = detail.robot_model_code
  messages.value = detail.messages
  historyTruncated.value = Boolean(detail.truncated)
  totalMessages.value = detail.total_messages ?? detail.messages.length
  if (syncRoute) await router.push({ name: 'chat', params: { id: String(id) } })
  await scrollToBottom()
}

function openCreateDialog() {
  dialogSelection.value = devices.value.length
    ? `device:${devices.value[0].id}`
    : modelOptions.value.length
      ? `model:${modelOptions.value[0].id}`
      : ''
  dialogVisible.value = true
}

function selectionModelId(selection: string): number | null {
  const [kind, rawId] = selection.split(':')
  if (kind === 'device') {
    const device = devices.value.find(item => String(item.id) === rawId)
    return device ? Number(device.robot_model_id) : null
  }
  if (kind === 'model') return Number(rawId)
  return null
}

async function createConversationFor(robotModelId: number): Promise<boolean> {
  // 单飞：连点「新会话」/两条建议问题都只应产生一个会话
  const result = await conversationCreation.run(async () => {
    creating.value = true
    try {
      const conversation = await conversationApi.create(robotModelId)
      conversations.value = [conversation, ...conversations.value]
      dialogVisible.value = false
      activeId.value = conversation.id
      activeModelCode.value = conversation.robot_model_code
      messages.value = []
      historyTruncated.value = false
      totalMessages.value = 0
      safetyError.value = null
      await router.push({ name: 'chat', params: { id: String(conversation.id) } })
      return true
    } catch (error) {
      ElMessage.error(apiError(error, '创建会话失败，请稍后重试'))
      return false
    } finally {
      creating.value = false
    }
  })
  // 被挡下等于「已经有一次创建在进行中」，按未成功处理：调用方不该接着发消息
  return result !== SKIPPED && result
}

async function createConversation() {
  const robotModelId = selectionModelId(dialogSelection.value)
  if (!robotModelId) return
  await createConversationFor(robotModelId)
}

async function send() {
  if (!canSend.value || !activeId.value) return
  const content = draft.value.trim()
  const conversationId = activeId.value
  draft.value = ''
  safetyError.value = null
  failedContent.value = ''
  streaming.value = true
  streamingText.value = ''
  streamStage.value = ''
  const controller = new AbortController()
  abortController.value = controller
  // 乐观展示用户消息；服务端 user_message 事件到达后以真实记录替换
  const optimistic: ChatMessage = {
    id: `local-${conversationId}-${messages.value.length}`,
    role: 'user', content, citations: [], refusal_reason: null, created_at: '',
  }
  messages.value = [...messages.value, optimistic]
  await scrollToBottom()
  try {
    const assistant = await streamChatMessage(conversationId, content, {
      onUserMessage: (message) => {
        messages.value = messages.value.map(item => (item.id === optimistic.id ? message : item))
      },
      onStage: (stage) => {
        // 真实阶段：检索中 → 检索完成 → 生成中。此前后端只有一个阶段点、
        // 前端还没接，用户全程只看到一句不变的"正在检索…"
        streamStage.value = stage
      },
      onDelta: async (text) => {
        streamingText.value += text
        await scrollToBottom()
      },
      onDiscard: () => {
        // 服务端判定已下发内容不能作数（拒答或答案被改写）——立刻清屏，
        // 绝不让一段没通过校验的文本留在用户眼前
        streamingText.value = ''
        streamStage.value = ''
      },
      signal: controller.signal,
    })
    messages.value = [...messages.value, assistant]
    refreshConversationSummary(conversationId, content)
    await scrollToBottom()
  } catch (error) {
    // 组件已卸载（用户离开了聊天页）：连接已在 onUnmounted 里 abort，
    // 这里既不该再拉详情，也不该弹一句没人看得到的提示
    if (disposed) { streaming.value = false; return }
    if (error instanceof ChatStreamAborted) {
      // 改真流式后，回答不再"下发前就已落库"——增量是边生成边发的，
      // 落库发生在全文校验通过之后。中断意味着这一轮整体回滚（含用户消息），
      // 所以按服务端的真实状态重新拉详情，并把提问放回输入框，
      // 不能再宣称"已保存在会话里"（那会让用户以为刷新还能找到）。
      streaming.value = false
      await reloadActiveConversation()
      draft.value = content
      ElMessage.info('已停止生成，这一轮未保存；提问已放回输入框')
      return
    }
    // 失败的轮次不落库：移除乐观消息（可能已被服务端 user_message 替换）并还原输入
    messages.value = dropLastUserMessage(
      messages.value.filter(item => item.id !== optimistic.id),
      content,
    )
    draft.value = content
    const parsed = parseApiError(error, '发送失败，请稍后重试')
    if (parsed.code === 'SAFETY_BLOCKED') {
      safetyError.value = parsed
    } else {
      // 安全拦截是用户自己该改问法，不给重试；其余失败都可一键重发
      failedContent.value = content
      ElMessage.error(userFacingApiError(error, '发送失败，请稍后重试'))
    }
  } finally {
    streaming.value = false
    streamingText.value = ''
    streamStage.value = ''
    abortController.value = null
  }
}

function stopGenerating() {
  abortController.value?.abort()
}

async function retryLastMessage() {
  if (!failedContent.value) return
  draft.value = failedContent.value
  failedContent.value = ''
  await send()
}

async function reloadActiveConversation() {
  if (!activeId.value) return
  // 与 openConversation 共用同一条时间线：中断后刷新的结果不能盖掉
  // 用户此刻已经切过去的另一个会话
  const { stale, value: detail, error } = await detailRequest.run(() => conversationApi.get(activeId.value as number))
  if (stale) return
  if (error || !detail) {
    ElMessage.error(apiError(error, '会话刷新失败，请手动重新打开'))
    return
  }
  messages.value = detail.messages
  historyTruncated.value = Boolean(detail.truncated)
  totalMessages.value = detail.total_messages ?? detail.messages.length
  await scrollToBottom()
}

// 建议问题面向的型号：有会话就跟着会话走，没会话就用用户第一台设备的型号，
// 再没有就用第一个可选型号——冷启动时也要给得出建议。
const suggestionModelId = computed<number | null>(() => {
  const conversation = activeConversation.value
  if (conversation) return Number(conversation.robot_model_id)
  if (devices.value.length) return Number(devices.value[0].robot_model_id)
  if (modelOptions.value.length) return Number(modelOptions.value[0].id)
  return null
})

async function loadSuggestions(modelId: number | null) {
  if (!modelId) { suggestions.value = []; return }
  suggestionsLoading.value = true
  const { stale, value, error } = await suggestionRequest.run(() => modelApi.suggestedQuestions(modelId))
  // 快速切型号时，旧型号的建议问题绝不能盖到新型号上——用户会照着不属于
  // 自己机器的问题去问
  if (stale) return
  suggestionsLoading.value = false
  // 建议问题是锦上添花，取不到就安静降级，不要用报错打断用户
  suggestions.value = error ? [] : (value ?? [])
}

watch(suggestionModelId, (modelId) => { void loadSuggestions(modelId) }, { immediate: false })

async function askSuggested(question: string) {
  // 重入守卫：冷启动连点两条建议问题，两次都看到 activeId 为空，
  // 会各建一个会话（多出来的那个还是空的）
  if (streaming.value || conversationCreation.busy) return
  // 还没有会话时先建一个：用户点建议问题的意图就是"我要问这个"，
  // 不应该再把他弹回"请先新建会话"的对话框
  if (!activeId.value) {
    const modelId = suggestionModelId.value
    if (!modelId) { openCreateDialog(); return }
    const created = await createConversationFor(modelId)
    if (!created) return
  }
  draft.value = question
  await send()
}

function dropLastUserMessage(list: ChatMessage[], content: string) {
  const lastIndex = list.length - 1
  if (lastIndex >= 0 && list[lastIndex].role === 'user' && list[lastIndex].content === content) {
    return list.slice(0, lastIndex)
  }
  return list
}

function refreshConversationSummary(conversationId: number, firstQuestion: string) {
  conversations.value = conversations.value
    .map(item => item.id === conversationId
      ? { ...item, title: item.title || firstQuestion.slice(0, 60) }
      : item)
    .sort((a, b) => (a.id === conversationId ? -1 : b.id === conversationId ? 1 : 0))
}

async function escalateToDiagnostic() {
  const conversation = activeConversation.value
  if (!conversation) return
  const lastUserMessage = [...messages.value].reverse().find(item => item.role === 'user')
  await router.push({
    path: '/diagnostics/new',
    query: {
      conversation: String(conversation.id),
      model: String(conversation.robot_model_id),
      description: (lastUserMessage?.content || conversation.title).slice(0, 500),
    },
  })
}

// ---- 快捷操作：后端按状态给出 code，前端只负责跳到对应入口 ----
async function runQuickAction(action: QuickAction) {
  switch (action.code) {
    case 'start_diagnostic':
    case 'upload_image':
      await escalateToDiagnostic()
      return
    case 'resume_diagnostic':
    case 'view_diagnostic':
      if (action.diagnostic_id) await router.push({ name: 'diagnostic-session', params: { id: String(action.diagnostic_id) } })
      return
    case 'view_report':
    case 'download_report_pdf':
      if (action.diagnostic_id) await router.push({ name: 'report', params: { id: String(action.diagnostic_id) } })
      return
    case 'mark_resolved':
      await markResolved(true)
      return
    case 'contact_support':
      // 指南页里没有任何联系方式，跳过去等于把用户送进死胡同。
      // 直接给官方入口，并把客服一定会问的型号一并摆出来。
      try {
        await ElMessageBox.confirm(
          supportHandoffHint(activeModelCode.value),
          `转接${OFFICIAL_SUPPORT_NAME}`,
          { confirmButtonText: '前往官方入口', cancelButtonText: '留在这里', type: 'info' },
        )
        window.open(OFFICIAL_SUPPORT_URL, '_blank', 'noopener,noreferrer')
      } catch {
        // 用户选择留在当前会话，不做任何事
      }
      return
    default:
      ElMessage.info('该操作暂不可用')
  }
}

// ---- 证据抽屉：引用可点开看原文 ----
function openEvidence(citation: AnswerCitation) {
  evidence.value = citation
  evidenceVisible.value = true
}

/** 外链补上页锚点：多数 PDF 阅读器认 #page=N，能直接落到那一页。 */
function sourceUrlWithPage(url: string, page?: number | null) {
  if (!page || page < 1 || url.includes('#')) return url
  return `${url}#page=${page}`
}

function openSourcePage() {
  const url = evidence.value?.source_url
  if (!url) return
  // synthetic:// 是演示数据的占位来源，不是可打开的网址，别让用户点了没反应
  if (!/^https?:\/\//i.test(url)) {
    ElMessage.info('这份资料是本地演示数据，没有可跳转的在线原页')
    return
  }
  window.open(sourceUrlWithPage(url, evidence.value?.page_number), '_blank', 'noopener')
}

const citationPageLoading = ref(false)

/**
 * 只打开引用命中的那一页。
 * 官方外链常是几十 MB 的整本 PDF（海尔那份 27MB），在手机上等于不可核验。
 * 取不到单页（老文档没有存档原件）时回退到外链，不把用户卡死在这一步。
 */
async function openCitedPage() {
  const citation = evidence.value
  if (!citation || citationPageLoading.value) return
  const page = citation.page_number
  if (!citation.document_sha256 || !page) { openSourcePage(); return }
  citationPageLoading.value = true
  try {
    const blob = await knowledgeApi.citationPage(citation.document_sha256, page)
    const objectUrl = URL.createObjectURL(blob)
    window.open(objectUrl, '_blank', 'noopener')
    // 交给浏览器打开后释放；立即 revoke 会让新标签页拿不到内容
    setTimeout(() => URL.revokeObjectURL(objectUrl), 60_000)
  } catch {
    ElMessage.info('这份资料没有留存原件，已为你打开来源链接')
    openSourcePage()
  } finally {
    citationPageLoading.value = false
  }
}

// ---- 会话管理 ----
async function searchConversations() {
  try {
    conversations.value = await conversationApi.list(searchKeyword.value.trim() || undefined)
  } catch (error) {
    ElMessage.error(apiError(error, '搜索失败，请稍后重试'))
  }
}

async function renameConversation(conversation: Conversation) {
  try {
    const { value } = await ElMessageBox.prompt('修改会话标题', '重命名', {
      inputValue: conversation.title,
      inputValidator: (input: string) => (input.trim() ? true : '标题不能为空'),
    })
    const updated = await conversationApi.update(conversation.id, { title: value.trim() })
    conversations.value = conversations.value.map(item => (item.id === updated.id ? updated : item))
  } catch (error) {
    if (error !== 'cancel' && error !== 'close') ElMessage.error(apiError(error, '重命名失败'))
  }
}

async function removeConversation(conversation: Conversation) {
  try {
    await ElMessageBox.confirm(
      `删除「${conversation.title || '新会话'}」？会话内的问答记录会一并删除，此操作不可撤销。`,
      '删除会话',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' },
    )
    await conversationApi.remove(conversation.id)
    conversations.value = conversations.value.filter(item => item.id !== conversation.id)
    if (activeId.value === conversation.id) {
      activeId.value = null
      messages.value = []
      await router.push({ name: 'chat' })
    }
    ElMessage.success('会话已删除')
  } catch (error) {
    if (error !== 'cancel' && error !== 'close') ElMessage.error(apiError(error, '删除失败'))
  }
}

async function markResolved(resolved: boolean) {
  if (!activeId.value) return
  try {
    const updated = await conversationApi.update(activeId.value, { resolved })
    conversations.value = conversations.value.map(item => (item.id === updated.id ? updated : item))
    ElMessage.success(resolved ? '已标记为「问题已解决」' : '已取消解决标记')
  } catch (error) {
    ElMessage.error(apiError(error, '标记失败'))
  }
}

// ---- 回答反馈 ----
const FEEDBACK_REASONS: { value: FeedbackReason; label: string }[] = [
  { value: 'off_topic', label: '答非所问' },
  { value: 'unclear_steps', label: '操作看不懂' },
  { value: 'wrong_citation', label: '引用不正确' },
  { value: 'wrong_model', label: '型号不匹配' },
  { value: 'still_unresolved', label: '问题仍未解决' },
]

async function sendFeedback(message: ChatMessage, helpful: boolean, reason?: FeedbackReason) {
  if (!activeId.value) return
  try {
    await conversationApi.feedback(activeId.value, message.id, { helpful, reason: reason ?? null })
    feedbackGiven.value = { ...feedbackGiven.value, [String(message.id)]: helpful }
    ElMessage.success(helpful ? '谢谢反馈' : '已记录，我们会据此改进资料')
  } catch (error) {
    ElMessage.error(apiError(error, '反馈提交失败'))
  }
}

async function copyMessage(message: ChatMessage) {
  try {
    await navigator.clipboard.writeText(message.content)
    ElMessage.success('已复制')
  } catch {
    ElMessage.error('复制失败，请手动选中文本')
  }
}

async function regenerate(message: ChatMessage) {
  // 重新回答＝把上一条用户提问再发一次；不改历史，让两次回答都留在会话里可对比
  const index = messages.value.findIndex(item => item.id === message.id)
  const question = [...messages.value.slice(0, index)].reverse().find(item => item.role === 'user')
  if (!question) return
  draft.value = question.content
  await send()
}

function onComposerKeydown(evt: Event | KeyboardEvent) {
  // el-input 的 keydown 声明成宽泛的 Event，这里收窄回键盘事件
  if (!(evt instanceof KeyboardEvent)) return
  const event = evt
  const action = resolveComposerKey(event, { composing: composing.value })
  if (action === 'ignore') return
  // 换行交给 textarea 默认行为；只有发送需要拦掉默认的插入换行
  if (action === 'send') {
    event.preventDefault()
    void send()
  }
}

// 产品操作按钮：后端路由层判成 action 的消息带 action_code，这里映射到真实入口。
// 图片上传落在诊断创建页（附件挂在诊断会话上，聊天消息本身不存附件），
// 报告落在历史页（报告由已完成的诊断生成，不能凭空开一份）。
const ACTION_BUTTONS: Record<MessageActionCode, { label: string; run: () => void | Promise<void> }> = {
  start_diagnostic: { label: '进入分步诊断', run: () => escalateToDiagnostic() },
  generate_report: { label: '查看/生成报告', run: async () => { await router.push({ name: 'history' }) } },
  upload_image: { label: '去上传图片', run: () => escalateToDiagnostic() },
}

function actionButton(code: MessageActionCode | null | undefined) {
  return code ? ACTION_BUTTONS[code] : null
}

async function scrollToBottom() {
  await nextTick()
  messageArea.value?.scrollTo({ top: messageArea.value.scrollHeight })
}
</script>

<template>
  <div class="page chat-page" v-loading="initialLoading">
    <div class="page-head">
      <div>
        <p class="eyebrow">MULTI-TURN SUPPORT</p>
        <h1>智能客服</h1>
        <p>围绕所选型号连续追问，每条回答都标注资料页码；资料不足时如实拒答，不编造内容。</p>
      </div>
    </div>

    <div class="chat-grid">
      <aside class="panel conversation-panel">
        <div class="conversation-head">
          <strong>会话</strong>
          <el-button class="brand-button" type="primary" :icon="Plus" size="small" @click="openCreateDialog">新会话</el-button>
        </div>
        <el-input
          v-model="searchKeyword"
          class="conversation-search"
          size="small"
          clearable
          placeholder="搜索标题或问过的内容"
          :prefix-icon="Search"
          @keydown.enter.prevent="searchConversations"
          @clear="searchConversations"
        />
        <div v-if="conversations.length" class="conversation-list">
          <div
            v-for="conversation in conversations"
            :key="conversation.id"
            class="conversation-row"
            :class="{ active: conversation.id === activeId }"
          >
            <button
              class="conversation-item"
              :disabled="streaming"
              @click="openConversation(conversation.id)"
            >
              <span class="conversation-title">
                {{ conversation.title || '新会话' }}
                <el-tag v-if="conversation.resolved" size="small" type="success" effect="plain">已解决</el-tag>
              </span>
              <span class="conversation-model">{{ conversation.robot_model_code }}</span>
            </button>
            <div class="conversation-ops">
              <el-button text size="small" :icon="Edit" :disabled="streaming" title="重命名" @click.stop="renameConversation(conversation)" />
              <el-button text size="small" :icon="Delete" :disabled="streaming" title="删除" @click.stop="removeConversation(conversation)" />
            </div>
          </div>
        </div>
        <p v-else class="conversation-empty">还没有会话，点击「新会话」选择设备型号开始提问。</p>
      </aside>

      <section class="panel chat-panel" v-loading="detailLoading">
        <template v-if="activeId">
          <div class="chat-head">
            <div class="chat-head-device">
              <strong>{{ activeConversation?.title || '新会话' }}</strong>
              <span class="device-line">
                当前设备：{{ activeDevice?.nickname || '未绑定设备' }}
                <em>型号 {{ activeModelCode }}</em>
              </span>
            </div>
            <div class="chat-head-actions">
              <el-button size="small" :disabled="streaming" @click="openCreateDialog">切换设备</el-button>
              <el-button
                v-if="messages.length"
                size="small"
                :disabled="streaming"
                @click="markResolved(true)"
              >问题已解决</el-button>
              <el-button
                v-if="messages.length"
                size="small"
                :icon="FirstAidKit"
                :disabled="streaming"
                @click="escalateToDiagnostic"
              >转分步诊断</el-button>
              <el-tag effect="plain">{{ activeModelCode }}</el-tag>
            </div>
          </div>

          <div ref="messageArea" class="message-area">
            <p v-if="historyTruncated" class="history-note">
              仅显示最近 {{ messages.length }} 条，此前还有 {{ totalMessages - messages.length }} 条更早的消息未加载。
            </p>
            <div v-if="!messages.length && !streaming" class="chat-placeholder">
              <el-icon><ChatDotRound /></el-icon>
              <p>描述“想完成的操作 + 当前现象 + 已尝试步骤”，回答会附资料页码；后续追问会带上本次会话的背景，无需重复描述。</p>
              <div v-if="suggestions.length" class="suggested">
                <span class="suggested-label">试试这些常见问题（{{ activeModelCode }}）：</span>
                <el-button
                  v-for="question in suggestions"
                  :key="question.text"
                  size="small"
                  round
                  :disabled="streaming || creating"
                  @click="askSuggested(question.text)"
                >{{ question.text }}</el-button>
              </div>
            </div>
            <template v-for="message in messages" :key="message.id">
              <div v-if="message.role === 'user'" class="bubble-row user-row">
                <div class="bubble user-bubble">{{ message.content }}</div>
              </div>
              <div v-else class="bubble-row">
                <!-- 拒答不是终点：后端已按「拒答优先给别的出口」排好 quick_actions，
                     这里必须渲染出来。此前只画一个 alert，用户在最需要人接手的时刻
                     屏幕上一个可点的东西都没有。 -->
                <div v-if="message.refusal_reason" class="refusal-block">
                  <el-alert
                    class="refusal-alert"
                    :title="refusalPresentation(message.refusal_reason).title"
                    :description="message.content"
                    :type="refusalPresentation(message.refusal_reason).type"
                    :closable="false"
                    show-icon
                  />
                  <div v-if="message.quick_actions?.length" class="bubble-action">
                    <el-button
                      v-for="action in message.quick_actions"
                      :key="action.code"
                      type="primary"
                      plain
                      size="small"
                      @click="runQuickAction(action)"
                    >{{ action.label }}</el-button>
                  </div>
                  <div class="bubble-feedback">
                    <el-dropdown trigger="click" @command="(reason: FeedbackReason) => sendFeedback(message, false, reason)">
                      <el-button text size="small" :type="feedbackGiven[String(message.id)] === false ? 'danger' : ''">反馈问题</el-button>
                      <template #dropdown>
                        <el-dropdown-menu>
                          <el-dropdown-item v-for="item in FEEDBACK_REASONS" :key="item.value" :command="item.value">
                            {{ item.label }}
                          </el-dropdown-item>
                        </el-dropdown-menu>
                      </template>
                    </el-dropdown>
                    <!-- 安全拦截不给重试：该改的是问法，重发只会再撞一次同样的拦截 -->
                    <el-button
                      v-if="message.refusal_reason !== 'unsafe_answer'"
                      text size="small" :icon="Refresh" :disabled="streaming"
                      @click="regenerate(message)"
                    >重新回答</el-button>
                    <el-button text size="small" :icon="FirstAidKit" :disabled="streaming" @click="escalateToDiagnostic">转分步诊断</el-button>
                  </div>
                </div>
                <div v-else class="bubble assistant-bubble">
                  <p>{{ message.content }}</p>
                  <div v-if="message.citations.length" class="bubble-citations">
                    <el-tag
                      v-for="citation in message.citations"
                      :key="citation.index"
                      class="citation-tag"
                      size="small"
                      effect="light"
                      @click="openEvidence(citation)"
                    >
                      [{{ citation.index }}] 说明书第 {{ citation.page_number }} 页
                    </el-tag>
                    <span class="citation-hint">点击查看原文</span>
                  </div>
                  <div v-if="message.quick_actions?.length" class="bubble-action">
                    <el-button
                      v-for="action in message.quick_actions"
                      :key="action.code"
                      type="primary"
                      plain
                      size="small"
                      @click="runQuickAction(action)"
                    >{{ action.label }}</el-button>
                  </div>
                  <div v-else-if="actionButton(message.action_code)" class="bubble-action">
                    <el-button type="primary" plain size="small" @click="actionButton(message.action_code)!.run()">
                      {{ actionButton(message.action_code)!.label }}
                    </el-button>
                  </div>
                  <div v-if="message.intent !== 'smalltalk'" class="bubble-feedback">
                    <el-button
                      text size="small"
                      :type="feedbackGiven[String(message.id)] === true ? 'success' : ''"
                      @click="sendFeedback(message, true)"
                    >有帮助</el-button>
                    <el-dropdown trigger="click" @command="(reason: FeedbackReason) => sendFeedback(message, false, reason)">
                      <el-button text size="small" :type="feedbackGiven[String(message.id)] === false ? 'danger' : ''">没帮助</el-button>
                      <template #dropdown>
                        <el-dropdown-menu>
                          <el-dropdown-item v-for="item in FEEDBACK_REASONS" :key="item.value" :command="item.value">
                            {{ item.label }}
                          </el-dropdown-item>
                        </el-dropdown-menu>
                      </template>
                    </el-dropdown>
                    <el-button text size="small" :icon="CopyDocument" @click="copyMessage(message)">复制</el-button>
                    <el-button text size="small" :icon="Refresh" :disabled="streaming" @click="regenerate(message)">重新回答</el-button>
                    <el-button text size="small" :icon="FirstAidKit" :disabled="streaming" @click="escalateToDiagnostic">转分步诊断</el-button>
                  </div>
                </div>
              </div>
            </template>
            <div v-if="streaming" class="bubble-row">
              <div class="bubble assistant-bubble streaming-bubble">
                <p v-if="streamingText">{{ streamingText }}<span class="cursor">▍</span></p>
                <p v-else class="thinking">{{ stageLabel }}</p>
              </div>
            </div>
          </div>

          <SafetyBlockCard v-if="safetyError" :error="safetyError" />

          <div class="composer">
            <el-input
              v-model="draft"
              type="textarea"
              :rows="2"
              maxlength="2000"
              :disabled="streaming"
              placeholder="继续提问，Enter 发送，Shift + Enter 换行"
              @keydown="onComposerKeydown"
              @compositionstart="composing = true"
              @compositionend="composing = false"
            />
            <div class="composer-buttons">
              <el-button
                v-if="streaming"
                size="large"
                :icon="VideoPause"
                @click="stopGenerating"
              >停止生成</el-button>
              <el-button
                v-else
                class="brand-button"
                type="primary"
                size="large"
                :icon="Promotion"
                :disabled="!canSend"
                @click="send"
              >发送</el-button>
              <el-button
                v-if="failedContent && !streaming"
                size="large"
                :icon="Refresh"
                @click="retryLastMessage"
              >重试</el-button>
            </div>
          </div>
          <p class="chat-note">回答只依据已收录资料生成，不代表官方诊断；涉及冒烟、异味、电池鼓包等危险情况请立即联系官方售后。</p>
        </template>

        <!-- 冷启动空态：此前只有一句"选择或新建一个会话"，用户无从下手。
             现在直接把该型号"问了会有结果"的问题摆出来，点一下自动建会话并发问。 -->
        <div v-else class="chat-placeholder standalone">
          <el-icon><ChatDotRound /></el-icon>
          <h3>直接问，或先新建一个会话</h3>
          <p>会话与设备型号绑定，回答严格限定在该型号的已收录资料内。</p>
          <div v-if="suggestions.length" class="suggested">
            <span class="suggested-label">这些问题现在就能答：</span>
            <el-button
              v-for="question in suggestions"
              :key="question.text"
              size="small"
              round
              :loading="creating"
              :disabled="streaming || creating"
              @click="askSuggested(question.text)"
            >{{ question.text }}</el-button>
          </div>
          <el-button class="brand-button" type="primary" :icon="Plus" @click="openCreateDialog">新会话</el-button>
        </div>
      </section>
    </div>

    <el-dialog v-model="dialogVisible" title="新会话" width="420px">
      <p class="dialog-hint">选择设备或型号，回答将严格限定在该型号的资料范围内。</p>
      <el-select v-model="dialogSelection" placeholder="选择设备或型号" style="width:100%" size="large">
        <el-option-group v-if="devices.length" label="我的设备">
          <el-option
            v-for="device in devices"
            :key="`device:${device.id}`"
            :value="`device:${device.id}`"
            :label="`${device.nickname} · ${device.robot_model?.code || '未知型号'}`"
          />
        </el-option-group>
        <el-option-group label="全部型号">
          <el-option
            v-for="model in modelOptions"
            :key="`model:${model.id}`"
            :value="`model:${model.id}`"
            :label="`${model.code} · ${model.name}`"
          />
        </el-option-group>
      </el-select>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button class="brand-button" type="primary" :loading="creating" :disabled="!dialogSelection" @click="createConversation">开始对话</el-button>
      </template>
    </el-dialog>

    <!-- 证据抽屉：引用要能核验，光给页码等于让用户自己去信 -->
    <el-drawer v-model="evidenceVisible" title="引用证据" size="440px">
      <div v-if="evidence" class="evidence">
        <dl>
          <dt>文档</dt>
          <dd>{{ evidence.document_title || '官方说明书' }}</dd>
          <dt>产品型号</dt>
          <dd>{{ activeModelCode }}</dd>
          <dt>页码</dt>
          <dd>第 {{ evidence.page_number }} 页</dd>
          <dt>相关度</dt>
          <dd>{{ evidence.score.toFixed(3) }}</dd>
        </dl>
        <div class="evidence-snippet">
          <strong>检索到的原文</strong>
          <p v-if="evidence.snippet">{{ evidence.snippet }}</p>
          <p v-else class="evidence-missing">
            这条引用产生于原文留痕上线之前，只保留了页码。请点下方链接核对说明书原页。
          </p>
        </div>
        <!-- 优先只打开命中的那一页：外链常是几十 MB 整本 PDF，手机上根本核对不了 -->
        <div class="evidence-actions">
          <el-button
            v-if="evidence.page_number"
            class="brand-button"
            type="primary"
            :loading="citationPageLoading"
            @click="openCitedPage"
          >只看第 {{ evidence.page_number }} 页</el-button>
          <el-button plain :disabled="!evidence.source_url" @click="openSourcePage">
            打开完整说明书
          </el-button>
        </div>
        <p class="evidence-url">{{ evidence.source_url }}</p>
      </div>
    </el-drawer>
  </div>
</template>

<style scoped>
.chat-page{max-width:1320px}
.chat-grid{display:grid;grid-template-columns:280px minmax(0,1fr);gap:18px;align-items:start}
.conversation-panel{padding:18px;display:flex;flex-direction:column;gap:14px;max-height:calc(100vh - 240px)}
.conversation-head{display:flex;align-items:center;justify-content:space-between}
.conversation-list{display:flex;flex-direction:column;gap:6px;overflow-y:auto}
.conversation-item{display:flex;flex-direction:column;gap:4px;text-align:left;border:0;background:none;padding:10px 12px;border-radius:10px;cursor:pointer}
.conversation-item:hover{background:#f2f6f4}
.conversation-item.active{background:var(--soft)}
.conversation-item:disabled{cursor:not-allowed;opacity:.6}
.conversation-title{font-size:13px;font-weight:650;color:var(--ink);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:220px}
.conversation-item.active .conversation-title{color:var(--brand)}
.conversation-model{font-size:11px;color:var(--muted)}
.conversation-empty{margin:0;font-size:12px;color:var(--muted);line-height:1.7}
.chat-panel{padding:22px;display:flex;flex-direction:column;min-height:520px;max-height:calc(100vh - 240px)}
.chat-head{display:flex;align-items:center;justify-content:space-between;padding-bottom:14px;border-bottom:1px solid var(--line)}
.chat-head strong{font-size:15px}
.chat-head-actions{display:flex;align-items:center;gap:10px}
.message-area{flex:1;overflow-y:auto;padding:18px 4px;display:flex;flex-direction:column;gap:14px}
.bubble-row{display:flex}
.user-row{justify-content:flex-end}
.bubble{max-width:78%;border-radius:14px;padding:12px 15px;font-size:13px;line-height:1.8}
.user-bubble{background:var(--brand);color:#fff;white-space:pre-wrap}
.assistant-bubble{background:#f4f8f6;color:#2d3f38}
.assistant-bubble p{margin:0;white-space:pre-wrap}
.bubble-citations{display:flex;flex-wrap:wrap;gap:6px;margin-top:10px;padding-top:10px;border-top:1px solid #e0ebe5}
.bubble-action{margin-top:10px;padding-top:10px;border-top:1px solid #e0ebe5;display:flex;flex-wrap:wrap;gap:8px}
.bubble-feedback{margin-top:6px;display:flex;flex-wrap:wrap;align-items:center;gap:2px}
.citation-tag{cursor:pointer}
.citation-hint{font-size:12px;color:var(--muted,#8a9a92);align-self:center}
.history-note{margin:0 0 10px;padding:8px 12px;background:#f4f8f6;border-radius:8px;color:var(--muted);font-size:12px;line-height:1.6;text-align:center}
.conversation-search{margin-bottom:10px}
.conversation-row{display:flex;align-items:center;gap:4px;border-radius:10px}
.conversation-row.active{background:#eef6f2}
.conversation-row .conversation-item{flex:1}
.conversation-ops{display:flex;flex-direction:column}
.chat-head-device{display:flex;flex-direction:column;gap:2px}
.device-line{font-size:12px;color:var(--muted,#8a9a92)}
.device-line em{font-style:normal;margin-left:8px}
.suggested{margin-top:14px;display:flex;flex-wrap:wrap;gap:8px;justify-content:center}
.suggested-label{width:100%;font-size:13px;color:var(--muted,#8a9a92)}
.composer-buttons{display:flex;flex-direction:column;gap:6px}
.evidence dl{display:grid;grid-template-columns:72px 1fr;row-gap:8px;margin:0 0 16px}
.evidence dt{color:var(--muted,#8a9a92);font-size:13px}
.evidence dd{margin:0;font-size:14px;word-break:break-all}
.evidence-snippet{background:#f6faf8;border:1px solid #e0ebe5;border-radius:10px;padding:12px;margin-bottom:16px}
.evidence-snippet p{margin:8px 0 0;line-height:1.7;white-space:pre-wrap}
.evidence-missing{color:var(--muted,#8a9a92)}
.evidence-actions{display:flex;flex-wrap:wrap;gap:10px;margin-top:4px}.evidence-url{margin-top:10px;font-size:12px;color:var(--muted,#8a9a92);word-break:break-all}
.refusal-alert{max-width:78%}
.streaming-bubble .thinking{color:var(--muted)}
.cursor{animation:blink 1s step-start infinite;color:var(--brand)}
@keyframes blink{50%{opacity:0}}
.composer{display:flex;gap:10px;align-items:flex-end;padding-top:14px;border-top:1px solid var(--line)}
.composer .el-button{height:54px}
.chat-note{margin:10px 0 0;font-size:11px;color:var(--muted)}
.chat-placeholder{display:flex;flex-direction:column;align-items:center;justify-content:center;gap:12px;color:var(--muted);text-align:center;padding:40px 20px;font-size:12px;line-height:1.7}
.chat-placeholder .el-icon{font-size:34px;color:#8eb6a7}
.chat-placeholder.standalone{flex:1}
.chat-placeholder h3{margin:0;color:var(--ink);font-size:16px}
.chat-placeholder p{max-width:420px;margin:0}
.dialog-hint{margin:0 0 14px;font-size:12px;color:var(--muted)}
/* 单列必须写 minmax(0,1fr) 而不是 1fr：1fr 的下限是 auto，grid item 的
   min-content（会话标题 220px + 操作列 + 气泡）会把列撑到 461px，390px 屏
   实测溢出 86px。宽屏那条早就写对了 minmax(0,1fr)，只有这条断点漏了。 */
@media(max-width:1000px){.chat-grid{grid-template-columns:minmax(0,1fr)}.conversation-panel{max-height:260px}}
@media(max-width:640px){.conversation-title{max-width:none}.bubble{max-width:92%}.conversation-panel,.chat-panel{padding:14px}.chat-head{flex-direction:column;align-items:flex-start;gap:10px}.chat-head-actions{flex-wrap:wrap;width:100%}.chat-head-device strong{font-size:14px;word-break:break-word}}
</style>
