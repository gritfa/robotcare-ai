import { beforeAll, beforeEach, describe, expect, it } from 'vitest'
import { applyAuthResult, TOKEN_KEY, USER_KEY } from './authSession'

class MemoryStorage implements Storage {
  private values = new Map<string, string>()
  get length() { return this.values.size }
  clear() { this.values.clear() }
  getItem(key: string) { return this.values.get(key) ?? null }
  key(index: number) { return [...this.values.keys()][index] ?? null }
  removeItem(key: string) { this.values.delete(key) }
  setItem(key: string, value: string) { this.values.set(key, String(value)) }
}

beforeAll(() => {
  Object.defineProperty(globalThis, 'localStorage', { configurable: true, value: new MemoryStorage() })
})

beforeEach(() => localStorage.clear())

describe('auth persistence boundary', () => {
  it('persists only the access token and user, never a refresh token', () => {
    applyAuthResult({
      access_token: 'access-only',
      user: { id: 7, email: 'user@example.com' },
      ...({ refresh_token: 'must-not-be-persisted' } as Record<string, string>),
    })

    expect(localStorage.getItem(TOKEN_KEY)).toBe('access-only')
    expect(localStorage.getItem(USER_KEY)).toContain('user@example.com')
    const storedEntries = Array.from({ length: localStorage.length }, (_, index) => [
      localStorage.key(index),
      localStorage.getItem(localStorage.key(index) || ''),
    ])
    expect(storedEntries).toHaveLength(2)
    expect(JSON.stringify(storedEntries)).not.toContain('must-not-be-persisted')
    expect(JSON.stringify(storedEntries)).not.toContain('refresh_token')
  })
})
