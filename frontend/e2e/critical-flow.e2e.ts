import { expect, type Page, test } from '@playwright/test'
import { readFile, stat } from 'node:fs/promises'

const password = 'StrongPass123'
const inviteCode = process.env.ROBOTCARE_E2E_INVITE_CODE || '7cYp9N2mK4qR8vTx'
const onePixelPng = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=', 'base64')

function rateLimitedResponse(seconds: number, message = '操作过于频繁') {
  return {
    status: 429,
    headers: { 'content-type': 'application/json', 'Retry-After': String(seconds) },
    body: JSON.stringify({ detail: { code: 'RATE_LIMITED', message } }),
  }
}

function uniqueEmail(prefix: string) {
  return `${prefix}-${Date.now()}-${Math.random().toString(16).slice(2)}@example.com`
}

async function register(page: Page, email: string, options: { expectSidebarProfile?: boolean } = {}) {
  const { expectSidebarProfile = true } = options
  await page.goto('/register')
  await page.getByPlaceholder('如何称呼你').fill('Edge 测试用户')
  await page.getByPlaceholder('name@example.com').fill(email)
  const passwordInputs = page.locator('input[type="password"]')
  await passwordInputs.nth(0).fill(password)
  await passwordInputs.nth(1).fill(password)
  await page.getByPlaceholder('请输入管理员提供的邀请码').fill(inviteCode)
  await page.getByRole('button', { name: '注册并开始使用' }).click()
  await expect(page).toHaveURL(/\/$/)
  // 邮箱显示在侧栏 profile 里；手机视口下侧栏收进抽屉，此断言不适用
  if (expectSidebarProfile) await expect(page.getByText(email)).toBeVisible()
}

function sidebarLeft(page: Page) {
  return page.evaluate(() => {
    const sidebar = document.querySelector('.sidebar')
    return sidebar ? Math.round(sidebar.getBoundingClientRect().left) : NaN
  })
}

async function logout(page: Page) {
  await page.locator('button.profile').click()
  await expect(page).toHaveURL(/\/login$/)
}

async function addDevice(page: Page, nickname: string, modelCode = 'JH69U1') {
  await page.goto('/devices')
  const firstDeviceButton = page.getByRole('button', {
    name: /添加第一台设备|添加设备/,
  }).first()
  await firstDeviceButton.click()
  const dialog = page.getByRole('dialog', { name: '添加扫地机器人' })
  await dialog.locator('.el-select').click()
  await page.locator('.el-select-dropdown:visible .el-select-dropdown__item')
    .filter({ hasText: modelCode })
    .click()
  await dialog.getByPlaceholder('例如：客厅扫地机器人').fill(nickname)
  await dialog.getByPlaceholder('可在设备铭牌上查看').fill('EDGE-E2E-001')
  await dialog.getByRole('button', { name: '确认添加' }).click()
  await expect(page.getByRole('heading', { name: nickname })).toBeVisible()
  await page.getByRole('link', { name: '为此设备开始诊断 →' }).click()
}

async function createDiagnostic(page: Page, description: string) {
  const options = page.locator('.categories button')
  await expect(options.first()).toBeVisible()
  await options.first().click()
  await page.getByPlaceholder(/扫地机器人清扫途中突然停止/).fill(description)
  await page.getByRole('button', { name: '创建诊断并查看第一步' }).click()
  await expect(page).toHaveURL(/\/diagnostics\/\d+$/)
  await expect(page.getByText('ONE STEP AT A TIME')).toBeVisible()
  return page.url().match(/\/diagnostics\/(\d+)$/)?.[1] || ''
}

async function submitUnresolved(page: Page) {
  const previousPosition = await page.locator('.progress-row b').textContent()
  await page.getByRole('button', { name: '仍未解决，继续下一步' }).click()
  const confirmation = page.locator('.el-message-box')
  await expect(confirmation).toBeVisible()
  const feedbackResponse = page.waitForResponse(response =>
    response.request().method() === 'POST'
    && response.url().includes('/feedback')
    && response.ok(),
  )
  await confirmation.getByRole('button', { name: /OK|确定|确认/ }).click()
  await feedbackResponse
  await expect(confirmation).toBeHidden()
  await expect.poll(async () => {
    if (await page.getByRole('button', { name: '生成售后诊断报告' }).isVisible().catch(() => false)) {
      return true
    }
    const currentPosition = await page.locator('.progress-row b').textContent().catch(() => null)
    return Boolean(currentPosition && currentPosition !== previousPosition)
  }).toBe(true)
}

test('Edge 完整走通注册、设备、分步诊断、报告和 PDF 下载', async ({ page }) => {
  await register(page, uniqueEmail('full-flow'))
  await addDevice(page, 'Edge 完整流程设备')
  await createDiagnostic(page, '设备清扫结束后无法返回基站充电，重新摆放基站后仍未恢复。')

  for (let step = 0; step < 10; step += 1) {
    const unresolved = page.getByRole('button', { name: '仍未解决，继续下一步' })
    if (!await unresolved.isVisible().catch(() => false)) break
    await submitUnresolved(page)
  }

  const createReport = page.getByRole('button', { name: '生成售后诊断报告' })
  await expect(createReport).toBeVisible()
  await createReport.click()
  await expect(page).toHaveURL(/\/reports\/\d+$/)
  await expect(page.getByRole('heading', { name: '售后诊断报告' })).toBeVisible()

  const downloadPromise = page.waitForEvent('download')
  await page.getByRole('button', { name: '下载正式 PDF' }).click()
  const download = await downloadPromise
  expect(download.suggestedFilename()).toMatch(/\.pdf$/)
  expect(await download.failure()).toBeNull()
  const downloadedPath = await download.path()
  expect(downloadedPath).not.toBeNull()
  expect((await stat(downloadedPath!)).size).toBeGreaterThan(0)
  expect((await readFile(downloadedPath!)).subarray(0, 5).toString('ascii')).toBe('%PDF-')
})

test('Edge 刷新页面后恢复同一个诊断步骤', async ({ page }) => {
  await register(page, uniqueEmail('resume'))
  await addDevice(page, 'Edge 恢复流程设备')
  await createDiagnostic(page, '设备无法正常返回充电基站，需要按安全步骤逐项检查。')
  const title = await page.locator('.step-body h2').innerText()

  await page.reload()

  await expect(page.locator('.step-body h2')).toHaveText(title)
  await expect(page.getByText('ONE STEP AT A TIME')).toBeVisible()
})

test('普通用户不能读取其他用户诊断或进入管理员后台', async ({ page }) => {
  await register(page, uniqueEmail('owner'))
  await addDevice(page, '隔离测试设备')
  const diagnosticId = await createDiagnostic(page, '设备无法返回充电基站，清理充电触点后仍未恢复。')
  expect(diagnosticId).not.toBe('')
  await logout(page)

  await register(page, uniqueEmail('other'))
  await page.goto(`/diagnostics/${diagnosticId}`)
  await expect(page).toHaveURL(/\/history$/)
  await expect(page.getByText('设备无法返回充电基站')).not.toBeVisible()

  await page.goto('/admin')
  await expect(page).not.toHaveURL(/\/admin$/)
})

test('分类明显冲突时展示候选，并允许改选后重试', async ({ page }) => {
  await register(page, uniqueEmail('category-mismatch'))
  await addDevice(page, '分类冲突测试设备', 'VC35U1')

  await page.locator('.categories button').filter({ hasText: '清扫异响' }).click()
  await page.getByPlaceholder(/扫地机器人清扫途中突然停止/).fill('Wi-Fi 配网失败，手机一直无法联网')
  await page.getByRole('button', { name: '创建诊断并查看第一步' }).click()

  await expect(page.getByRole('heading', { name: '问题描述与所选类型可能不一致' })).toBeVisible()
  const retry = page.getByRole('button', { name: /改选.*配网失败.*并重试/ })
  await expect(retry).toBeVisible()
  await retry.click()
  await expect(page).toHaveURL(/\/diagnostics\/\d+$/)
})

test('高风险描述在诊断页展示持久安全阻断卡片', async ({ page }) => {
  await register(page, uniqueEmail('safety-block'))
  await addDevice(page, '安全阻断测试设备')

  await page.locator('.categories button').first().click()
  await page.getByPlaceholder(/扫地机器人清扫途中突然停止/).fill('机器人充电时正在冒烟，我想继续拆机检查')
  await page.getByRole('button', { name: '创建诊断并查看第一步' }).click()

  const safetyBlock = page.getByRole('alert').filter({ hasText: '立即停止操作' })
  await expect(safetyBlock).toBeVisible()
  await expect(safetyBlock).toContainText('不要继续充电、拆机或重复测试')
  await expect(page).toHaveURL(/\/diagnostics\/new/)
})

test('注册限流展示 Retry-After 持久提示', async ({ page }) => {
  await page.route('**/api/v1/auth/register', route => route.fulfill(rateLimitedResponse(17, '注册请求过于频繁')))
  await page.goto('/register')
  await page.getByPlaceholder('如何称呼你').fill('限流测试用户')
  await page.getByPlaceholder('name@example.com').fill(uniqueEmail('register-rate-limit'))
  const passwordInputs = page.locator('input[type="password"]')
  await passwordInputs.nth(0).fill(password)
  await passwordInputs.nth(1).fill(password)
  await page.getByPlaceholder('请输入管理员提供的邀请码').fill(inviteCode)
  await page.getByRole('button', { name: '注册并开始使用' }).click()

  await expect(page.getByRole('alert').filter({ hasText: '注册请求过于频繁' })).toContainText('17 秒后重试')
  await expect(page).toHaveURL(/\/register$/)
})

test('附件限流停止后续上传并在会话页提供重试入口', async ({ page }) => {
  await register(page, uniqueEmail('attachment-rate-limit'))
  await addDevice(page, '附件限流测试设备')
  await page.locator('.categories button').first().click()
  await page.getByPlaceholder(/扫地机器人清扫途中突然停止/).fill('设备无法返回基站，需要保存故障图片。')
  await page.locator('input[type="file"]').setInputFiles([
    { name: 'first.png', mimeType: 'image/png', buffer: onePixelPng },
    { name: 'second.png', mimeType: 'image/png', buffer: onePixelPng },
  ])

  let uploadAttempts = 0
  await page.route('**/api/v1/diagnostics/*/attachments', async route => {
    if (route.request().method() !== 'POST') return route.continue()
    uploadAttempts += 1
    return route.fulfill(rateLimitedResponse(9, '图片上传过于频繁'))
  })
  await page.getByRole('button', { name: '创建诊断并查看第一步' }).click()
  await expect(page).toHaveURL(/\/diagnostics\/\d+$/)
  await expect(page.locator('section.attachments .el-alert')).toContainText('9 秒后重试')
  expect(uploadAttempts).toBe(1)

  await page.locator('input[type="file"]').setInputFiles({ name: 'retry.png', mimeType: 'image/png', buffer: onePixelPng })
  await expect(page.getByRole('button', { name: /重试上传（1）/ })).toBeVisible()
  await expect(page.locator('section.attachments .el-alert')).toContainText('9 秒后重试')
})

test('报告和 PDF 限流均展示可重试的持久提示', async ({ page }) => {
  await register(page, uniqueEmail('report-rate-limit'))
  await addDevice(page, '报告限流测试设备')
  await createDiagnostic(page, '设备无法返回基站，按步骤检查后仍未解决。')
  for (let step = 0; step < 10; step += 1) {
    if (!await page.getByRole('button', { name: '仍未解决，继续下一步' }).isVisible().catch(() => false)) break
    await submitUnresolved(page)
  }

  const reportPattern = '**/api/v1/diagnostics/*/report'
  await page.route(reportPattern, route => route.request().method() === 'POST'
    ? route.fulfill(rateLimitedResponse(11, '报告生成过于频繁'))
    : route.continue())
  await page.getByRole('button', { name: '生成售后诊断报告' }).click()
  await expect(page.getByRole('alert').filter({ hasText: '报告生成过于频繁' })).toContainText('11 秒后重试')
  await page.unroute(reportPattern)
  await page.getByRole('button', { name: '生成售后诊断报告' }).click()
  await expect(page).toHaveURL(/\/reports\/\d+$/)

  const pdfPattern = '**/api/v1/diagnostics/*/report/pdf'
  await page.route(pdfPattern, route => route.request().method() === 'POST'
    ? route.fulfill(rateLimitedResponse(13, 'PDF 生成过于频繁'))
    : route.continue())
  await page.getByRole('button', { name: '下载正式 PDF' }).click()
  await expect(page.getByRole('alert').filter({ hasText: 'PDF 生成过于频繁' })).toContainText('13 秒后重试')
  await expect(page.getByRole('button', { name: '下载正式 PDF' })).toBeEnabled()
})

test('聊天交互：Enter 发送、闲聊不走检索、报告指令给出前置提示、引用可查证据', async ({ page }) => {
  await register(page, uniqueEmail('chat-ux'))
  await addDevice(page, '客厅扫地机器人')

  await page.goto('/chat')
  await page.getByRole('complementary').getByRole('button', { name: '新会话' }).click()
  await page.getByRole('button', { name: '开始对话' }).click()
  await expect(page).toHaveURL(/\/chat\/\d+$/)

  // 当前设备用昵称展示，型号作为副标题——普通用户记不住 JH69U1
  await expect(page.getByText('当前设备：客厅扫地机器人')).toBeVisible()
  // 新会话给推荐问题，而不是只有一段使用说明。
  // 推荐问题现在按型号动态生成（真实问过且答得上来 > 有诊断流程 > 通用兜底），
  // 所以断言"有可点的推荐问题"，而不是某句写死的文案。
  const suggested = page.locator('.suggested el-button, .suggested button')
  await expect(suggested.first()).toBeVisible()
  expect(await suggested.count()).toBeGreaterThan(0)
  expect((await suggested.first().innerText()).trim()).not.toBe('')

  const composer = page.locator('.composer textarea')

  // 闲聊：Enter 直接发送，且不该被当成知识缺口拒答
  await composer.fill('你好')
  await composer.press('Enter')
  await expect(page.getByText('我是这台设备的售后知识助手', { exact: false })).toBeVisible()
  await expect(page.getByText('资料中没有找到能回答这个问题的内容')).toHaveCount(0)

  // Shift+Enter 换行不发送
  await composer.fill('第一行')
  await composer.press('Shift+Enter')
  await expect(composer).toHaveValue(/第一行\n/)
  await composer.fill('')

  // 能力介绍：像客服自我介绍，且落到当前型号
  await composer.fill('你能做什么')
  await composer.press('Enter')
  await expect(page.getByText('查询 JH69U1 的使用方法', { exact: false })).toBeVisible()

  // 报告指令：没有诊断时说明前置条件并给出入口，而不是去检索说明书
  await composer.fill('生成报告')
  await composer.press('Enter')
  await expect(page.getByText('需要先完成一次安全分步检查', { exact: false })).toBeVisible()
  await expect(page.getByRole('button', { name: '开始诊断' })).toBeVisible()

  // 知识问题：引用可点开证据抽屉。E2E 环境没有真实生成模型，这里桩住 SSE
  // 只验证前端渲染链路（引用带原文 → 可点击 → 抽屉展示文档/型号/页码/原文），
  // 后端把 snippet 塞进引用由 test_conversations 覆盖。
  const assistantMessage = {
    id: 999, role: 'assistant', content: '请先取下拖布组件检查卡扣 [1]。',
    citations: [{
      index: 1, source_url: 'https://www.haier.com/manual/jh69u1.pdf', page_number: 15,
      score: 0.8123, document_sha256: 'a'.repeat(64),
      snippet: '取下拖布组件，检查卡扣是否到位；若拖布支架未安装到位，拖布不会转动。',
      document_title: 'JH69U1 用户使用说明书',
    }],
    refusal_reason: null, intent: 'knowledge', action_code: null,
    quick_actions: [{ code: 'start_diagnostic', label: '开始分步诊断' }],
    created_at: new Date().toISOString(),
  }
  await page.route('**/messages/stream', async (route) => {
    const sse = [
      `event: user_message\ndata: ${JSON.stringify({ id: 998, role: 'user', content: '拖布不转怎么办', citations: [], refusal_reason: null, created_at: '' })}\n\n`,
      'event: stage\ndata: {"stage":"generating"}\n\n',
      `event: delta\ndata: ${JSON.stringify({ text: assistantMessage.content })}\n\n`,
      `event: assistant_message\ndata: ${JSON.stringify(assistantMessage)}\n\n`,
      'event: done\ndata: {}\n\n',
    ].join('')
    await route.fulfill({ status: 200, headers: { 'content-type': 'text/event-stream' }, body: sse })
  })
  await composer.fill('拖布不转怎么办')
  await composer.press('Enter')
  const citation = page.locator('.citation-tag').first()
  await expect(citation).toBeVisible()
  await citation.click()
  await expect(page.getByRole('heading', { name: '引用证据' })).toBeVisible()
  await expect(page.getByText('JH69U1 用户使用说明书')).toBeVisible()
  await expect(page.getByText('若拖布支架未安装到位', { exact: false })).toBeVisible()
  await expect(page.locator('.evidence')).toContainText('第 15 页')
})

test('管理员知识库后台：文档管理面板可用、缺口闭环入口齐备', async ({ page }) => {
  // E2E 后端没有配置 embedding key，真上传必然 503，所以这条不测入库；
  // 它守的是另一件事：管理页模板本身能渲染出全部运维入口——
  // 单元测试测的是 composable，模板写错只有真跑一遍才发现。
  const adminEmail = process.env.ROBOTCARE_E2E_ADMIN_EMAIL || 'e2e-admin@example.com'
  const adminPassword = process.env.ROBOTCARE_E2E_ADMIN_PASSWORD || 'E2eAdminPass123'

  await page.goto('/login')
  await page.getByPlaceholder('name@example.com').fill(adminEmail)
  await page.getByPlaceholder('请输入密码').fill(adminPassword)
  await page.getByRole('button', { name: '登录' }).click()
  await expect(page).toHaveURL(/\/$/)

  await page.goto('/admin')
  await expect(page).toHaveURL(/\/admin$/)

  // 文档管理面板：列表 + 生命周期操作入口
  const documents = page.getByRole('heading', { name: '知识文档管理' })
  await expect(documents).toBeVisible()
  const documentSection = page.locator('section', { has: documents })
  await expect(documentSection.getByRole('columnheader', { name: '版本' })).toBeVisible()
  await expect(documentSection.getByRole('columnheader', { name: '发布时间' })).toBeVisible()
  await expect(documentSection.getByRole('columnheader', { name: 'SHA256' })).toBeVisible()

  // 上传前差异预览入口
  await expect(page.getByRole('button', { name: '预览差异' })).toBeVisible()

  // 缺口闭环：状态、关联资料、复测三列齐备，闭环说明可见
  const gaps = page.getByRole('heading', { name: /内容缺口榜/ })
  await expect(gaps).toBeVisible()
  const gapSection = page.locator('section', { has: gaps })
  await expect(gapSection.getByRole('columnheader', { name: '状态' })).toBeVisible()
  await expect(gapSection.getByRole('columnheader', { name: '关联资料' })).toBeVisible()
  await expect(gapSection.getByRole('columnheader', { name: '复测' })).toBeVisible()
  await expect(gapSection.getByText('能引用作答才自动标记为已解决', { exact: false })).toBeVisible()
})

// 手机视口专测。全站此前被 styles.css 的 body{min-width:1100px} 强撑到 1100px，
// 各组件里写好的 650~1000px 断点永远触发不到——扫地机坏了的人手里拿的就是手机。
// 这条守住三件事：不出现横向溢出、侧栏默认不占屏、抽屉能开且跳转后自动收起。
test.describe('手机视口（390px）', () => {
  test.use({ viewport: { width: 390, height: 844 } })

  test('手机上无横向溢出，侧栏收进抽屉且跳转后自动收起', async ({ page }) => {
    await register(page, uniqueEmail('mobile'), { expectSidebarProfile: false })

    for (const path of ['/', '/chat', '/guides', '/diagnostics/new', '/history', '/devices']) {
      await page.goto(path)
      await expect(page).toHaveURL(new RegExp(`${path.replace(/\//g, '\\/')}$`))
      const overflow = await page.evaluate(
        () => document.documentElement.scrollWidth - window.innerWidth,
      )
      expect(overflow, `${path} 在 390px 视口不应出现横向溢出`).toBeLessThanOrEqual(1)
    }

    await page.goto('/chat')
    const toggle = page.getByRole('button', { name: '打开导航菜单' })
    await expect(toggle).toBeVisible()
    // 默认收起：侧栏整体移出视口左侧
    expect(await sidebarLeft(page)).toBeLessThan(0)

    await toggle.click()
    await expect.poll(() => sidebarLeft(page)).toBe(0)

    // 点导航跳转后必须自动收起，否则用户落在新页面却仍被遮罩挡住
    await page.locator('.sidebar nav a').filter({ hasText: '使用指导' }).click()
    await expect(page).toHaveURL(/\/guides$/)
    await expect.poll(() => sidebarLeft(page)).toBeLessThan(0)
  })
})
