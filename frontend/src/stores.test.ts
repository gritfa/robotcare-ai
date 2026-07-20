import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import type { Diagnostic, DiagnosticStep, DiagnosticStatus } from './types'

const apiMocks = vi.hoisted(() => ({
  get: vi.fn(),
  currentStep: vi.fn(),
  feedback: vi.fn(),
}))

vi.mock('./api', () => ({
  TOKEN_KEY: 'robotcare_access_token',
  authApi: { login: vi.fn(), register: vi.fn(), me: vi.fn() },
  diagnosticApi: apiMocks,
}))

import { useDiagnosticStore } from './stores'

const step: DiagnosticStep = {
  id: 31,
  position: 1,
  title: '检查主刷',
  instruction: '关闭电源后清理缠绕物。',
  source_label: 'JH69U1 说明书第 15 页',
}

function diagnostic(status: DiagnosticStatus): Diagnostic {
  return {
    id: 7,
    device_id: 2,
    issue_description: '主刷无法转动',
    status,
  }
}

describe('diagnostic store resume behavior', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
  })

  it('loads the detail first and only requests a current step for an in-progress diagnostic', async () => {
    apiMocks.get.mockResolvedValue(diagnostic('in_progress'))
    apiMocks.currentStep.mockResolvedValue(step)

    const store = useDiagnosticStore()
    await store.load('7')

    expect(apiMocks.get).toHaveBeenCalledWith('7')
    expect(apiMocks.currentStep).toHaveBeenCalledWith('7')
    expect(apiMocks.get.mock.invocationCallOrder[0]).toBeLessThan(apiMocks.currentStep.mock.invocationCallOrder[0])
    expect(store.currentStep).toEqual(step)
  })

  it.each<DiagnosticStatus>(['resolved', 'unresolved', 'report_ready'])('does not request a current step when reopening a %s diagnostic', async (status) => {
    apiMocks.get.mockResolvedValue(diagnostic(status))

    const store = useDiagnosticStore()
    await store.load('7')

    expect(apiMocks.currentStep).not.toHaveBeenCalled()
    expect(store.active?.status).toBe(status)
    expect(store.currentStep).toBeNull()
  })

  it('clears a stale step when a refreshed diagnostic has already ended', async () => {
    apiMocks.get
      .mockResolvedValueOnce(diagnostic('in_progress'))
      .mockResolvedValueOnce(diagnostic('resolved'))
    apiMocks.currentStep.mockResolvedValue(step)

    const store = useDiagnosticStore()
    await store.load('7')
    await store.load('7')

    expect(apiMocks.currentStep).toHaveBeenCalledTimes(1)
    expect(store.active?.status).toBe('resolved')
    expect(store.currentStep).toBeNull()
  })

  it('submits feedback with the loaded step id and applies the returned terminal state', async () => {
    apiMocks.get.mockResolvedValue(diagnostic('in_progress'))
    apiMocks.currentStep.mockResolvedValue(step)
    apiMocks.feedback.mockResolvedValue({ diagnostic: diagnostic('resolved'), current_step: null })

    const store = useDiagnosticStore()
    await store.load('7')
    await store.submitFeedback(true)

    expect(apiMocks.feedback).toHaveBeenCalledWith(7, 31, true)
    expect(store.active?.status).toBe('resolved')
    expect(store.currentStep).toBeNull()
  })
})
