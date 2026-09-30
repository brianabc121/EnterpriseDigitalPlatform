<script setup lang="ts">
import { errorMessage, type Schemas } from '@edp/api-client'
import { ElMessage } from 'element-plus'
import { ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { api, formatDateTime } from '../api'
import HistoryFeed from '../components/history/HistoryFeed.vue'
import { ACTOR_TYPE, AUDIT_GROUPS, auditActionLabel } from '../labels'

// 两页：修改历史（订单、单据、待办、商品的每一个版本，§25.14）和系统日志（原来的操作日志）。
const route = useRoute()
const router = useRouter()
const tab = ref<'history' | 'system'>(route.query.tab === 'system' ? 'system' : 'history')

const PAGE_SIZE = 50

const items = ref<Schemas['AuditOut'][]>([])
const loading = ref(false)
const group = ref<string | null>(null)
const range = ref<[Date, Date] | null>(null)
const nextBefore = ref<string | null>(null)

async function load(append = false): Promise<void> {
  loading.value = true
  const { data, error } = await api.GET('/api/v1/audit-logs', {
    params: {
      query: {
        action: group.value ?? undefined,
        start: range.value?.[0].toISOString(),
        before: append && nextBefore.value ? nextBefore.value : range.value?.[1].toISOString(),
        limit: PAGE_SIZE,
      },
    },
  })
  loading.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  items.value = append ? [...items.value, ...data.items] : data.items
  nextBefore.value = data.next_before
}

function detailText(detail: Record<string, unknown>): string {
  const text = JSON.stringify(detail)
  return text === '{}' ? '' : text
}

// 系统日志第一次打开时加载。
watch(
  tab,
  (value) => {
    if (value === 'system' && !items.value.length) void load()
    void router.replace({ query: value === 'system' ? { tab: 'system' } : {} })
  },
  { immediate: true },
)
</script>

<template>
  <div>
    <div class="page-header">
      <h2>操作日志</h2>
    </div>
    <el-tabs v-model="tab" data-testid="audit-tabs">
      <el-tab-pane name="history">
        <template #label><span data-testid="audit-tab-history">修改历史</span></template>
        <HistoryFeed v-if="tab === 'history'" />
      </el-tab-pane>
      <el-tab-pane name="system">
        <template #label><span data-testid="audit-tab-system">系统日志</span></template>
        <div class="filters system-filters">
          <el-select
            v-model="group"
            clearable
            placeholder="全部操作"
            style="width: 140px"
            data-testid="audit-group"
            @change="load()"
          >
            <el-option v-for="g in AUDIT_GROUPS" :key="g.value" :label="g.label" :value="g.value" />
          </el-select>
          <el-date-picker
            v-model="range"
            type="datetimerange"
            start-placeholder="开始时间"
            end-placeholder="结束时间"
            style="width: 360px"
            @change="load()"
          />
        </div>
        <el-alert
          type="info"
          :closable="false"
          show-icon
          title="系统日志只能查看，不能修改或删除；登录、权限、导出、查看敏感信息等操作，以及平台运维经授权查看数据的记录都在这里。"
          class="tip"
        />
        <el-table v-loading="loading" :data="items" data-testid="audit-table" empty-text="暂无记录">
          <el-table-column label="时间" width="180">
            <template #default="{ row }">{{ formatDateTime(row.created_at) }}</template>
          </el-table-column>
          <el-table-column label="操作人" width="160">
            <template #default="{ row }">
              {{ row.actor_name ?? '—' }}
              <el-tag v-if="row.actor_type !== 'staff'" size="small" type="warning" disable-transitions>
                {{ ACTOR_TYPE[row.actor_type] ?? row.actor_type }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="操作" width="160">
            <template #default="{ row }">{{ auditActionLabel(row.action) }}</template>
          </el-table-column>
          <el-table-column label="对象" width="200">
            <template #default="{ row }">
              <span v-if="row.resource_type" class="muted">{{ row.resource_type }}</span>
              {{ row.resource_id ?? '' }}
            </template>
          </el-table-column>
          <el-table-column label="详情" min-width="240">
            <template #default="{ row }">
              <span class="detail">{{ detailText(row.detail) }}</span>
            </template>
          </el-table-column>
          <el-table-column prop="ip" label="IP" width="140" />
        </el-table>
        <div v-if="nextBefore" class="page-footer">
          <el-button :loading="loading" @click="load(true)">加载更多</el-button>
        </div>
      </el-tab-pane>
    </el-tabs>
  </div>
</template>

<style scoped>
.filters {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.system-filters {
  margin-bottom: 12px;
}

.tip {
  margin-bottom: 12px;
}

.muted {
  color: var(--el-text-color-secondary);
  margin-right: 4px;
}

.detail {
  font-family: ui-monospace, monospace;
  font-size: 12px;
  word-break: break-all;
}
</style>
