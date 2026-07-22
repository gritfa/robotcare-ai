import axios from 'axios'
import { describe, expect, it } from 'vitest'
import { apiError, parseApiError, userFacingApiError } from './api'

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
    expect(userFacingApiError(axiosError(429, {
      detail: { code: 'RATE_LIMITED', message: '请求过于频繁' },
    }, { 'retry-after': '12' }))).toBe('请求过于频繁；请在 12 秒后重试')
  })

  it('supports an HTTP-date Retry-After header', () => {
    const now = new Date('2026-07-22T02:00:00Z')
    const originalNow = Date.now
    Date.now = () => now.getTime()
    try {
      const retryAt = new Date(now.getTime() + 25_000).toUTCString()
      const parsed = parseApiError(axiosError(429, {
        detail: { code: 'RATE_LIMITED', message: '配额暂时不可用' },
      }, { 'retry-after': retryAt }))
      expect(parsed.retryAfterSeconds).toBe(25)
      expect(userFacingApiError(axiosError(429, {
        detail: { code: 'RATE_LIMITED', message: '配额暂时不可用' },
      }, { 'retry-after': retryAt }))).toContain('25 秒后重试')
    } finally {
      Date.now = originalNow
    }
  })
})
