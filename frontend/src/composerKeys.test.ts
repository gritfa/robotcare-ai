import { describe, expect, it } from 'vitest'

import { resolveComposerKey } from './composerKeys'

const idle = { composing: false }
const composing = { composing: true }

describe('resolveComposerKey', () => {
  it('Enter 发送，Shift+Enter 换行', () => {
    expect(resolveComposerKey({ key: 'Enter', shiftKey: false }, idle)).toBe('send')
    expect(resolveComposerKey({ key: 'Enter', shiftKey: true }, idle)).toBe('newline')
  })

  it('非 Enter 键一律不拦截', () => {
    expect(resolveComposerKey({ key: 'a', shiftKey: false }, idle)).toBe('ignore')
    expect(resolveComposerKey({ key: 'Escape', shiftKey: true }, idle)).toBe('ignore')
  })

  it('中文输入法选词时的回车属于输入法，绝不发送', () => {
    // 标准字段
    expect(
      resolveComposerKey({ key: 'Enter', shiftKey: false, isComposing: true }, idle),
    ).toBe('ignore')
    // 不设 isComposing 的浏览器只给 keyCode 229
    expect(
      resolveComposerKey({ key: 'Enter', shiftKey: false, keyCode: 229 }, idle),
    ).toBe('ignore')
    // 组件自己维护的 composition 状态兜底
    expect(resolveComposerKey({ key: 'Enter', shiftKey: false }, composing)).toBe('ignore')
  })

  it('选词结束后的回车恢复为发送', () => {
    // compositionend 后 composing 归 false，此时的回车才是用户要发送
    expect(
      resolveComposerKey({ key: 'Enter', shiftKey: false, isComposing: false }, idle),
    ).toBe('send')
  })

  it('输入法状态下的 Shift+Enter 同样不换行不发送', () => {
    // 组词期间整个回车键都归输入法，不该由输入框解释
    expect(resolveComposerKey({ key: 'Enter', shiftKey: true }, composing)).toBe('ignore')
  })
})
