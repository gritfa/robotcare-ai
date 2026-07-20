import { expect, type Page, test } from '@playwright/test'

const password = 'StrongPass123'
const inviteCode = process.env.ROBOTCARE_E2E_INVITE_CODE || '7cYp9N2mK4qR8vTx'

function uniqueEmail(prefix: string) {
  return `${prefix}-${Date.now()}-${Math.random().toString(16).slice(2)}@example.com`
}

async function register(page: Page, email: string) {
  await page.goto('/register')
  await page.getByPlaceholder('如何称呼你').fill('Edge 测试用户')
  await page.getByPlaceholder('name@example.com').fill(email)
  const passwordInputs = page.locator('input[type="password"]')
  await passwordInputs.nth(0).fill(password)
  await passwordInputs.nth(1).fill(password)
  await page.getByPlaceholder('请输入管理员提供的邀请码').fill(inviteCode)
  await page.getByRole('button', { name: '注册并开始使用' }).click()
  await expect(page).toHaveURL(/\/$/)
  await expect(page.getByText(email)).toBeVisible()
}

async function logout(page: Page) {
  await page.locator('button.profile').click()
  await expect(page).toHaveURL(/\/login$/)
}

async function addDevice(page: Page, nickname: string) {
  await page.goto('/devices')
  const firstDeviceButton = page.getByRole('button', {
    name: /添加第一台设备|添加设备/,
  }).first()
  await firstDeviceButton.click()
  const dialog = page.getByRole('dialog', { name: '添加扫地机器人' })
  await dialog.locator('.el-select').click()
  await page.locator('.el-select-dropdown:visible .el-select-dropdown__item')
    .filter({ hasText: 'JH69U1' })
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
  await createDiagnostic(page, '主刷转动异常，清理可见毛发后仍未恢复。')

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
  expect((await download.createReadStream())?.readable).toBe(true)
})

test('Edge 刷新页面后恢复同一个诊断步骤', async ({ page }) => {
  await register(page, uniqueEmail('resume'))
  await addDevice(page, 'Edge 恢复流程设备')
  await createDiagnostic(page, '设备无法正常启动，需要按安全步骤逐项检查。')
  const title = await page.locator('.step-body h2').innerText()

  await page.reload()

  await expect(page.locator('.step-body h2')).toHaveText(title)
  await expect(page.getByText('ONE STEP AT A TIME')).toBeVisible()
})

test('普通用户不能读取其他用户诊断或进入管理员后台', async ({ page }) => {
  await register(page, uniqueEmail('owner'))
  await addDevice(page, '隔离测试设备')
  const diagnosticId = await createDiagnostic(page, '主刷被毛发缠绕，清理可见异物后仍无法转动。')
  expect(diagnosticId).not.toBe('')
  await logout(page)

  await register(page, uniqueEmail('other'))
  await page.goto(`/diagnostics/${diagnosticId}`)
  await expect(page).toHaveURL(/\/history$/)
  await expect(page.getByText('主刷被毛发缠绕')).not.toBeVisible()

  await page.goto('/admin')
  await expect(page).not.toHaveURL(/\/admin$/)
})
