<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ChatDotRound, FirstAidKit, Plus, Promotion } from '@element-plus/icons-vue'
import { apiError, conversationApi, deviceApi, modelApi, parseApiError, userFacingApiError, type ApiErrorInfo } from '../api'
import { streamChatMessage } from '../chatStream'
import { refusalPresentation } from '../answerDisplay'
import { resolveComposerKey } from '../composerKeys'
import SafetyBlockCard from '../components/SafetyBlockCard.vue'
import type { ChatMessage, Conversation, Device, MessageActionCode, RobotModel } from '../types'

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

const dialogVisible = ref(false)
const dialogSelection = ref('')
const creating = ref(false)

const messageArea = ref<HTMLElement | null>(null)

const activeConversation = computed(() => conversations.value.find(item => item.id === activeId.value) || null)
const canSend = computed(() => Boolean(activeId.value && draft.value.trim() && !streaming.value))
const modelOptions = computed(() => models.value.filter(model => model.enabled !== false))

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
  streaming.value = true
  streamingText.value = ''
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
    })
    messages.value = [...messages.value, assistant]
    refreshConversationSummary(conversationId, content)
    await scrollToBottom()
  } catch (error) {
    // 失败的轮次不落库：移除乐观消息（可能已被服务端 user_message 替换）并还原输入
    messages.value = dropLastUserMessage(
      messages.value.filter(item => item.id !== optimistic.id),
      content,
    )
    draft.value = content
    const parsed = parseApiError(error, '发送失败，请稍后重试')
    if (parsed.code === 'SAFETY_BLOCKED') safetyError.value = parsed
    else ElMessage.error(userFacingApiError(error, '发送失败，请稍后重试'))
  } finally {
    streaming.value = false
    streamingText.value = ''
  }
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
        <div v-if="conversations.length" class="conversation-list">
          <button
            v-for="conversation in conversations"
            :key="conversation.id"
            class="conversation-item"
            :class="{ active: conversation.id === activeId }"
            :disabled="streaming"
            @click="openConversation(conversation.id)"
          >
            <span class="conversation-title">{{ conversation.title || '新会话' }}</span>
            <span class="conversation-model">{{ conversation.robot_model_code }}</span>
          </button>
        </div>
        <p v-else class="conversation-empty">还没有会话，点击「新会话」选择设备型号开始提问。</p>
      </aside>

      <section class="panel chat-panel" v-loading="detailLoading">
        <template v-if="activeId">
          <div class="chat-head">
            <strong>{{ activeConversation?.title || '新会话' }}</strong>
            <div class="chat-head-actions">
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
                    <el-tag v-for="citation in message.citations" :key="citation.index" size="small" effect="light">
                      [{{ citation.index }}] 说明书第 {{ citation.page_number }} 页
                    </el-tag>
                  </div>
                  <div v-if="actionButton(message.action_code)" class="bubble-action">
                    <el-button type="primary" plain size="small" @click="actionButton(message.action_code)!.run()">
                      {{ actionButton(message.action_code)!.label }}
                    </el-button>
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
            <el-button class="brand-button" type="primary" size="large" :icon="Promotion" :loading="streaming" :disabled="!canSend" @click="send">发送</el-button>
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
.bubble-action{margin-top:10px;padding-top:10px;border-top:1px solid #e0ebe5}
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
