import { spawn } from 'node:child_process'
import { existsSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const scriptDir = path.dirname(fileURLToPath(import.meta.url))
const frontendDir = path.resolve(scriptDir, '..')
const backendDir = path.resolve(frontendDir, '..', 'backend')
const project = process.argv[2] || 'edge'
const forwardedArgs = process.argv.slice(3)
const backendPort = process.env.ROBOTCARE_E2E_BACKEND_PORT || '8000'
const frontendPort = process.env.ROBOTCARE_E2E_FRONTEND_PORT || '5173'
const inviteCode = process.env.ROBOTCARE_E2E_INVITE_CODE || '7cYp9N2mK4qR8vTx'
// 单方言：E2E 也跑在 PostgreSQL 测试容器上（本机默认 55433 的 pgvector 容器，
// CI 用 workflow service）。库在启动前自动创建并清空。
const e2eDatabaseUrl = process.env.ROBOTCARE_E2E_DATABASE_URL
  || 'postgresql+psycopg://postgres:test@127.0.0.1:55433/robotcare_e2e'

const prepareDatabaseScript = `
import os
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.pool import NullPool

url = make_url(os.environ["ROBOTCARE_DATABASE_URL"])
name = url.database
admin = create_engine(url.set(database="postgres"), poolclass=NullPool, isolation_level="AUTOCOMMIT")
with admin.connect() as connection:
    exists = connection.scalar(text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": name})
    if not exists:
        connection.execute(text(f'CREATE DATABASE "{name}"'))
admin.dispose()
engine = create_engine(url, poolclass=NullPool)
with engine.begin() as connection:
    connection.execute(text("DROP SCHEMA public CASCADE"))
    connection.execute(text("CREATE SCHEMA public"))
    connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
engine.dispose()
print("e2e database ready:", name)
`
// macOS/Linux 上裸 `python` 常常不存在（只有 python3），直接用后端 venv 解释器；
// venv 缺失时再退回 python3，仍可用 ROBOTCARE_E2E_PYTHON 覆盖。
const venvPython = process.platform === 'win32'
  ? path.join(backendDir, '.venv', 'Scripts', 'python.exe')
  : path.join(backendDir, '.venv', 'bin', 'python')
const python = process.env.ROBOTCARE_E2E_PYTHON
  || (existsSync(venvPython) ? venvPython : (process.platform === 'win32' ? 'python' : 'python3'))

function start(command, args, options) {
  return spawn(command, args, {
    ...options,
    shell: false,
    stdio: 'inherit',
  })
}

function waitForExit(child) {
  return new Promise(resolve => {
    if (child.exitCode !== null || child.signalCode !== null) return resolve(child.exitCode ?? 1)
    child.once('exit', code => resolve(code ?? 1))
    child.once('error', () => resolve(1))
  })
}

async function waitForUrl(url, children, timeoutMs = 60_000) {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    for (const child of children) {
      if (child.exitCode !== null || child.signalCode !== null) {
        throw new Error(`E2E web server exited before becoming ready: ${url}`)
      }
    }
    try {
      const response = await fetch(url)
      if (response.ok) return
    } catch {
      // The server is still starting.
    }
    await new Promise(resolve => setTimeout(resolve, 250))
  }
  throw new Error(`Timed out waiting for E2E web server: ${url}`)
}

async function stopTree(child) {
  if (!child || child.exitCode !== null || child.signalCode !== null || !child.pid) return
  // Both managed servers are launched directly (no shell/npm intermediary),
  // so terminating the child itself is sufficient and does not require the
  // elevated Windows `taskkill /T` permission.
  child.kill('SIGTERM')
  await Promise.race([
    waitForExit(child),
    new Promise(resolve => setTimeout(resolve, 3_000)),
  ])
  if (child.exitCode === null && child.signalCode === null) {
    child.kill('SIGKILL')
    await Promise.race([
      waitForExit(child),
      new Promise(resolve => setTimeout(resolve, 3_000)),
    ])
  }
}

const sharedEnvironment = {
  ...process.env,
  ROBOTCARE_E2E_BACKEND_PORT: backendPort,
  ROBOTCARE_E2E_FRONTEND_PORT: frontendPort,
  ROBOTCARE_E2E_INVITE_CODE: inviteCode,
  ROBOTCARE_E2E_MANAGED_SERVERS: '1',
  ROBOTCARE_DEV_API_TARGET: `http://127.0.0.1:${backendPort}`,
}

const prepareDatabase = start(
  python,
  ['-c', prepareDatabaseScript],
  {
    cwd: backendDir,
    env: { ...sharedEnvironment, ROBOTCARE_DATABASE_URL: e2eDatabaseUrl },
  },
)
if (await waitForExit(prepareDatabase) !== 0) {
  console.error(`Failed to prepare the E2E PostgreSQL database: ${e2eDatabaseUrl}`)
  process.exit(1)
}

const backend = start(
  python,
  ['-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', backendPort],
  {
    cwd: backendDir,
    env: {
      ...sharedEnvironment,
      ROBOTCARE_DATABASE_URL: e2eDatabaseUrl,
      ROBOTCARE_ENVIRONMENT: 'development',
      ROBOTCARE_AUTO_CREATE_SCHEMA: 'true',
      ROBOTCARE_REGISTRATION_MODE: 'invite',
      ROBOTCARE_REGISTRATION_INVITE_SECRET: inviteCode,
      ROBOTCARE_ATTACHMENT_DIR: './data/e2e/attachments',
      ROBOTCARE_REPORT_DIR: './data/e2e/reports',
      ROBOTCARE_DASHSCOPE_API_KEY: '',
    },
  },
)

const vite = start(
  process.execPath,
  [path.join(frontendDir, 'node_modules', 'vite', 'bin', 'vite.js'), '--host', '127.0.0.1', '--port', frontendPort],
  { cwd: frontendDir, env: sharedEnvironment },
)

let exitCode = 1
try {
  await Promise.all([
    waitForUrl(`http://127.0.0.1:${backendPort}/health`, [backend, vite]),
    waitForUrl(`http://127.0.0.1:${frontendPort}/login`, [backend, vite]),
  ])
  const playwright = start(
    process.execPath,
    [
      path.join(frontendDir, 'node_modules', '@playwright', 'test', 'cli.js'),
      'test',
      `--project=${project}`,
      ...forwardedArgs,
    ],
    { cwd: frontendDir, env: sharedEnvironment },
  )
  exitCode = await waitForExit(playwright)
} catch (error) {
  console.error(error)
} finally {
  await Promise.allSettled([stopTree(vite), stopTree(backend)])
}

process.exitCode = exitCode
