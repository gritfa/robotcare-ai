import { defineConfig } from '@playwright/test'
import path from 'node:path'

const frontendDir = path.resolve(import.meta.dirname)
const backendDir = path.resolve(frontendDir, '..', 'backend')
const e2eInviteCode = process.env.ROBOTCARE_E2E_INVITE_CODE || '7cYp9N2mK4qR8vTx'
const pythonCommand = process.env.ROBOTCARE_E2E_PYTHON
  || (process.platform === 'win32'
    ? '.\\.venv\\Scripts\\python.exe'
    : 'python')

export default defineConfig({
  testDir: './e2e',
  testMatch: '**/*.e2e.ts',
  fullyParallel: false,
  workers: 1,
  retries: process.env.CI ? 1 : 0,
  timeout: 60_000,
  expect: { timeout: 10_000 },
  reporter: process.env.CI
    ? [['line'], ['html', { open: 'never' }]]
    : [['list'], ['html', { open: 'never' }]],
  use: {
    baseURL: 'http://127.0.0.1:5173',
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    // Local Edge runs use the system browser without downloading Playwright's
    // optional ffmpeg bundle. CI installs Chromium/ffmpeg and retains videos.
    video: process.env.CI ? 'retain-on-failure' : 'off',
  },
  projects: [
    {
      name: 'edge',
      use: { channel: 'msedge' },
    },
    {
      name: 'chromium',
      use: { browserName: 'chromium' },
    },
  ],
  webServer: [
    {
      command: `${pythonCommand} -m uvicorn app.main:app --host 127.0.0.1 --port 8000`,
      cwd: backendDir,
      // The isolated E2E database uses explicit test-only create_all, so it
      // has no Alembic revision row. Use liveness for server startup; migration
      // readiness remains covered by backend integration tests.
      url: 'http://127.0.0.1:8000/health',
      reuseExistingServer: false,
      timeout: 60_000,
      env: {
        ...process.env,
        ROBOTCARE_DATABASE_URL: 'sqlite:///./data/e2e/robotcare-e2e.db',
        ROBOTCARE_ENVIRONMENT: 'development',
        ROBOTCARE_AUTO_CREATE_SCHEMA: 'true',
        ROBOTCARE_REGISTRATION_MODE: 'invite',
        ROBOTCARE_REGISTRATION_INVITE_SECRET: e2eInviteCode,
        ROBOTCARE_ATTACHMENT_DIR: './data/e2e/attachments',
        ROBOTCARE_REPORT_DIR: './data/e2e/reports',
        ROBOTCARE_DASHSCOPE_API_KEY: '',
      },
    },
    {
      command: 'npm run dev -- --host 127.0.0.1',
      cwd: frontendDir,
      url: 'http://127.0.0.1:5173/login',
      reuseExistingServer: false,
      timeout: 60_000,
    },
  ],
})
