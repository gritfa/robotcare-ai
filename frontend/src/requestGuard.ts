/**
 * 异步请求的两类竞态守卫。抽成纯模块是为了能测——逻辑留在 .vue 里
 * 就只能靠人点两下试试看，而竞态恰恰是人手点不出来的那种 bug。
 */

/**
 * 只认最后一次发起的结果。
 *
 * 场景：连点两个会话，先点的 A 请求慢、后点的 B 请求快，B 先渲染完，
 * A 随后返回把消息列表又盖回 A 的内容——用户看到的是 B 的标题配 A 的消息。
 * 用发起序号比对，被后发请求超越的旧结果一律标 stale 丢弃。
 */
export function createLatestOnly() {
  let seq = 0
  return {
    /** 结果里的 stale=true 表示「这次请求已被更新的请求超越」，调用方必须直接 return。 */
    async run<T>(task: () => Promise<T>): Promise<{ stale: boolean; value?: T; error?: unknown }> {
      const mine = ++seq
      try {
        const value = await task()
        return { stale: mine !== seq, value }
      } catch (error) {
        return { stale: mine !== seq, error }
      }
    },
    /** 让所有进行中的请求作废（组件卸载、用户切走）。 */
    invalidate() {
      seq += 1
    },
  }
}

/** 被单飞守卫挡下时的返回值。用 symbol 而不是 undefined，免得和「任务本身返回 undefined」混淆。 */
export const SKIPPED = Symbol('request-skipped')

/**
 * 同一时刻只放行一次。
 *
 * 场景：冷启动没有会话时连点两条建议问题，两次都看到 activeId 为空，
 * 于是各建一个会话——用户莫名多出一个空会话，问题还只发进了其中一个。
 */
export function createSingleFlight() {
  let busy = false
  return {
    get busy() {
      return busy
    },
    async run<T>(task: () => Promise<T>): Promise<T | typeof SKIPPED> {
      if (busy) return SKIPPED
      busy = true
      try {
        return await task()
      } finally {
        busy = false
      }
    },
  }
}
