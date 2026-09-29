<script setup lang="ts">
import { errorMessage, type Schemas } from '@edp/api-client'
import { ElMessage } from 'element-plus'
import { computed, onMounted, reactive, ref } from 'vue'

import { api, formatDateTime } from '../../api'
import { TRANSFER_KIND, transferSummary } from '../../wecom'

/**
 * 离职继承（设计 §14.3）：企业微信里离职成员的客户进入"待分配"，分配给接手的员工——平台归属随之
 * 变更，企业微信里走离职继承；可以同时把他作为群主的客户群转给接手的员工（客户群继承）。
 */
const emit = defineEmits<{ changed: [] }>()

interface Handover {
  userid: string
  name: string
  dimission: string | null
  items: Schemas['UnassignedCustomerOut'][]
}

const items = ref<Schemas['UnassignedCustomerOut'][]>([])
const members = ref<Schemas['MemberOut'][]>([])
const transfers = ref<Schemas['GroupTransferOut'][]>([])
const loading = ref(false)
const assigning = ref('')
const targets = reactive<Record<string, string>>({})
const withGroups = reactive<Record<string, boolean>>({})

const handovers = computed<Handover[]>(() => {
  const map = new Map<string, Handover>()
  for (const item of items.value) {
    const entry = map.get(item.handover_userid) ?? {
      userid: item.handover_userid,
      name: item.handover_name ?? item.handover_userid,
      dimission: item.dimission_time,
      items: [],
    }
    entry.items.push(item)
    map.set(item.handover_userid, entry)
  }
  return [...map.values()]
})

// 接手的员工需要绑定了在职的企业微信成员。
const takeovers = computed(() =>
  members.value
    .filter((m) => m.status === 'active' && m.staff_id)
    .map((m) => ({ staffId: m.staff_id!, label: `${m.staff_name}（${m.name || m.userid}）` })),
)

async function load(): Promise<void> {
  loading.value = true
  const [unassigned, bound, groups] = await Promise.all([
    api.GET('/api/v1/admin/integrations/wecom/unassigned'),
    api.GET('/api/v1/admin/integrations/wecom/members'),
    api.GET('/api/v1/admin/integrations/wecom/group-transfers'),
  ])
  loading.value = false
  if (!unassigned.data) {
    ElMessage.error(errorMessage(unassigned.error))
    return
  }
  items.value = unassigned.data.items
  members.value = bound.data?.items ?? []
  transfers.value = groups.data?.items ?? []
  for (const h of handovers.value) withGroups[h.userid] ??= true
}

async function assign(handover: Handover): Promise<void> {
  const target = targets[handover.userid]
  if (!target) {
    ElMessage.warning('请选择接手的员工')
    return
  }
  assigning.value = handover.userid
  const { data, error } = await api.POST('/api/v1/admin/integrations/wecom/unassigned/assign', {
    body: {
      handover_userid: handover.userid,
      to_owner_id: target,
      transfer_groups: withGroups[handover.userid] ?? true,
    },
  })
  assigning.value = ''
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  ElMessage.success(transferSummary(data.transferred, data.wecom))
  await load()
  emit('changed')
}

onMounted(load)
</script>

<template>
  <div v-loading="loading" data-testid="wecom-resigned">
    <p class="muted intro">
      企业微信里成员离职后，他的客户进入"待分配"。分配给接手的员工：平台上的归属随之变更，
      企业微信里走离职继承（客户 24 小时后自动接替）；可以同时转移他作为群主的客户群。
    </p>
    <el-empty
      v-if="!handovers.length"
      :image-size="60"
      description="没有待分配的离职成员客户"
      data-testid="resigned-empty"
    />
    <el-card
      v-for="h in handovers"
      :key="h.userid"
      shadow="never"
      class="handover"
      data-testid="resigned-member"
    >
      <div class="head">
        <div>
          <b>{{ h.name }}</b>
          <span class="muted">
            · {{ h.items.length }} 位客户 · 离职于
            {{ h.dimission ? formatDateTime(h.dimission) : '—' }}
          </span>
        </div>
        <div class="actions">
          <el-select
            v-model="targets[h.userid]"
            filterable
            size="small"
            placeholder="接手的员工"
            no-data-text="没有绑定企业微信的员工"
            class="select"
            data-testid="resigned-target"
          >
            <el-option
              v-for="t in takeovers"
              :key="t.staffId"
              :label="t.label"
              :value="t.staffId"
            />
          </el-select>
          <el-checkbox v-model="withGroups[h.userid]">同时转移客户群</el-checkbox>
          <el-button
            type="primary"
            size="small"
            :loading="assigning === h.userid"
            data-testid="resigned-assign"
            @click="assign(h)"
          >
            分配
          </el-button>
        </div>
      </div>
      <el-table :data="h.items" size="small">
        <el-table-column label="客户" min-width="180">
          <template #default="{ row }">
            {{ row.customer_name ?? row.external_userid }}
            <el-tag v-if="!row.customer_id" size="small" type="info">未同步</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="平台归属" min-width="120">
          <template #default="{ row }">{{ row.owner_name ?? '—' }}</template>
        </el-table-column>
      </el-table>
    </el-card>

    <h4>客户群继承记录</h4>
    <el-table :data="transfers" size="small" empty-text="还没有转移过客户群" data-testid="group-transfers">
      <el-table-column label="客户群" min-width="160">
        <template #default="{ row }">{{ row.group_name ?? row.chat_id }}</template>
      </el-table-column>
      <el-table-column label="原群主 → 新群主" min-width="180">
        <template #default="{ row }">
          {{ row.handover_name ?? row.handover_userid }} →
          {{ row.takeover_name ?? row.takeover_userid }}
        </template>
      </el-table-column>
      <el-table-column label="方式" width="100">
        <template #default="{ row }">{{ TRANSFER_KIND[row.kind] ?? row.kind }}</template>
      </el-table-column>
      <el-table-column label="结果" min-width="160">
        <template #default="{ row }">
          <span v-if="row.status === 'success'" class="ok">成功</span>
          <span v-else class="error">{{ row.error ?? '失败' }}</span>
        </template>
      </el-table-column>
      <el-table-column label="时间" width="170">
        <template #default="{ row }">{{ formatDateTime(row.created_at) }}</template>
      </el-table-column>
    </el-table>
  </div>
</template>

<style scoped>
.intro {
  margin-top: 0;
}

.handover {
  margin-bottom: 12px;
}

.head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 8px;
}

.actions {
  display: flex;
  align-items: center;
  gap: 12px;
}

.select {
  width: 220px;
}

.muted {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.ok {
  color: var(--el-color-success);
}

.error {
  color: var(--el-color-danger);
}
</style>
