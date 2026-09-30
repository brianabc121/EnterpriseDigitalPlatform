<script setup lang="ts">
import { errorMessage } from '@edp/api-client'
import { ElMessage } from 'element-plus'
import { onMounted, ref } from 'vue'

import { api, formatDateTime } from '../../api'
import {
  ACTION_FILTERS,
  actionTag,
  actorColor,
  TYPE_LABEL,
  type FeedItem,
  type RecordType,
} from '../../history'
import HistoryDrawer from './HistoryDrawer.vue'

/**
 * 全部修改历史（设计文档 §25.14，管理员）：所有订单、领料单、入库单、待办、成品和材料的每一个版本，
 * 按类型、操作、操作人、时间、单号或名称筛选；点单号打开这条记录的历史（已经删除的也能看）。
 */
const PAGE_SIZE = 50

const items = ref<FeedItem[]>([])
const loading = ref(false)
const cursor = ref<string | null>(null)
const staff = ref<{ id: string; name: string }[]>([])
const filters = ref({
  type: '' as RecordType | '',
  action: '',
  actor: '',
  q: '',
  range: null as [Date, Date] | null,
})
const viewing = ref<{ open: boolean; type: RecordType; id: string | null; title: string }>({
  open: false,
  type: 'order',
  id: null,
  title: '',
})

async function load(append = false): Promise<void> {
  loading.value = true
  const f = filters.value
  const { data, error } = await api.GET('/api/v1/history', {
    params: {
      query: {
        type: f.type || undefined,
        action: f.action || undefined,
        actor_id: f.actor || undefined,
        q: f.q.trim() || undefined,
        start: f.range?.[0].toISOString(),
        end: f.range?.[1].toISOString(),
        cursor: append ? (cursor.value ?? undefined) : undefined,
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
  cursor.value = data.next_cursor
}

async function loadStaff(): Promise<void> {
  const { data } = await api.GET('/api/v1/staff')
  staff.value = (data?.items ?? []).map((s) => ({ id: s.id, name: s.display_name }))
}

function open(row: FeedItem): void {
  viewing.value = {
    open: true,
    type: row.record_type,
    id: row.record_id,
    title: `${row.type_label} ${row.label}`,
  }
}

onMounted(() => {
  void load()
  void loadStaff()
})
</script>

<template>
  <div>
    <div class="filters">
      <el-select v-model="filters.type" clearable placeholder="全部类型" class="narrow" data-testid="history-type" @change="load()">
        <el-option v-for="(label, value) in TYPE_LABEL" :key="value" :label="label" :value="value" />
      </el-select>
      <el-select v-model="filters.action" placeholder="全部操作" class="narrow" data-testid="history-action" @change="load()">
        <el-option v-for="[value, label] in ACTION_FILTERS" :key="value" :label="label" :value="value" />
      </el-select>
      <el-select v-model="filters.actor" clearable filterable placeholder="全部操作人" class="narrow" data-testid="history-actor" @change="load()">
        <el-option v-for="s in staff" :key="s.id" :label="s.name" :value="s.id" />
      </el-select>
      <el-date-picker
        v-model="filters.range"
        type="datetimerange"
        start-placeholder="开始时间"
        end-placeholder="结束时间"
        class="range"
        @change="load()"
      />
      <el-input
        v-model="filters.q"
        clearable
        placeholder="单号或名称"
        class="search"
        data-testid="history-search"
        @keyup.enter="load()"
        @clear="load()"
      />
    </div>
    <el-alert
      type="info"
      :closable="false"
      show-icon
      class="tip"
      title="订单、领料单、入库单、待办、成品和材料的每一次新建、修改和删除都留下版本，只能查看，不能修改或删除。点单号查看这条记录的全部版本和改动。"
    />
    <el-table v-loading="loading" :data="items" data-testid="history-feed" empty-text="暂无修改记录">
      <el-table-column label="时间" width="170">
        <template #default="{ row }">{{ formatDateTime(row.created_at) }}</template>
      </el-table-column>
      <el-table-column label="操作人" width="140">
        <template #default="{ row }">
          <span class="actor">
            <span class="dot" :style="{ background: actorColor(row.actor_name) }"></span>{{ row.actor_name }}
          </span>
        </template>
      </el-table-column>
      <el-table-column label="类型" width="80" prop="type_label" />
      <el-table-column label="单号或名称" min-width="180">
        <template #default="{ row }">
          <el-button link type="primary" data-testid="history-open" :data-label="row.label" @click="open(row)">{{
            row.label
          }}</el-button>
        </template>
      </el-table-column>
      <el-table-column label="操作" width="110">
        <template #default="{ row }">
          <el-tag size="small" :type="actionTag(row.action)" data-testid="history-action-tag">{{ row.action_label }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="改动" min-width="260" show-overflow-tooltip>
        <template #default="{ row }">
          <span data-testid="history-feed-summary">{{ row.summary || row.reason || row.note || '—' }}</span>
        </template>
      </el-table-column>
    </el-table>
    <div v-if="cursor" class="page-footer">
      <el-button :loading="loading" data-testid="history-more" @click="load(true)">加载更多</el-button>
    </div>
    <HistoryDrawer v-model="viewing.open" :record-type="viewing.type" :record-id="viewing.id" :title="viewing.title" />
  </div>
</template>

<style scoped>
.filters {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 12px;
}

.narrow {
  width: 140px;
}

.range {
  max-width: 360px;
}

.search {
  width: 180px;
}

.tip {
  margin-bottom: 12px;
}

.actor {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}

.dot {
  display: inline-block;
  width: 8px;
  height: 8px;
  border-radius: 50%;
}
</style>
