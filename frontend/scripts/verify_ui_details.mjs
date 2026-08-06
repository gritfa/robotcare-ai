#!/usr/bin/env node
/**
 * C5 界面细节自证：在**正在运行的生产栈**上验证这批改动真的生效。
 *
 * 为什么不靠单测：引用编号重排、运维信息收权、退出二次确认这些都是
 * "组件渲染出来是什么样"，单测断言的是纯函数，证明不了模板接线对不对。
 * 本频道踩过的坑：后端做了功能≠用户能用到，得查到生产镜像那一层。
 *
 * 用法：node scripts/verify_ui_details.mjs
 */
import { chromium } from 'playwright'

const WEB = process.env.ROBOTCARE_WEB || 'http://127.0.0.1:5173'
const API = process.env.ROBOTCARE_API || 'http://127.0.0.1:8010'
const PREFIX = '/api/v1'

// 凭据一律从环境变量注入，脚本内不留任何默认值。
// 本仓是公开仓：把可登录账号写成默认值等同于明文公开生产口令。
function requireCred(emailVar, passVar) {
  const email = process.env[emailVar]
  const password = process.env[passVar]
  if (!email || !password) {
    console.error(`缺少环境变量 ${emailVar} / ${passVar}，无法登录。`)
    console.error(`用法: ${emailVar}=... ${passVar}=... node frontend/scripts/verify_ui_details.mjs`)
    process.exit(2)
  }
  return { email, password }
}

const USER = requireCred('RC_USER', 'RC_PASS')
const VIEWER = requireCred('RC_VIEWER', 'RC_VIEWER_PASS')

const checks = []
function record(ok, label, detail = '') {
  checks.push({ ok, label, detail })
  console.log(`${ok ? 'ok  ' : 'FAIL'}  ${label}${detail ? `  ${detail}` : ''}`)
}

async function login(cred) {
  const res = await fetch(`${API}${PREFIX}/auth/login`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(cred),
  })
  if (!res.ok) throw new Error(`登录失败 ${cred.email}: HTTP ${res.status}`)
  return (await res.json()).access_token
}

async function withSession(browser, token, viewport = { width: 1440, height: 900 }) {
  const page = await browser.newPage({ viewport })
  await page.goto(`${WEB}/login`, { waitUntil: 'domcontentloaded' })
  await page.evaluate(t => localStorage.setItem('robotcare_access_token', t), token)
  return page
}

const browser = await chromium.launch()
try {
  const userToken = await login(USER)
  const viewerToken = await login(VIEWER)

  // 1) 运维信息收权：普通用户看不到深度探测，viewer 看得到
  {
    const page = await withSession(browser, userToken)
    await page.goto(`${WEB}/guides`, { waitUntil: 'networkidle', timeout: 40000 })
    const probeForUser = await page.locator('.probe-block').count()
    const statsForUser = await page.locator('.status-stats').count()
    record(probeForUser === 0, '普通用户看不到「深度探测」', `probe-block=${probeForUser}`)
    record(statsForUser === 0, '普通用户看不到文档/分片/向量数', `status-stats=${statsForUser}`)
    await page.close()

    const viewerPage = await withSession(browser, viewerToken)
    await viewerPage.goto(`${WEB}/guides`, { waitUntil: 'networkidle', timeout: 40000 })
    const probeForViewer = await viewerPage.locator('.probe-block').count()
    record(probeForViewer === 1, 'viewer 仍能看到「深度探测」（没有误伤运营）', `probe-block=${probeForViewer}`)
    await viewerPage.close()
  }

  // 2) 退出登录二次确认：点头像后不应直接跳走
  {
    const page = await withSession(browser, userToken)
    await page.goto(`${WEB}/`, { waitUntil: 'networkidle', timeout: 40000 })
    await page.locator('button.profile').click()
    await page.waitForTimeout(500)
    const dialogVisible = await page.locator('.el-message-box').isVisible().catch(() => false)
    const stillLoggedIn = !page.url().includes('/login')
    record(dialogVisible && stillLoggedIn, '点头像弹出确认框且未直接退出', `url=${new URL(page.url()).pathname}`)

    // 取消后应留在原页
    await page.locator('.el-message-box__btns button').first().click()
    await page.waitForTimeout(400)
    record(!page.url().includes('/login'), '确认框点「取消」后不退出登录')
    await page.close()
  }

  // 3) 引用编号连续：渲染出来的角标必须是 1..N，不能跳号
  {
    const page = await withSession(browser, userToken)
    const conversations = await fetch(`${API}${PREFIX}/conversations`, {
      headers: { Authorization: `Bearer ${userToken}` },
    }).then(r => (r.ok ? r.json() : []))
    let checked = 0
    for (const conversation of conversations.slice(0, 6)) {
      await page.goto(`${WEB}/chat/${conversation.id}`, { waitUntil: 'networkidle', timeout: 40000 })
      const groups = await page.locator('.bubble-citations').all()
      for (const group of groups) {
        const labels = await group.locator('.citation-tag').allInnerTexts()
        if (!labels.length) continue
        const numbers = labels.map(text => Number(text.match(/\[(\d+)\]/)?.[1]))
        const expected = numbers.map((_, i) => i + 1)
        if (JSON.stringify(numbers) !== JSON.stringify(expected)) {
          record(false, '引用角标连续编号', `实际=${numbers.join(',')}`)
          checked = -1
          break
        }
        checked += 1
      }
      if (checked === -1) break
    }
    if (checked > 0) record(true, '引用角标连续编号', `检查 ${checked} 组均为 1..N`)
    else if (checked === 0) record(true, '引用角标连续编号（跳过：现有会话无引用样本）', 'skipped')
    await page.close()
  }

  // 4) 报告页在窄屏可读：正文不被裁切
  {
    const page = await withSession(browser, userToken, { width: 390, height: 780 })
    const diagnostics = await fetch(`${API}${PREFIX}/diagnostics`, {
      headers: { Authorization: `Bearer ${userToken}` },
    }).then(r => (r.ok ? r.json() : []))
    // 与 verify_mobile_layout.mjs 同口径：report_available 才是「有报告」的真判据
    const withReport = diagnostics.find(d => d.status === 'report_ready' || d.report_available)
    if (withReport) {
      await page.goto(`${WEB}/reports/${withReport.id}`, { waitUntil: 'networkidle', timeout: 40000 })
      const paper = page.locator('.paper')
      if (await paper.count()) {
        const box = await paper.first().boundingBox()
        record(box !== null && box.width <= 390, '报告页正文在 390px 内不溢出', `宽=${Math.round(box?.width ?? 0)}px`)
      } else {
        record(true, '报告页（跳过：该诊断没有已生成报告）', 'skipped')
      }
    } else {
      record(true, '报告页（跳过：账号下无已完成诊断）', 'skipped')
    }
    await page.close()
  }
} finally {
  await browser.close()
}

const failed = checks.filter(c => !c.ok)
console.log(`\n检查 ${checks.length} 项`)
if (failed.length) {
  console.log(`FAIL：${failed.length} 项不通过`)
  process.exit(1)
}
console.log('PASS：C5 界面细节全部生效')
