/**
 * 聊天输入框的回车语义：Enter 发送、Shift+Enter 换行、中文输入法选词期间一律不发送。
 *
 * 单独抽成纯函数是因为这段逻辑的坑全在分支上，而分支很难在组件里测：
 * 中文/日文输入法在候选词浮窗打开时，敲 Enter 是"确认选词"而不是"发送消息"。
 * 若直接绑 @keydown.enter，用户打"扫地机"选完词按回车，选词的那一下就会把
 * 半截内容发出去——这正是把发送键做成 Ctrl+Enter 想绕开、但绕错了方向的问题。
 *
 * 三重判定，任一成立就认定仍在输入法组词中：
 * 1. `event.isComposing`——标准字段，Chrome/Firefox/Edge 在确认选词的那次 keydown 上为 true；
 * 2. `keyCode === 229`——老式但至今可靠的 IME 哨兵值，覆盖不设 isComposing 的浏览器；
 * 3. 组件自己用 compositionstart/compositionend 维护的 composing 状态兜底。
 *
 * 已知边界（不藏）：Safari 某些输入法会把 compositionend 排在 keydown 之前，
 * 且该次 keydown 的 isComposing 为 false、keyCode 非 229，此时无法与"真的想发送"区分。
 * 这种情况下会误发一次。要彻底消除需要在 compositionend 后加时间窗抑制，
 * 那会让"选完词立刻回车发送"变得迟钝——两害相权，这里选择不引入时间窗。
 */

export type ComposerKeyAction = 'send' | 'newline' | 'ignore'

export interface ComposerKeyEvent {
  key: string
  shiftKey: boolean
  /** 浏览器标准字段；测试里可省略 */
  isComposing?: boolean
  keyCode?: number
}

export interface ComposerState {
  /** 组件通过 compositionstart / compositionend 维护 */
  composing: boolean
}

const IME_KEY_CODE = 229

export function isComposingKey(event: ComposerKeyEvent, state: ComposerState): boolean {
  return Boolean(event.isComposing) || event.keyCode === IME_KEY_CODE || state.composing
}

export function resolveComposerKey(
  event: ComposerKeyEvent,
  state: ComposerState = { composing: false },
): ComposerKeyAction {
  if (event.key !== 'Enter') return 'ignore'
  // 输入法组词优先于一切：这一下回车属于输入法，不属于聊天框
  if (isComposingKey(event, state)) return 'ignore'
  return event.shiftKey ? 'newline' : 'send'
}
