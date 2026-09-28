import { errorMessage, type Schemas } from '@edp/api-client'
import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import { api, tokens } from '../api'

export const useAuthStore = defineStore('platform-auth', () => {
  const me = ref<Schemas['PlatformMe'] | null>(null)
  const isAuthenticated = computed(() => me.value !== null)

  async function login(username: string, password: string): Promise<void> {
    const { data, error } = await api.POST('/platform/v1/auth/login', {
      body: { username, password },
    })
    if (!data) throw new Error(errorMessage(error, '登录失败'))
    tokens.set(data.access_token)
    const profile = await api.GET('/platform/v1/me')
    if (!profile.data) throw new Error(errorMessage(profile.error))
    me.value = profile.data
  }

  function logout(): void {
    tokens.set(null)
    me.value = null
  }

  return { me, isAuthenticated, login, logout }
})
