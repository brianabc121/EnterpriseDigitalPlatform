<script setup lang="ts">
import { ElMessage } from 'element-plus'
import { computed, onBeforeUnmount, ref } from 'vue'

import { useWorkbenchStore } from '../../stores/workbench'

const wb = useWorkbenchStore()
const now = ref(Date.now())
const busy = ref(false)
const timer = setInterval(() => (now.value = Date.now()), 1000)
onBeforeUnmount(() => clearInterval(timer))

const request = computed(() => wb.incoming[0] ?? null)
const secondsLeft = computed(() => {
  const expires = request.value?.expires_at
  return expires ? Math.max(0, Math.ceil((Date.parse(expires) - now.value) / 1000)) : 0
})

async function decide(decision: 'accept' | 'reject'): Promise<void> {
  if (!request.value) return
  busy.value = true
  try {
    await wb.decideTransfer(request.value, decision)
    if (decision === 'accept') ElMessage.success('已接手会话')
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : String(e))
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <el-dialog
    :model-value="request !== null && secondsLeft > 0"
    title="转接请求"
    width="400px"
    :close-on-click-modal="false"
    :show-close="false"
    data-testid="incoming-transfer"
  >
    <template v-if="request">
      <p>
        <strong>{{ request.from_staff_name ?? '同事' }}</strong>
        请求将客户 <strong>{{ request.customer_display_name ?? '' }}</strong> 转接给您。
      </p>
      <p v-if="request.note" class="note">备注：{{ request.note }}</p>
      <p class="countdown">{{ secondsLeft }} 秒后自动取消</p>
    </template>
    <template #footer>
      <el-button :loading="busy" data-testid="reject-transfer" @click="decide('reject')">
        拒绝
      </el-button>
      <el-button
        type="primary"
        :loading="busy"
        data-testid="accept-transfer"
        @click="decide('accept')"
      >
        接受
      </el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.note {
  background: var(--el-fill-color-light);
  padding: 6px 10px;
  border-radius: 4px;
}

.countdown {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
</style>
