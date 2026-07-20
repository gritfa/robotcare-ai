import type { AuthResult, User } from './types'

export const TOKEN_KEY = 'robotcare_access_token'
export const USER_KEY = 'robotcare_user'

type AuthStateBinding = {
  getAccessToken: () => string
  applyAuth: (result: AuthResult) => void
  clearAuth: () => void
}

type AuthenticationLostHandler = () => void | Promise<void>

let stateBinding: AuthStateBinding | null = null
let authenticationLostHandler: AuthenticationLostHandler = () => undefined

function storageAvailable() {
  return typeof localStorage !== 'undefined'
}

export function readStoredAccessToken() {
  return storageAvailable() ? localStorage.getItem(TOKEN_KEY) || '' : ''
}

export function readStoredUser(): User | null {
  if (!storageAvailable()) return null
  const saved = localStorage.getItem(USER_KEY)
  if (!saved) return null
  try {
    return JSON.parse(saved) as User
  } catch {
    localStorage.removeItem(USER_KEY)
    return null
  }
}

function persistAuth(result: AuthResult) {
  if (!storageAvailable()) return
  localStorage.setItem(TOKEN_KEY, result.access_token)
  if (result.user) localStorage.setItem(USER_KEY, JSON.stringify(result.user))
  else localStorage.removeItem(USER_KEY)
}

export function persistUser(user: User) {
  if (storageAvailable()) localStorage.setItem(USER_KEY, JSON.stringify(user))
}

export function bindAuthState(binding: AuthStateBinding) {
  stateBinding = binding
  return () => {
    if (stateBinding === binding) stateBinding = null
  }
}

export function setAuthenticationLostHandler(handler: AuthenticationLostHandler) {
  authenticationLostHandler = handler
  return () => {
    if (authenticationLostHandler === handler) authenticationLostHandler = () => undefined
  }
}

export function getAccessToken() {
  return stateBinding?.getAccessToken() || readStoredAccessToken()
}

export function applyAuthResult(result: AuthResult) {
  persistAuth(result)
  stateBinding?.applyAuth(result)
}

export function clearAuthState() {
  if (storageAvailable()) {
    localStorage.removeItem(TOKEN_KEY)
    localStorage.removeItem(USER_KEY)
  }
  stateBinding?.clearAuth()
}

export async function notifyAuthenticationLost() {
  await authenticationLostHandler()
}
