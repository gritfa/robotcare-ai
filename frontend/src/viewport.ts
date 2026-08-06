import { getCurrentScope, onScopeDispose, ref, type Ref } from 'vue'

/**
 * 窄屏断点。与 styles.css 里 `@media (max-width: 640px)` 保持一致——
 * CSS 能收拾的布局交给 CSS，只有「该不该渲染这个 DOM」（例如宽表格的次要列）
 * 才需要 JS 层判断，否则列还在、只是被 CSS 藏起来，表格照样按全宽算列。
 */
export const NARROW_QUERY = '(max-width: 640px)'

/**
 * 跟随 media query 变化的响应式布尔值。
 *
 * 没有 window.matchMedia 的环境（vitest 默认 node 环境、SSR）一律返回 false，
 * 即「按宽屏渲染」——宁可多渲染几列也不要在服务端把内容藏掉。
 */
export function useMediaQuery(query: string): Ref<boolean> {
  const matches = ref(false)
  const mql = typeof globalThis.matchMedia === 'function' ? globalThis.matchMedia(query) : null
  if (!mql) return matches

  matches.value = mql.matches
  const onChange = (event: MediaQueryListEvent) => {
    matches.value = event.matches
  }
  // Safari < 14 只有 addListener；两个都试，避免旧 iOS 上监听器静默不生效
  if (typeof mql.addEventListener === 'function') mql.addEventListener('change', onChange)
  else if (typeof mql.addListener === 'function') mql.addListener(onChange)

  if (getCurrentScope()) {
    onScopeDispose(() => {
      if (typeof mql.removeEventListener === 'function') mql.removeEventListener('change', onChange)
      else if (typeof mql.removeListener === 'function') mql.removeListener(onChange)
    })
  }
  return matches
}

/** 当前是否窄屏（手机竖屏）。 */
export function useIsNarrow(): Ref<boolean> {
  return useMediaQuery(NARROW_QUERY)
}
