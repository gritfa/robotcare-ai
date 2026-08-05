<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ChatDotRound, CopyDocument, Delete, Edit, FirstAidKit, Plus, Promotion, Refresh, Search, VideoPause } from '@element-plus/icons-vue'
import { apiError, conversationApi, deviceApi, modelApi, parseApiError, userFacingApiError, type ApiErrorInfo } from '../api'
import { ChatStreamAborted, streamChatMessage } from '../chatStream'
import { refusalPresentation } from '../answerDisplay'
import { resolveComposerKey } from '../composerKeys'
import SafetyBlockCard from '../components/SafetyBlockCard.vue'
import type { AnswerCitation, ChatMessage, Conversation, Device, FeedbackReason, MessageActionCode, QuickAction, RobotModel } from '../types'

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
const safetyError = ref<ApiErrorInfo | null>(null)

// 停止生成：中断信号；服务端在下发 delta 前已把校验通过的回答落库，
// 所以"停止"只停渲染，之后重新拉详情就能拿到那条已持久化的消息
const abortController = ref<AbortController | null>(null)
// 发送失败时暂存原文，供"重试"一键重发（输入框内容同时保留）
const failedContent = ref('')
const searchKeyword = ref('')
const evidence = ref<AnswerCitation | null>(null)
const evidenceVisible = ref(false)
// 每条回答的反馈状态：id → helpful，用于按钮高亮，避免重复点击看不出效果
const feedbackGiven = ref<Record<string, boolean>>({})

const SUGGESTED_QUESTIONS = [
  '无法启动怎么办？',
  '为什么回不了充电座？',
  '如何重新配网？',
  '主刷卡住怎么清理？',
  '如何生成售后报告？',
]

const dialogVisible = ref(false)
const dialogSelection = ref('')
const creating = ref(false)

const messageArea = ref<HTMLElement | null>(null)

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
  try {
    const detail = await conversationApi.get(id)
    activeId.value = detail.id
    activeModelCode.value = detail.robot_model_code
    messages.value = detail.messages
    if (syncRoute) await router.push({ name: 'chat', params: { id: String(id) } })
    await scrollToBottom()
  } catch (error) {
    ElMessage.error(apiError(error, '会话加载失败，请稍后重试'))
  } finally {
    detailLoading.value = false
  }
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

async function createConversation() {
  const robotModelId = selectionModelId(dialogSelection.value)
  if (!robotModelId) return
  creating.value = true
  try {
    const conversation = await conversationApi.create(robotModelId)
    conversations.value = [conversation, ...conversations.value]
    dialogVisible.value = false
    activeId.value = conversation.id
    activeModelCode.value = conversation.robot_model_code
    messages.value = []
    safetyError.value = null
    await router.push({ name: 'chat', params: { id: String(conversation.id) } })
  } catch (error) {
    ElMessage.error(apiError(error, '创建会话失败，请稍后重试'))
  } finally {
    creating.value = false
  }
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
      onDelta: async (text) => {
        streamingText.value += text
        await scrollToBottom()
      },
      signal: controller.signal,
    })
    messages.value = [...messages.value, assistant]
    refreshConversationSummary(conversationId, content)
    await scrollToBottom()
  } catch (error) {
    if (error instanceof ChatStreamAborted) {
      // 中断只停渲染：回答早在下发前就通过校验并落库了，重新拉详情把它取回来，
      // 假装这轮没发生反而会让用户以为内容丢了
      streaming.value = false
      await reloadActiveConversation()
      ElMessage.info('已停止生成，本次回答已保存在会话里')
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
  try {
    const detail = await conversationApi.get(activeId.value)
    messages.value = detail.messages
    await scrollToBottom()
  } catch (error) {
    ElMessage.error(apiError(error, '会话刷新失败，请手动重新打开'))
  }
}

function askSuggested(question: string) {
  draft.value = question
  void send()
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
      // 官方售后入口在指南页，未来接入工单系统时只需换这一处
      await router.push({ name: 'guides' })
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

function openSourcePage() {
  const url = evidence.value?.source_url
  if (!url) return
  // synthetic:// 是演示数据的占位来源，不是可打开的网址，别让用户点了没反应
  if (!/^https?:\/\//i.test(url)) {
    ElMessage.info('这份资料是本地演示数据，没有可跳转的在线原页')
    return
  }
  window.open(url, '_blank', 'noopener')
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
            <div v-if="!messages.length && !streaming" class="chat-placeholder">
              <el-icon><ChatDotRound /></el-icon>
              <p>描述“想完成的操作 + 当前现象 + 已尝试步骤”，回答会附资料页码；追问时无需重复背景。</p>
              <div class="suggested">
                <span class="suggested-label">试试这些常见问题：</span>
                <el-button
                  v-for="question in SUGGESTED_QUESTIONS"
                  :key="question"
                  size="small"
                  round
                  @click="askSuggested(question)"
                >{{ question }}</el-button>
              </div>
            </div>
            <template v-for="message in messages" :key="message.id">
              <div v-if="message.role === 'user'" class="bubble-row user-row">
                <div class="bubble user-bubble">{{ message.content }}</div>
              </div>
              <div v-else class="bubble-row">
                <el-alert
                  v-if="message.refusal_reason"
                  class="refusal-alert"
                  :title="refusalPresentation(message.refusal_reason).title"
                  :description="message.content"
                  :type="refusalPresentation(message.refusal_reason).type"
                  :closable="false"
                  show-icon
                />
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
                <p v-else class="thinking">正在检索资料并生成回答…</p>
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

        <div v-else class="chat-placeholder standalone">
          <el-icon><ChatDotRound /></el-icon>
          <h3>选择或新建一个会话</h3>
          <p>会话与设备型号绑定，回答严格限定在该型号的已收录资料内。</p>
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
        <el-button type="primary" plain :disabled="!evidence.source_url" @click="openSourcePage">
          打开说明书原页
        </el-button>
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
.evidence-url{margin-top:10px;font-size:12px;color:var(--muted,#8a9a92);word-break:break-all}
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
@media(max-width:1000px){.chat-grid{grid-template-columns:1fr}.conversation-panel{max-height:260px}}
</style>
