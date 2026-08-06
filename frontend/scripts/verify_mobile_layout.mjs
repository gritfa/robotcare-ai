#!/usr/bin/env node
/**
 * 移动端布局回归自证：三档视口遍历**全部**路由，断言零横向溢出。
 *
 * 为什么要有这个脚本：上一轮做移动端适配时靠人工走查了 8 个页面，
 * 结果 DevicesView / HistoryView / AdminLayout 三个页面一个 @media 都没有，
 * 全被漏掉了。走查会漏，脚本不会。
 *
 * 打的是**正在运行的前端**（默认生产栈 127.0.0.1:5173），不是 dev server——
 * 本频道踩过的坑：代码改了但镜像没重建时，dev server 一切正常、线上纹丝不动。
 *
 * 用法：
 *   node scripts/verify_mobile_layout.mjs
 *   ROBOTCARE_WEB=http://127.0.0.1:5173 ROBOTCARE_API=http://127.0.0.1:8010 \
 *     RC_USER=... RC_PASS=... RC_ADMIN_USER=... RC_ADMIN_PASS=... node scripts/verify_mobile_layout.mjs
 */
import { chromium } from 'playwright'

const WEB = process.env.ROBOTCARE_WEB || 'http://127.0.0.1:5173'
const API = process.env.ROBOTCARE_API || 'http://127.0.0.1:8010'
const PREFIX = '/api/v1'
const VIEWPORTS = [390, 768, 1440]
const TOLERANCE = 2 // 浏览器亚像素舍入

// 凭据一律从环境变量注入，脚本内不留任何默认值。
// 本仓是公开仓：把可登录账号写成默认值等同于明文公开生产口令。
function requireCred(emailVar, passVar) {
  const email = process.env[emailVar]
  const password = process.env[passVar]
  if (!email || !password) {
    console.error(`缺少环境变量 ${emailVar} / ${passVar}，无法登录。`)
    console.error(`用法: ${emailVar}=... ${passVar}=... node frontend/scripts/verify_mobile_layout.mjs`)
    process.exit(2)
  }
  return { email, password }
}

const USER = requireCred('RC_USER', 'RC_PASS')
const ADMIN = requireCred('RC_ADMIN_USER', 'RC_ADMIN_PASS')

async function login(cred) {
  const res = await fetch(`${API}${PREFIX}/auth/login`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(cred),
  })
  if (!res.ok) throw new Error(`登录失败 ${cred.email}: HTTP ${res.status} ${await res.text()}`)
  return (await res.json()).access_token
}

async function get(token, path) {
  const res = await fetch(`${API}${PREFIX}${path}`, { headers: { Authorization: `Bearer ${token}` } })
  return res.ok ? res.json() : null
}

/** 把带参数的路由解析成可访问的真实 URL；拿不到样本数据的路由跳过并显式记账。 */
async function resolveRoutes(token) {
  const routes = [
    { path: '/login', anonymous: true },
    { path: '/register', anonymous: true },
    { path: '/' },
    { path: '/devices' },
    { path: '/guides' },
    { path: '/chat' },
    { path: '/diagnostics/new' },
    { path: '/history' },
  ]
  const skipped = []

  const diagnostics = (await get(token, '/diagnostics')) || []
  const anyDiagnostic = diagnostics[0]
  if (anyDiagnostic) routes.push({ path: `/diagnostics/${anyDiagnostic.id}` })
  else skipped.push('/diagnostics/:id（账号下无诊断记录）')

  const withReport = diagnostics.find(d => d.status === 'report_ready' || d.report_available)
  if (withReport) routes.push({ path: `/reports/${withReport.id}` })
  else skipped.push('/reports/:id（账号下无已生成报告的诊断）')

  const conversations = (await get(token, '/conversations')) || []
  if (conversations[0]) routes.push({ path: `/chat/${conversations[0].id}` })
  else skipped.push('/chat/:id（账号下无会话）')

  return { routes, skipped }
}

/** 返回横向溢出像素数与最靠右的越界元素（排除自身可横向滚动的容器内部，如 el-table）。 */
const probe = () => {
  const doc = document.documentElement
  const overflow = doc.scrollWidth - doc.clientWidth
  const insideScroller = el => {
    let node = el.parentElement
    while (node) {
      if (node.scrollWidth > node.clientWidth + 2 && getComputedStyle(node).overflowX !== 'visible') return true
      node = node.parentElement
    }
    return false
  }
  const worst = [...document.querySelectorAll('*')]
    .filter(el => el.getBoundingClientRect().right > doc.clientWidth + 2 && !insideScroller(el))
    .map(el => ({
      tag: el.tagName.toLowerCase() + (el.className?.toString?.() ? '.' + el.className.toString().trim().split(/\s+/).slice(0, 2).join('.') : ''),
      right: Math.round(el.getBoundingClientRect().right),
      width: Math.round(el.getBoundingClientRect().width),
    }))
    .sort((a, b) => b.right - a.right)[0] || null
  return { overflow, worst }
}

async function main() {
  const [userToken, adminToken] = await Promise.all([login(USER), login(ADMIN)])
  const { routes, skipped } = await resolveRoutes(userToken)
  routes.push({ path: '/admin', admin: true })

  const browser = await chromium.launch()
  const failures = []
  let checks = 0

  for (const width of VIEWPORTS) {
    const narrow = width < 700
    for (const token of [userToken, adminToken]) {
      const targets = routes.filter(r => (token === adminToken ? r.admin : !r.admin))
      if (!targets.length) continue
      const context = await browser.newContext({ viewport: { width, height: 900 }, isMobile: narrow, hasTouch: narrow })
      const page = await context.newPage()
      await page.goto(`${WEB}/login`, { waitUntil: 'domcontentloaded' })
      await page.evaluate(t => localStorage.setItem('robotcare_access_token', t), token)

      for (const route of targets) {
        // 匿名页要在未登录状态下看，否则守卫会把已登录用户弹回首页
        if (route.anonymous) await page.evaluate(() => localStorage.removeItem('robotcare_access_token'))
        await page.goto(WEB + route.path, { waitUntil: 'networkidle', timeout: 40000 })
        await page.waitForTimeout(1200)
        const landed = new URL(page.url()).pathname
        const result = await page.evaluate(probe)
        checks += 1
        const detail = result.worst ? `  最右越界元素 ${result.worst.tag} right=${result.worst.right} w=${result.worst.width}` : ''
        const redirected = landed !== route.path ? ` (跳转至 ${landed})` : ''
        if (result.overflow > TOLERANCE) {
          failures.push(`${width}px ${route.path}${redirected} 横向溢出 ${result.overflow}px${detail}`)
          console.log(`FAIL ${String(width).padStart(4)}px ${route.path.padEnd(20)} 溢出=${result.overflow}px${detail}`)
        } else {
          console.log(`ok   ${String(width).padStart(4)}px ${route.path.padEnd(20)} 溢出=${result.overflow}px${redirected}`)
        }
        if (route.anonymous) await page.evaluate(t => localStorage.setItem('robotcare_access_token', t), token)
      }
      await context.close()
    }
  }
  await browser.close()

  // 覆盖不全时必须说出来：静默跳过会让报告读起来像「全都测过了」
  if (skipped.length) console.log(`\n未覆盖（缺少样本数据）：\n  - ${skipped.join('\n  - ')}`)
  console.log(`\n检查 ${checks} 项，视口 ${VIEWPORTS.join('/')}px`)
  if (failures.length) {
    console.log(`FAIL：${failures.length} 项横向溢出\n  - ${failures.join('\n  - ')}`)
    process.exit(1)
  }
  console.log('PASS：所有路由在全部视口下零横向溢出')
}

main().catch(error => {
  console.error('脚本自身出错：', error)
  process.exit(2)
})
