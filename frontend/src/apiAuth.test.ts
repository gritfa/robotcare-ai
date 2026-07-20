import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'
import { AxiosError, AxiosHeaders, type AxiosAdapter, type AxiosResponse, type InternalAxiosRequestConfig } from 'axios'
import { http } from './api'
import { bindAuthState, setAuthenticationLostHandler, TOKEN_KEY, USER_KEY } from './authSession'

class MemoryStorage implements Storage {
  private values = new Map<string, string>()
  get length() { return this.values.size }
  clear() { this.values.clear() }
  getItem(key: string) { return this.values.get(key) ?? null }
  key(index: number) { return [...this.values.keys()][index] ?? null }
  removeItem(key: string) { this.values.delete(key) }
  setItem(key: string, value: string) { this.values.set(key, String(value)) }
}

function response(config: InternalAxiosRequestConfig, status: number, data: unknown): AxiosResponse {
  return { config, status, statusText: String(status), data, headers: new AxiosHeaders() }
}

function unauthorized(config: InternalAxiosRequestConfig) {
  const result = response(config, 401, { detail: 'unauthorized' })
  return Promise.reject(new AxiosError('unauthorized', AxiosError.ERR_BAD_REQUEST, config, undefined, result))
}

function conflict(config: InternalAxiosRequestConfig) {
  const result = response(config, 409, { detail: 'Refresh already rotated; retry with current cookie' })
  ;(result.headers as AxiosHeaders).set('Retry-After', '0')
  return Promise.reject(new AxiosError('conflict', AxiosError.ERR_BAD_REQUEST, config, undefined, result))
}

function forbidden(config: InternalAxiosRequestConfig, detail: string) {
  const result = response(config, 403, { detail })
  return Promise.reject(new AxiosError('forbidden', AxiosError.ERR_BAD_REQUEST, config, undefined, result))
}

const originalAdapter = http.defaults.adapter
const originalNavigatorDescriptor = Object.getOwnPropertyDescriptor(globalThis, 'navigator')

beforeAll(() => {
  Object.defineProperty(globalThis, 'localStorage', { configurable: true, value: new MemoryStorage() })
})

beforeEach(() => {
  localStorage.clear()
  localStorage.setItem(TOKEN_KEY, 'expired-access')
})

afterEach(() => {
  http.defaults.adapter = originalAdapter
  if (originalNavigatorDescriptor) Object.defineProperty(globalThis, 'navigator', originalNavigatorDescriptor)
  else Reflect.deleteProperty(globalThis, 'navigator')
  vi.restoreAllMocks()
})

describe('axios authentication refresh', () => {
  it('uses credentials and shares one refresh request across concurrent 401 responses', async () => {
    const calls = new Map<string, number>()
    const adapter: AxiosAdapter = async (config) => {
      const url = config.url || ''
      calls.set(url, (calls.get(url) || 0) + 1)
      if (url === '/auth/refresh') {
        await Promise.resolve()
        return response(config, 200, {
          access_token: 'renewed-access',
          user: { id: 1, email: 'user@example.com', role: 'user' },
        })
      }
      if (config.headers.get('Authorization') !== 'Bearer renewed-access') return unauthorized(config)
      return response(config, 200, { ok: true })
    }
    http.defaults.adapter = adapter

    const [first, second] = await Promise.all([http.get('/protected-a'), http.get('/protected-b')])

    expect(http.defaults.withCredentials).toBe(true)
    expect(calls.get('/auth/refresh')).toBe(1)
    expect(calls.get('/protected-a')).toBe(2)
    expect(calls.get('/protected-b')).toBe(2)
    expect(first.data).toEqual({ ok: true })
    expect(second.data).toEqual({ ok: true })
    expect(localStorage.getItem(TOKEN_KEY)).toBe('renewed-access')
  })

  it('rechecks shared storage inside a browser lock so another tab can satisfy refresh', async () => {
    const calls = new Map<string, number>()
    Object.defineProperty(globalThis, 'navigator', {
      configurable: true,
      value: {
        locks: {
          request: async (_name: string, callback: () => Promise<unknown>) => {
            localStorage.setItem(TOKEN_KEY, 'renewed-by-other-tab')
            return callback()
          },
        },
      },
    })
    const adapter: AxiosAdapter = async (config) => {
      const url = config.url || ''
      calls.set(url, (calls.get(url) || 0) + 1)
      if (url === '/auth/refresh') throw new Error('refresh should not be called')
      if (config.headers.get('Authorization') !== 'Bearer renewed-by-other-tab') return unauthorized(config)
      return response(config, 200, { ok: true })
    }
    http.defaults.adapter = adapter

    await expect(http.get('/protected-cross-tab')).resolves.toMatchObject({ data: { ok: true } })

    expect(calls.get('/auth/refresh')).toBeUndefined()
    expect(calls.get('/protected-cross-tab')).toBe(2)
  })

  it('retries a short server-side concurrent refresh conflict without clearing auth', async () => {
    let refreshCalls = 0
    const clearAuth = vi.fn()
    const onLost = vi.fn()
    const unbind = bindAuthState({ getAccessToken: () => localStorage.getItem(TOKEN_KEY) || '', applyAuth: vi.fn(), clearAuth })
    const unsetHandler = setAuthenticationLostHandler(onLost)
    const adapter: AxiosAdapter = async (config) => {
      if (config.url === '/auth/refresh') {
        refreshCalls += 1
        if (refreshCalls === 1) return conflict(config)
        return response(config, 200, { access_token: 'renewed-after-conflict' })
      }
      if (config.headers.get('Authorization') !== 'Bearer renewed-after-conflict') return unauthorized(config)
      return response(config, 200, { ok: true })
    }
    http.defaults.adapter = adapter

    await expect(http.get('/protected-conflict')).resolves.toMatchObject({ data: { ok: true } })

    expect(refreshCalls).toBe(2)
    expect(clearAuth).not.toHaveBeenCalled()
    expect(onLost).not.toHaveBeenCalled()
    unbind()
    unsetHandler()
  })

  it('clears bound and persisted auth state once when refresh fails', async () => {
    localStorage.setItem(USER_KEY, JSON.stringify({ id: 1, email: 'old@example.com' }))
    const clearAuth = vi.fn()
    const onLost = vi.fn()
    const unbind = bindAuthState({ getAccessToken: () => 'expired-access', applyAuth: vi.fn(), clearAuth })
    const unsetHandler = setAuthenticationLostHandler(onLost)
    const adapter: AxiosAdapter = (config) => unauthorized(config)
    http.defaults.adapter = adapter

    await expect(http.get('/protected')).rejects.toMatchObject({ response: { status: 401 } })

    expect(clearAuth).toHaveBeenCalledTimes(1)
    expect(onLost).toHaveBeenCalledTimes(1)
    expect(localStorage.getItem(TOKEN_KEY)).toBeNull()
    expect(localStorage.getItem(USER_KEY)).toBeNull()
    unbind()
    unsetHandler()
  })

  it('clears authentication when the backend reports a disabled account', async () => {
    localStorage.setItem(USER_KEY, JSON.stringify({ id: 1, email: 'disabled@example.com' }))
    const clearAuth = vi.fn()
    const onLost = vi.fn()
    const unbind = bindAuthState({ getAccessToken: () => 'expired-access', applyAuth: vi.fn(), clearAuth })
    const unsetHandler = setAuthenticationLostHandler(onLost)
    http.defaults.adapter = ((config) => forbidden(config, 'Account disabled')) as AxiosAdapter

    await expect(http.get('/protected-disabled')).rejects.toMatchObject({ response: { status: 403 } })

    expect(clearAuth).toHaveBeenCalledTimes(1)
    expect(onLost).toHaveBeenCalledTimes(1)
    expect(localStorage.getItem(TOKEN_KEY)).toBeNull()
    expect(localStorage.getItem(USER_KEY)).toBeNull()
    unbind()
    unsetHandler()
  })

  it.each(['/auth/login', '/auth/register', '/auth/refresh', '/auth/logout'])(
    'does not refresh recursively when %s returns 401',
    async (endpoint) => {
    const calls: string[] = []
    http.defaults.adapter = ((config) => {
      calls.push(config.url || '')
      return unauthorized(config)
    }) as AxiosAdapter

    await expect(http.post(endpoint, { email: 'x', password: 'x' })).rejects.toBeInstanceOf(AxiosError)

    expect(calls).toEqual([endpoint])
    },
  )
})
