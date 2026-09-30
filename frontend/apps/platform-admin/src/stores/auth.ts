import { errorCode, errorMessage, type Schemas } from '@edp/api-client'
import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import { api, tokens } from '../api'

/** 登录失败；code 为 mfa_required 时需要输入二次验证码。 */
export class LoginError extends Error {
  constructor(
    message: string,
    readonly code: string | null,
  ) {
    super(message)
  }
}

export const useAuthStore = defineStore('platform-auth', () => {
  const me = ref<Schemas['PlatformMe'] | null>(null)
  const isAuthenticated = computed(() => me.value !== null)
  /** 平台要求二次验证而这个账号还没有设置：只能访问账号安全页。 */
  const needsMfaSetup = computed(
    () => me.value !== null && me.value.mfa_required && !me.value.mfa_enabled,
  )

  async function refresh(): Promise<void> {
    const profile = await api.GET('/platform/v1/me')
    if (!profile.data) throw new Error(errorMessage(profile.error))
    me.value = profile.data
  }

  async function login(username: string, password: string, otp?: string): Promise<void> {
    const { data, error } = await api.POST('/platform/v1/auth/login', {
      body: { username, password, otp: otp || null },
    })
    if (!data) throw new LoginError(errorMessage(error, '登录失败'), errorCode(error))
    tokens.set(data.access_token)
    await refresh()
  }

  function logout(): void {
    tokens.set(null)
    me.value = null
  }

  return { me, isAuthenticated, needsMfaSetup, login, logout, refresh }
})
