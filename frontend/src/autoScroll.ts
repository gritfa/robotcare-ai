/** 「用户上滑看历史时，不要把他拽回底部」。
 *
 * 问题背景：每个 SSE delta 曾无条件调用 scrollTo(scrollHeight)。
 * 回答生成期间用户想往上翻看前一条的引用页码，屏幕会被每几十毫秒一次的
 * 增量硬拽回底部，等于生成期间根本没法读历史。
 *
 * 判定用「距底部的距离」而不是记录一个 userScrolledUp 布尔量：后者要处理
 * 「滑上去又自己滑回来」的复位，容易漏；距离判定天然自洽。
 */

/** 容差：内容行高约 24~28px，留一行多一点，避免像素级抖动误判为「用户上滑了」。 */
export const STICK_TO_BOTTOM_THRESHOLD = 32

export interface ScrollGeometry {
  scrollTop: number
  clientHeight: number
  scrollHeight: number
}

/** 当前是否贴在底部（据此决定新增量要不要自动滚）。 */
export function isPinnedToBottom(
  geometry: ScrollGeometry,
  threshold: number = STICK_TO_BOTTOM_THRESHOLD,
): boolean {
  const distance = geometry.scrollHeight - geometry.scrollTop - geometry.clientHeight
  return distance <= threshold
}
