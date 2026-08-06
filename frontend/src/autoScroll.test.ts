import { describe, expect, it } from 'vitest'
import { STICK_TO_BOTTOM_THRESHOLD, isPinnedToBottom } from './autoScroll'

describe('isPinnedToBottom', () => {
  it('正好在底部时贴底', () => {
    expect(isPinnedToBottom({ scrollTop: 800, clientHeight: 400, scrollHeight: 1200 })).toBe(true)
  })

  it('容差内的像素抖动仍算贴底', () => {
    expect(
      isPinnedToBottom({ scrollTop: 800 - STICK_TO_BOTTOM_THRESHOLD, clientHeight: 400, scrollHeight: 1200 }),
    ).toBe(true)
  })

  it('用户明显上滑后不再算贴底——此时抢滚就是把他拽回来', () => {
    expect(isPinnedToBottom({ scrollTop: 200, clientHeight: 400, scrollHeight: 1200 })).toBe(false)
  })

  it('内容还没超过一屏时始终贴底', () => {
    expect(isPinnedToBottom({ scrollTop: 0, clientHeight: 400, scrollHeight: 300 })).toBe(true)
  })

  it('用户上滑后又自己滑回底部，重新算贴底（不需要额外复位状态）', () => {
    const geometry = { scrollTop: 200, clientHeight: 400, scrollHeight: 1200 }
    expect(isPinnedToBottom(geometry)).toBe(false)
    expect(isPinnedToBottom({ ...geometry, scrollTop: 800 })).toBe(true)
  })
})
