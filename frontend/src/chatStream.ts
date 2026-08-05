import { authApi } from './api'
import { getAccessToken } from './authSession'
import type { ChatMessage, EntityId } from './types'

/** 智能客服 SSE 客户端。服务端只在回答通过引用与安全校验后才开始分片下发，
 * 因此 delta 拼接结果必然等于最终 assistant_message 的 content。 */

export interface ChatStreamEvent {
  event: string
  data: unknown
}

export interface ChatStreamHandlers {
  onUserMessage?: (message: ChatMessage) => void
  onStage?: (stage: string) => void
  onDelta?: (text: string) => void
  /** 用户点"停止生成"用的中断信号 */
  signal?: AbortSignal
}

/** 用户主动中断。与网络错误区分开：服务端在开始下发 delta 之前就已经把
 * 通过校验的回答落库了，所以中断只是停止渲染，答案本身并没有丢——
 * 调用方应当重新拉取会话详情拿到那条已持久化的消息，而不是报错重来。 */
export class ChatStreamAborted extends Error {
  constructor() {
    super('用户已停止生成')
    this.name = 'ChatStreamAborted'
  }
}

/** 增量 SSE 解析器：容忍事件被网络分片从任意位置切开。 */
export function createSseParser(onEvent: (event: ChatStreamEvent) => void) {
  let buffer = ''
  return {
    push(chunk: string) {
      buffer += chunk
      let boundary = buffer.indexOf('\n\n')
      while (boundary >= 0) {
        const block = buffer.slice(0, boundary)
        buffer = buffer.slice(boundary + 2)
        const parsed = parseBlock(block)
        if (parsed) onEvent(parsed)
        boundary = buffer.indexOf('\n\n')
      }
    },
  }
}

function parseBlock(block: string): ChatStreamEvent | null {
  let event = 'message'
  const dataLines: string[] = []
  for (const line of block.split('\n')) {
    if (line.startsWith('event:')) event = line.slice('event:'.length).trim()
    else if (line.startsWith('data:')) dataLines.push(line.slice('data:'.length).replace(/^ /, ''))
  }
  if (!dataLines.length) return null
  try {
    return { event, data: JSON.parse(dataLines.join('\n')) }
  } catch {
    return null
  }
}

function requestStream(
  conversationId: EntityId,
  content: string,
  fetchImpl: typeof fetch,
  signal?: AbortSignal,
) {
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    Accept: 'text/event-stream',
  }
  const token = getAccessToken()
  if (token) headers.Authorization = `Bearer ${token}`
  return fetchImpl(`/api/v1/conversations/${conversationId}/messages/stream`, {
    method: 'POST',
    headers,
    body: JSON.stringify({ content }),
    credentials: 'include',
    signal,
  })
}

/** 非 2xx 时抛出 axios 形状的错误，让 parseApiError / userFacingApiError 复用既有解析。 */
async function responseError(response: Response) {
  let data: unknown = null
  try {
    data = await response.json()
  } catch {
    data = null
  }
  return Object.assign(new Error('chat stream request failed'), {
    isAxiosError: true,
    response: { status: response.status, data, headers: response.headers },
  })
}

export async function streamChatMessage(
  conversationId: EntityId,
  content: string,
  handlers: ChatStreamHandlers = {},
  fetchImpl: typeof fetch = (...request) => fetch(...request),
): Promise<ChatMessage> {
  const { signal } = handlers
  let response = await requestStream(conversationId, content, fetchImpl, signal)
  if (response.status === 401) {
    await authApi.refresh()
    response = await requestStream(conversationId, content, fetchImpl, signal)
  }
  if (!response.ok || !response.body) throw await responseError(response)

  let assistantMessage: ChatMessage | null = null
  let streamError: { message?: string } | null = null
  const parser = createSseParser(({ event, data }) => {
    if (event === 'user_message') handlers.onUserMessage?.(data as ChatMessage)
    else if (event === 'stage') handlers.onStage?.((data as { stage: string }).stage)
    else if (event === 'delta') handlers.onDelta?.((data as { text: string }).text)
    else if (event === 'assistant_message') assistantMessage = data as ChatMessage
    else if (event === 'error') streamError = data as { message?: string }
  })
  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  try {
    for (;;) {
      if (signal?.aborted) {
        await reader.cancel().catch(() => undefined)
        throw new ChatStreamAborted()
      }
      const { done, value } = await reader.read()
      if (done) break
      parser.push(decoder.decode(value, { stream: true }))
    }
  } catch (error) {
    // fetch 被 abort 时 read() 自己会抛 AbortError，同样归成"用户停止"
    if (signal?.aborted) throw new ChatStreamAborted()
    throw error
  }
  parser.push(decoder.decode())

  if (streamError) throw new Error((streamError as { message?: string }).message || '生成服务暂不可用，请稍后重试')
  if (!assistantMessage) throw new Error('回答流意外中断，请稍后重试')
  return assistantMessage
}
