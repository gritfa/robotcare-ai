import { afterEach, describe, expect, it } from 'vitest'
import { effectScope } from 'vue'
import { NARROW_QUERY, useIsNarrow, useMediaQuery } from './viewport'

type Listener = (event: MediaQueryListEvent) => void

function stubMatchMedia(initial: boolean) {
  const listeners = new Set<Listener>()
  const queries: string[] = []
  const mql = {
    get matches() {
      return current
    },
    addEventListener: (_: string, fn: Listener) => listeners.add(fn),
    removeEventListener: (_: string, fn: Listener) => listeners.delete(fn),
  }
  let current = initial
  ;(globalThis as Record<string, unknown>).matchMedia = (query: string) => {
    queries.push(query)
    return mql
  }
  return {
    queries,
    listenerCount: () => listeners.size,
    emit(next: boolean) {
      current = next
      listeners.forEach(fn => fn({ matches: next } as MediaQueryListEvent))
    },
  }
}

afterEach(() => {
  delete (globalThis as Record<string, unknown>).matchMedia
})

describe('useMediaQuery', () => {
  it('没有 matchMedia 的环境按宽屏处理，而不是崩溃或藏掉内容', () => {
    delete (globalThis as Record<string, unknown>).matchMedia
    const scope = effectScope()
    const matches = scope.run(() => useMediaQuery('(max-width: 640px)'))!
    expect(matches.value).toBe(false)
    scope.stop()
  })

  it('初始值取自 matchMedia，并跟随 change 事件更新', () => {
    const stub = stubMatchMedia(true)
    const scope = effectScope()
    const matches = scope.run(() => useMediaQuery('(max-width: 640px)'))!
    expect(matches.value).toBe(true)
    stub.emit(false)
    expect(matches.value).toBe(false)
    scope.stop()
  })

  it('作用域销毁后移除监听器，避免组件卸载后仍被回调', () => {
    const stub = stubMatchMedia(false)
    const scope = effectScope()
    scope.run(() => useMediaQuery('(max-width: 640px)'))
    expect(stub.listenerCount()).toBe(1)
    scope.stop()
    expect(stub.listenerCount()).toBe(0)
  })

  it('useIsNarrow 用的断点与 styles.css 的 640px 一致', () => {
    const stub = stubMatchMedia(false)
    const scope = effectScope()
    scope.run(() => useIsNarrow())
    expect(stub.queries).toEqual([NARROW_QUERY])
    expect(NARROW_QUERY).toContain('640px')
    scope.stop()
  })
})
