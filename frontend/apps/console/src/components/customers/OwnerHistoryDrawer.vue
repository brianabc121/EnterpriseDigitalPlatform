<script setup lang="ts">
import { errorMessage, type Schemas } from '@edp/api-client'
import { ElMessage } from 'element-plus'
import { ref, watch } from 'vue'

import { api, formatDateTime } from '../../api'
import { TRANSFER_STATUS } from '../../wecom'

const props = defineProps<{ customer: Schemas['CustomerOut'] | null }>()
const visible = defineModel<boolean>({ required: true })
const items = ref<Schemas['OwnerHistoryOut'][]>([])

const REASON: Record<string, string> = {
  session_transfer: '会话转接',
  manual: '管理员转移',
  handover: '离职交接',
  wecom: '企业微信添加人',
}

watch(visible, async (open) => {
  if (!open || !props.customer) return
  const { data, error } = await api.GET('/api/v1/customers/{customer_id}/owner-history', {
    params: { path: { customer_id: props.customer.id } },
  })
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  items.value = data.items
})
</script>

<template>
  <el-drawer v-model="visible" :title="`归属记录 · ${customer?.display_name ?? ''}`" size="420px">
    <el-timeline v-if="items.length">
      <el-timeline-item
        v-for="h in items"
        :key="h.id"
        :timestamp="formatDateTime(h.created_at)"
        placement="top"
      >
        <div>{{ h.from_owner_name ?? '无归属' }} → {{ h.to_owner_name ?? '无归属' }}</div>
        <div class="meta">
          {{ REASON[h.reason] ?? h.reason }}
          <template v-if="h.actor_name"> · 操作人 {{ h.actor_name }}</template>
          <template v-if="h.note"> · {{ h.note }}</template>
          <template v-if="h.wecom_sync_status">
            · 企业微信{{ TRANSFER_STATUS[h.wecom_sync_status] ?? h.wecom_sync_status }}
          </template>
        </div>
      </el-timeline-item>
    </el-timeline>
    <el-empty v-else description="没有归属变更记录" />
  </el-drawer>
</template>

<style scoped>
.meta {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
</style>
