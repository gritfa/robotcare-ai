import type { Device, DiagnosticOption, EntityId } from './types'

export function diagnosticOptionKey(option: DiagnosticOption): string {
  return `${option.stable_key}:${option.version}`
}

export function visibleDiagnosticOptions(options: DiagnosticOption[]): DiagnosticOption[] {
  const seen = new Set<string>()
  return options.filter((option) => {
    if (option.status && option.status !== 'published') return false
    if (!option.stable_key || !option.issue_category_code || !option.title) return false
    const key = diagnosticOptionKey(option)
    if (seen.has(key)) return false
    seen.add(key)
    return true
  })
}

export function robotModelIdForDevice(devices: Device[], deviceId: EntityId | ''): EntityId | null {
  if (deviceId === '') return null
  return devices.find(device => String(device.id) === String(deviceId))?.robot_model_id ?? null
}
