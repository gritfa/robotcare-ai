import { describe, expect, it } from 'vitest'
import { SKIPPED, createLatestOnly, createSingleFlight } from './requestGuard'

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason: unknown) => void
  const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej })
  return { promise, resolve, reject }
}

describe('createLatestOnly', () => {
  it('慢的旧请求后返回时标为 stale，不许覆盖新结果', async () => {
    const guard = createLatestOnly()
    const slow = deferred<string>()
    const fast = deferred<string>()

    const first = guard.run(() => slow.promise)   // 点了会话 A
    const second = guard.run(() => fast.promise)  // 又点了会话 B

    fast.resolve('B')
    slow.resolve('A')

    expect(await second).toEqual({ stale: false, value: 'B' })
    expect((await first).stale).toBe(true)
  })

  it('单独一次请求不会被误判为 stale', async () => {
    const guard = createLatestOnly()
    expect(await guard.run(async () => 42)).toEqual({ stale: false, value: 42 })
  })

  it('旧请求报错也要标 stale——否则会弹一个属于上一个会话的错误提示', async () => {
    const guard = createLatestOnly()
    const failing = deferred<string>()
    const first = guard.run(() => failing.promise)
    const second = guard.run(async () => 'new')

    failing.reject(new Error('会话 A 加载失败'))
    await second

    const result = await first
    expect(result.stale).toBe(true)
    expect(result.error).toBeInstanceOf(Error)
  })

  it('最新请求自身报错时不标 stale，错误要照常报给用户', async () => {
    const guard = createLatestOnly()
    const result = await guard.run(async () => { throw new Error('boom') })
    expect(result.stale).toBe(false)
    expect((result.error as Error).message).toBe('boom')
  })

  it('invalidate 之后进行中的请求全部作废', async () => {
    const guard = createLatestOnly()
    const pending = deferred<string>()
    const inflight = guard.run(() => pending.promise)
    guard.invalidate()
    pending.resolve('迟到的结果')
    expect((await inflight).stale).toBe(true)
  })
})

describe('createSingleFlight', () => {
  it('进行中时后续调用被挡下，不会重复建会话', async () => {
    const flight = createSingleFlight()
    const pending = deferred<string>()
    let calls = 0
    const task = () => { calls += 1; return pending.promise }

    const first = flight.run(task)
    const second = await flight.run(task)

    expect(second).toBe(SKIPPED)
    expect(calls).toBe(1)
    expect(flight.busy).toBe(true)

    pending.resolve('conversation-1')
    expect(await first).toBe('conversation-1')
    expect(flight.busy).toBe(false)
  })

  it('任务失败后也要释放，不能把入口永久锁死', async () => {
    const flight = createSingleFlight()
    await expect(flight.run(async () => { throw new Error('创建失败') })).rejects.toThrow('创建失败')
    expect(flight.busy).toBe(false)
    expect(await flight.run(async () => 'ok')).toBe('ok')
  })

  it('任务返回 undefined 与「被挡下」可区分', async () => {
    const flight = createSingleFlight()
    expect(await flight.run(async () => undefined)).toBeUndefined()
  })
})
