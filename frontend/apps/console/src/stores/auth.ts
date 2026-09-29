import { errorMessage, refreshAccessToken, type Permission, type Schemas } from '@edp/api-client'
import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import { api, apiBase, tokens } from '../api'

export const useAuthStore = defineStore('auth', () => {
  const me = ref<Schemas['MeResponse'] | null>(null)
  let restoring: Promise<void> | null = null

  const permissions = computed(() => new Set<Permission>(me.value?.permissions ?? []))
  const isAuthenticated = computed(() => me.value !== null)

  function can(permission: Permission): boolean {
    return permissions.value.has(permission)
  }

  async function fetchMe(): Promise<void> {
    const { data, error } = await api.GET('/api/v1/me')
    if (!data) throw new Error(errorMessage(error))
    me.value = data
  }

  async function login(tenantCode: string, username: string, password: string): Promise<void> {
    const { data, error } = await api.POST('/api/v1/auth/login', {
      body: { tenant_code: tenantCode, username, password },
    })
    if (!data) throw new Error(errorMessage(error, '登录失败'))
    tokens.set(data.access_token)
    await fetchMe()
  }

  /** 企业微信扫码登录或企业微信内免登：用 code 换取令牌。 */
  async function loginWithWecom(corpId: string, code: string): Promise<void> {
    const { data, error } = await api.POST('/api/v1/auth/wecom', {
      body: { corp_id: corpId, code },
    })
    if (!data) throw new Error(errorMessage(error, '企业微信登录失败'))
    tokens.set(data.access_token)
    restoring = Promise.resolve()
    await fetchMe()
  }

  /** 页面刷新后恢复登录：用 httpOnly Cookie 中的刷新令牌换取 Access Token（只执行一次）。 */
  function restore(): Promise<void> {
    restoring ??= (async () => {
      const token = await refreshAccessToken(apiBase)
      if (!token) return
      tokens.set(token)
      try {
        await fetchMe()
      } catch {
        clear()
      }
    })()
    return restoring
  }

  async function logout(): Promise<void> {
    await api.POST('/api/v1/auth/logout')
    clear()
  }

  function clear(): void {
    tokens.set(null)
    me.value = null
  }

  return {
    me,
    permissions,
    isAuthenticated,
    can,
    login,
    loginWithWecom,
    restore,
    logout,
    clear,
  }
})
