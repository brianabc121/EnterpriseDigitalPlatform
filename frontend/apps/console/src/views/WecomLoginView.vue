<script setup lang="ts">
import { errorMessage } from '@edp/api-client'
import { onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { api } from '../api'
import { firstAccessiblePath, safeRedirect } from '../menu'
import { useAuthStore } from '../stores/auth'
import { checkLoginState } from '../wecom'

/**
 * 企业微信登录回来的落地页：/wecom/login?corp=...&code=...&state=...
 * - 扫码登录（登录页发起）：核对 state 后登录；
 * - 企业微信内网页授权（应用消息、侧边栏）：直接登录后进入 next 指定的页面；
 * - bind=1：已登录的员工绑定自己的企业微信账号。
 */
const route = useRoute()
const router = useRouter()
const auth = useAuthStore()
const message = ref('正在登录…')
const failed = ref(false)

function query(name: string): string {
  const value = route.query[name]
  return typeof value === 'string' ? value : ''
}

onMounted(async () => {
  const corp = query('corp')
  const code = query('code')
  if (!corp || !code) {
    failed.value = true
    message.value = '企业微信没有返回登录凭证，请重新扫码'
    return
  }
  if (query('bind') === '1') {
    await auth.restore()
    const { error } = await api.POST('/api/v1/me/wecom', { body: { corp_id: corp, code } })
    if (error) {
      failed.value = true
      message.value = errorMessage(error, '绑定失败')
      return
    }
    message.value = '已绑定企业微信账号'
    await router.replace(safeRedirect(query('next'), '/'))
    return
  }
  if (!checkLoginState(query('state'))) {
    failed.value = true
    message.value = '登录请求已失效，请回到登录页重新扫码'
    return
  }
  try {
    await auth.loginWithWecom(corp, code)
  } catch (e) {
    failed.value = true
    message.value = e instanceof Error ? e.message : '企业微信登录失败'
    return
  }
  const saved = sessionStorage.getItem('edp:wecom:next') ?? ''
  sessionStorage.removeItem('edp:wecom:next')
  const next = query('next') || saved
  await router.replace(safeRedirect(next, firstAccessiblePath(auth.permissions)))
})
</script>

<template>
  <div class="page">
    <el-result
      :icon="failed ? 'error' : 'info'"
      :title="failed ? '登录没有完成' : '企业微信登录'"
      :sub-title="message"
      data-testid="wecom-login-result"
    >
      <template v-if="failed" #extra>
        <el-button type="primary" @click="router.replace({ name: 'login' })">返回登录页</el-button>
      </template>
    </el-result>
  </div>
</template>

<style scoped>
.page {
  display: flex;
  align-items: center;
  justify-content: center;
  height: 100%;
}
</style>
