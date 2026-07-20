import axios from 'axios'
import { describe, expect, it } from 'vitest'
import { apiError, parseApiError } from './api'

function axiosError(status: number, data: unknown, headers?: Record<string, string>) {
  return new axios.AxiosError(
    'request failed',
    undefined,
    undefined,
    undefined,
    { status, data, headers: headers || {}, statusText: '', config: {} as never },
  )
}

describe('parseApiError', () => {
  it('parses a string detail', () => {
    expect(apiError(axiosError(400, { detail: 'Bad request' }))).toBe('Bad request')
  })

  it('joins FastAPI validation detail arrays without rendering objects', () => {
    const parsed = parseApiError(axiosError(422, {
      detail: [
        { loc: ['body', 'query'], msg: 'Field required', type: 'missing' },
        { loc: ['body', 'top_k'], msg: 'Must be positive' },
      ],
    }))
    expect(parsed.message).toBe('Field required；Must be positive')
    expect(parsed.validationIssues).toHaveLength(2)
  })

  it('preserves structured safety information', () => {
    const parsed = parseApiError(axiosError(422, {
      detail: {
        code: 'SAFETY_BLOCKED',
        blocked: true,
        risk_level: 'critical',
        reason: '检测到冒烟',
        official_service_advice: '断电并联系官方售后',
      },
    }))
    expect(parsed).toMatchObject({
      code: 'SAFETY_BLOCKED',
      blocked: true,
      reason: '检测到冒烟',
      officialServiceAdvice: '断电并联系官方售后',
    })
    expect(parsed.message).toBe('检测到冒烟')
  })

  it('parses category candidates represented by strings or objects', () => {
    const parsed = parseApiError(axiosError(409, {
      detail: {
        code: 'ISSUE_CATEGORY_MISMATCH',
        selected: 'wifi',
        suggested_categories: ['brush', { issue_category_code: 'battery' }, { code: 'brush' }],
        requires_confirmation: true,
      },
    }))
    expect(parsed.suggestedCategories).toEqual(['brush', 'battery'])
    expect(parsed.selectedCategory).toBe('wifi')
    expect(parsed.requiresConfirmation).toBe(true)
  })

  it('classifies authentication and rate limiting responses', () => {
    expect(parseApiError(axiosError(401, {})).code).toBe('AUTHENTICATION_REQUIRED')
    const limited = parseApiError(axiosError(429, {}, { 'retry-after': '12' }))
    expect(limited.code).toBe('RATE_LIMITED')
    expect(limited.retryAfterSeconds).toBe(12)
  })
})
