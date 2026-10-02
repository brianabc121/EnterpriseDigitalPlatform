<script setup lang="ts">
import { errorMessage, type Schemas } from '@edp/api-client'
import { ElMessage, ElMessageBox } from 'element-plus'
import { onMounted, ref } from 'vue'

import { api, formatDateTime } from '../../api'
import { PROVIDER_NAME, type Identity } from '../../assistant'

/** 绑定管理：每个机器人下出现过的 IM 账号，手工对应到员工或解除。 */
const items = ref<Identity[]>([])
const staff = ref<Schemas['StaffOption'][]>([])
const loading = ref(false)
const busy = ref<string | null>(null)

async function load(): Promise<void> {
  loading.value = true
  const [list, options] = await Promise.all([
    api.GET('/api/v1/assistant/identities'),
    api.GET('/api/v1/tasks/staff'),
  ])
  loading.value = false
  if (!list.data) {
    ElMessage.error(errorMessage(list.error))
    return
  }
  items.value = list.data.items
  staff.value = options.data?.items ?? []
}

async function bind(identity: Identity, staffId: string): Promise<void> {
  if (!staffId) return
  busy.value = identity.id
  const { data, error } = await api.PUT('/api/v1/assistant/identities/{identity_id}', {
    params: { path: { identity_id: identity.id } },
    body: { staff_id: staffId },
  })
  busy.value = null
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  await load()
}

async function unbind(identity: Identity): Promise<void> {
  try {
    await ElMessageBox.confirm(`解除 ${identity.display_name || identity.external_user_id} 与 ${identity.staff_name} 的对应？`, '解除绑定', {
      confirmButtonText: '解除',
      cancelButtonText: '取消',
      type: 'warning',
    })
  } catch {
    return
  }
  busy.value = identity.id
  const { error } = await api.DELETE('/api/v1/assistant/identities/{identity_id}', {
    params: { path: { identity_id: identity.id } },
  })
  busy.value = null
  if (error) {
    ElMessage.error(errorMessage(error))
    return
  }
  await load()
}

onMounted(load)
</script>

<template>
  <div>
    <p class="muted">员工通常自己用绑定码绑定；这里可以把出现过的 IM 账号手工对应到员工，或解除。</p>
    <el-table v-loading="loading" :data="items" data-testid="identities-table" empty-text="还没有人和助理说过话">
      <el-table-column label="渠道" width="160">
        <template #default="{ row }">{{ PROVIDER_NAME[row.provider] }} · {{ row.bot_name }}</template>
      </el-table-column>
      <el-table-column label="IM 账号" min-width="200">
        <template #default="{ row }">
          <div>{{ row.display_name || '—' }}</div>
          <div class="muted">{{ row.external_user_id }}</div>
        </template>
      </el-table-column>
      <el-table-column label="员工" width="220">
        <template #default="{ row }">
          <template v-if="row.staff_id">
            {{ row.staff_name }}
            <el-button link type="danger" size="small" :loading="busy === row.id" @click="unbind(row)">解除</el-button>
          </template>
          <el-select
            v-else
            size="small"
            placeholder="对应到员工"
            filterable
            :loading="busy === row.id"
            :data-testid="`identity-bind-${row.external_user_id}`"
            @change="(v: string) => bind(row, v)"
          >
            <el-option v-for="s in staff" :key="s.id" :label="s.name" :value="s.id" />
          </el-select>
        </template>
      </el-table-column>
      <el-table-column label="绑定时间" width="170">
        <template #default="{ row }">{{ row.bound_at ? formatDateTime(row.bound_at) : '—' }}</template>
      </el-table-column>
      <el-table-column label="最近活动" width="170">
        <template #default="{ row }">{{ row.last_seen_at ? formatDateTime(row.last_seen_at) : '—' }}</template>
      </el-table-column>
    </el-table>
  </div>
</template>

<style scoped>
.muted {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
</style>
