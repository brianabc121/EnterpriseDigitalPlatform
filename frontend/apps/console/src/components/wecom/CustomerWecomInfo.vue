<script setup lang="ts">
import type { Schemas } from '@edp/api-client'
import { onMounted, ref } from 'vue'

import { api, formatDateTime } from '../../api'
import { TRANSFER_STATUS } from '../../wecom'

/** 客户在企业微信里的添加人（及备注、标签）、所在客户群、在职继承记录（客户 360）。 */
const props = defineProps<{ customerId: string }>()
const info = ref<Schemas['CustomerWecom'] | null>(null)

onMounted(async () => {
  const { data } = await api.GET('/api/v1/customers/{customer_id}/wecom', {
    params: { path: { customer_id: props.customerId } },
  })
  info.value = data ?? null
})
</script>

<template>
  <section
    v-if="info && (info.follows.length || info.group_chats.length)"
    class="block"
    data-testid="customer-wecom"
  >
    <h3>企业微信</h3>
    <div v-for="f in info.follows" :key="f.userid" class="row" :class="{ deleted: f.deleted }">
      <span>{{ f.staff_name ?? f.member_name ?? f.userid }} 添加</span>
      <span v-if="f.remark" class="muted">备注「{{ f.remark }}」</span>
      <el-tag v-for="t in f.tags" :key="t" size="small" type="info">{{ t }}</el-tag>
      <span v-if="f.deleted" class="muted">已解除</span>
    </div>
    <div v-if="info.group_chats.length" class="groups">
      <div class="muted">所在客户群</div>
      <div v-for="g in info.group_chats" :key="g.chat_id" class="row">
        <span>{{ g.name || '未命名客户群' }}</span>
        <span class="muted"
          >群主 {{ g.owner_name ?? g.owner_userid }} · {{ g.member_count }} 人</span
        >
        <span v-if="g.status === 'dismissed'" class="muted">已解散</span>
      </div>
    </div>
    <div v-for="t in info.transfers.slice(0, 3)" :key="t.id" class="muted">
      在职继承 {{ formatDateTime(t.created_at) }}：{{ TRANSFER_STATUS[t.status] ?? t.status }}
      <template v-if="t.error">（{{ t.error }}）</template>
    </div>
  </section>
</template>

<style scoped>
.block {
  border-bottom: 1px solid var(--el-border-color-lighter);
  padding: 8px 0;
}

h3 {
  font-size: 13px;
  font-weight: 600;
  margin: 4px 0 8px;
}

.row {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 4px 6px;
  font-size: 12px;
  padding: 2px 0;
}

.row.deleted {
  opacity: 0.6;
}

.groups {
  margin-top: 6px;
}

.muted {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
</style>
