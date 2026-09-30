<script setup lang="ts">
import { errorMessage, type Schemas } from '@edp/api-client'
import { ElMessage } from 'element-plus'
import { onMounted, reactive, ref } from 'vue'

import { api, formatDateTime } from '../api'
import { ACTOR_TYPE } from '../labels'

const items = ref<Schemas['PlatformAuditOut'][]>([])
const tenants = ref<Schemas['TenantOut'][]>([])
const next = ref<string | null>(null)
const loading = ref(false)
const filters = reactive({ tenantId: '', action: '', actorType: '' })

async function load(more = false): Promise<void> {
  loading.value = true
  const { data, error } = await api.GET('/platform/v1/audit-logs', {
    params: {
      query: {
        tenant_id: filters.tenantId || undefined,
        action: filters.action.trim() || undefined,
        actor_type: filters.actorType || undefined,
        before: more && next.value ? next.value : undefined,
        limit: 50,
      },
    },
  })
  loading.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  items.value = more ? [...items.value, ...data.items] : data.items
  next.value = data.next_before ?? null
}

onMounted(async () => {
  const list = await api.GET('/platform/v1/tenants')
  tenants.value = list.data?.items ?? []
  await load()
})
</script>

<template>
  <div>
    <div class="page-header">
      <h2>审计日志</h2>
      <div class="filters">
        <el-select v-model="filters.tenantId" placeholder="全部租户" clearable filterable @change="load()">
          <el-option v-for="t in tenants" :key="t.id" :label="`${t.name}（${t.code}）`" :value="t.id" />
        </el-select>
        <el-select v-model="filters.actorType" placeholder="全部操作人" clearable @change="load()">
          <el-option v-for="(label, key) in ACTOR_TYPE" :key="key" :label="label" :value="key" />
        </el-select>
        <el-input
          v-model="filters.action"
          placeholder="动作，如 tenant、support.view"
          clearable
          data-testid="audit-action"
          @change="load()"
        />
      </div>
    </div>
    <el-table v-loading="loading" :data="items" data-testid="audit-table" empty-text="暂无记录">
      <el-table-column label="时间" width="170">
        <template #default="{ row }">{{ formatDateTime(row.created_at) }}</template>
      </el-table-column>
      <el-table-column label="租户" width="120">
        <template #default="{ row }">{{ row.tenant_code ?? '平台' }}</template>
      </el-table-column>
      <el-table-column label="操作人" width="150">
        <template #default="{ row }">
          {{ ACTOR_TYPE[row.actor_type] ?? row.actor_type }}
          <span v-if="row.actor_name">· {{ row.actor_name }}</span>
        </template>
      </el-table-column>
      <el-table-column prop="action" label="动作" width="200" />
      <el-table-column label="对象" width="180">
        <template #default="{ row }">
          <span class="sub">{{ row.resource_type }}</span>
          <div class="sub mono">{{ row.resource_id }}</div>
        </template>
      </el-table-column>
      <el-table-column label="详情">
        <template #default="{ row }">
          <code class="detail">{{ JSON.stringify(row.detail) }}</code>
        </template>
      </el-table-column>
      <el-table-column prop="ip" label="IP" width="120" />
    </el-table>
    <div class="more">
      <el-button v-if="next" :loading="loading" @click="load(true)">加载更多</el-button>
    </div>
  </div>
</template>

<style scoped>
.page-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 16px;
  gap: 12px;
}

.page-header h2 {
  margin: 0;
  font-size: 18px;
  white-space: nowrap;
}

.filters {
  display: flex;
  gap: 8px;
}

.detail {
  font-size: 12px;
  word-break: break-all;
}

.mono {
  font-family: monospace;
}

.sub {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.more {
  margin-top: 12px;
  text-align: center;
}
</style>
