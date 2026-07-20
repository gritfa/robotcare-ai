import { expect, type Page, test } from '@playwright/test'
import { readFile, stat } from 'node:fs/promises'

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
