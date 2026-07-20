import { rm } from 'node:fs/promises'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const scriptDir = path.dirname(fileURLToPath(import.meta.url))
const e2eDataDir = path.resolve(scriptDir, '..', '..', 'backend', 'data', 'e2e')

await rm(e2eDataDir, { recursive: true, force: true })
console.log(`cleaned isolated E2E data: ${e2eDataDir}`)
