import { describe, expect, it } from 'vitest'
import { diagnosticOptionKey, robotModelIdForDevice, visibleDiagnosticOptions } from './diagnosticOptions'
import type { Device, DiagnosticOption } from './types'

const published: DiagnosticOption = {
  stable_key: 'wifi-setup',
  version: 2,
  issue_category_code: 'wifi_setup_failure',
  issue_category_name: '配网问题',
  title: '无法完成配网',
  status: 'published',
}

describe('diagnostic option selection helpers', () => {
  it('resolves the model from the selected user device', () => {
    const devices: Device[] = [
      { id: 1, nickname: '客厅', robot_model_id: 11 },
      { id: '2', nickname: '卧室', robot_model_id: '22' },
    ]
    expect(robotModelIdForDevice(devices, '2')).toBe('22')
    expect(robotModelIdForDevice(devices, 'missing')).toBeNull()
    expect(robotModelIdForDevice(devices, '')).toBeNull()
  })

  it('uses stable key and version as the UI identity', () => {
    expect(diagnosticOptionKey(published)).toBe('wifi-setup:2')
  })

  it('accepts the status-less published endpoint response and removes duplicates', () => {
    const withoutStatus = { ...published }
    delete withoutStatus.status
    expect(visibleDiagnosticOptions([published, withoutStatus])).toEqual([published])
  })

  it('filters explicitly non-published and malformed options', () => {
    const draft: DiagnosticOption = { ...published, stable_key: 'draft-flow', status: 'draft' }
    const retired: DiagnosticOption = { ...published, stable_key: 'old-flow', status: 'retired' }
    const malformed: DiagnosticOption = { ...published, stable_key: '', title: '' }
    expect(visibleDiagnosticOptions([draft, retired, malformed, published])).toEqual([published])
  })
})
