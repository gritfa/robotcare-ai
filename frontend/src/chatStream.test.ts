import { describe, expect, it } from 'vitest'
import { parseApiError } from './api'
import { createSseParser, streamChatMessage, type ChatStreamEvent } from './chatStream'
import type { ChatMessage } from './types'

function sse(event: string, data: unknown) {
  return `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`
}

function streamResponse(body: string, chunkSize = body.length) {
  const encoder = new TextEncoder()
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      for (let index = 0; index < body.length; index += chunkSize) {
        controller.enqueue(encoder.encode(body.slice(index, index + chunkSize)))
      }
      controller.close()
    },
  })
  return new Response(stream, { status: 200, headers: { 'Content-Type': 'text/event-stream' } })
}

const USER_MESSAGE: ChatMessage = {
  id: 1, role: 'user', content: '吸力变小了怎么办', citations: [], refusal_reason: null, created_at: '2026-08-04T10:00:00Z',
}
const ASSISTANT_MESSAGE: ChatMessage = {
  id: 2, role: 'assistant', content: '先清空尘盒并清理滤网 [1]。',
  citations: [{ index: 1, source_url: 'synthetic://manual', page_number: 3, score: 0.9, document_sha256: 'abc' }],
  refusal_reason: null, created_at: '2026-08-04T10:00:05Z',
}

describe('createSseParser', () => {
  it('容忍事件在任意字节处被切开、一次推入多个事件', () => {
    const events: ChatStreamEvent[] = []
    const parser = createSseParser((event) => events.push(event))
    const full = sse('stage', { stage: 'generating' }) + sse('delta', { text: '第一段' }) + sse('delta', { text: '换行\n仍在同一事件' })
    for (const char of full) parser.push(char)
    expect(events).toEqual([
      { event: 'stage', data: { stage: 'generating' } },
      { event: 'delta', data: { text: '第一段' } },
      { event: 'delta', data: { text: '换行\n仍在同一事件' } },
    ])
  })

  it('忽略无 data 或 JSON 损坏的块，不影响后续事件', () => {
    const events: ChatStreamEvent[] = []
    const parser = createSseParser((event) => events.push(event))
    parser.push(': keep-alive comment\n\nevent: delta\ndata: {broken\n\n' + sse('done', {}))
    expect(events).toEqual([{ event: 'done', data: {} }])
  })
})

describe('streamChatMessage', () => {
  it('按序派发 user_message/stage/delta 并返回最终已校验消息', async () => {
    const body = sse('user_message', USER_MESSAGE)
      + sse('stage', { stage: 'generating' })
      + sse('delta', { text: '先清空尘盒并' })
      + sse('delta', { text: '清理滤网 [1]。' })
      + sse('assistant_message', ASSISTANT_MESSAGE)
      + sse('done', {})
    const stages: string[] = []
    const deltas: string[] = []
    let userMessage: ChatMessage | null = null
    const result = await streamChatMessage(7, '吸力变小了怎么办', {
      onUserMessage: (message) => { userMessage = message },
      onStage: (stage) => stages.push(stage),
      onDelta: (text) => deltas.push(text),
    }, async () => streamResponse(body, 5))
    expect(userMessage).toEqual(USER_MESSAGE)
    expect(stages).toEqual(['generating'])
    expect(deltas.join('')).toBe(ASSISTANT_MESSAGE.content)
    expect(result).toEqual(ASSISTANT_MESSAGE)
  })

  it('error 事件转成用户可读异常', async () => {
    const body = sse('user_message', USER_MESSAGE)
      + sse('stage', { stage: 'generating' })
      + sse('error', { code: 'GENERATION_UNAVAILABLE', message: '生成服务暂不可用，请稍后重试' })
    await expect(
      streamChatMessage(7, '你好', {}, async () => streamResponse(body)),
    ).rejects.toThrow('生成服务暂不可用')
  })

  it('非 2xx 响应抛出 axios 形状错误，parseApiError 能识别安全拦截', async () => {
    const detail = {
      code: 'SAFETY_BLOCKED', blocked: true, category: 'disassembly',
      risk_level: 'high', reason: '涉及拆机操作', official_service_advice: '请联系官方售后',
    }
    const failing = async () => new Response(JSON.stringify({ detail }), {
      status: 422, headers: { 'Content-Type': 'application/json' },
    })
    const error = await streamChatMessage(7, '教我拆机', {}, failing).catch((raised) => raised)
    const parsed = parseApiError(error)
    expect(parsed.code).toBe('SAFETY_BLOCKED')
    expect(parsed.blocked).toBe(true)
    expect(parsed.officialServiceAdvice).toBe('请联系官方售后')
  })

  it('回答流中断（无 assistant_message 也无 error）时报错', async () => {
    const body = sse('user_message', USER_MESSAGE) + sse('stage', { stage: 'generating' })
    await expect(
      streamChatMessage(7, '你好', {}, async () => streamResponse(body)),
    ).rejects.toThrow('回答流意外中断')
  })
})
