<script setup lang="ts">
import { errorMessage, type Schemas } from '@edp/api-client'
import { ElMessage } from 'element-plus'
import { computed, onMounted, ref } from 'vue'

import { api } from '../../api'

/** 企业成员与平台员工的绑定：绑定后员工可以扫码登录、接收提醒，添加的客户默认归他。 */
const emit = defineEmits<{ changed: [] }>()

const members = ref<Schemas['MemberOut'][]>([])
const staff = ref<Schemas['StaffOut'][]>([])
const keyword = ref('')
const loading = ref(false)

const shown = computed(() => {
  const k = keyword.value.trim()
  return k ? members.value.filter((m) => m.name.includes(k) || m.userid.includes(k)) : members.value
})

async function load(): Promise<void> {
  loading.value = true
  const [m, s] = await Promise.all([
    api.GET('/api/v1/admin/integrations/wecom/members'),
    api.GET('/api/v1/staff'),
  ])
  loading.value = false
  if (!m.data) {
    ElMessage.error(errorMessage(m.error))
    return
  }
  members.value = m.data.items
  staff.value = s.data?.items.filter((x) => x.status === 'active') ?? []
}

async function bind(member: Schemas['MemberOut'], staffId: string | null): Promise<void> {
  const { data, error } = await api.PUT('/api/v1/admin/integrations/wecom/members/{userid}', {
    params: { path: { userid: member.userid } },
    body: { staff_id: staffId },
  })
  if (!data) {
    ElMessage.error(errorMessage(error))
    await load()
    return
  }
  ElMessage.success(staffId ? `已绑定到 ${data.staff_name}` : '已解除绑定')
  await load()
  emit('changed')
}

onMounted(load)
</script>

<template>
  <div v-loading="loading" data-testid="wecom-members">
    <div class="bar">
      <el-input
        v-model="keyword"
        placeholder="搜索姓名或账号"
        clearable
        size="small"
        class="search"
      />
      <span class="muted">只有配置了客户联系功能的成员才能添加客户</span>
    </div>
    <el-table :data="shown" size="small">
      <el-table-column label="成员" min-width="160">
        <template #default="{ row }">
          {{ row.name || '（未取到姓名）' }} <span class="muted">{{ row.userid }}</span>
        </template>
      </el-table-column>
      <el-table-column label="客户联系" width="100">
        <template #default="{ row }">
          <el-tag v-if="row.follow" size="small" type="success">已开通</el-tag>
          <span v-else class="muted">—</span>
        </template>
      </el-table-column>
      <el-table-column label="状态" width="80">
        <template #default="{ row }">{{ row.status === 'active' ? '在职' : '已离开' }}</template>
      </el-table-column>
      <el-table-column label="平台员工" min-width="200">
        <template #default="{ row }">
          <el-select
            :model-value="row.staff_id"
            placeholder="未绑定"
            clearable
            filterable
            size="small"
            class="staff"
            :data-testid="`bind-${row.userid}`"
            @update:model-value="(v: string | null | undefined) => bind(row, v || null)"
          >
            <el-option v-for="s in staff" :key="s.id" :label="s.display_name" :value="s.id" />
          </el-select>
        </template>
      </el-table-column>
    </el-table>
  </div>
</template>

<style scoped>
.bar {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 8px;
}

.search {
  width: 220px;
}

.staff {
  width: 180px;
}

.muted {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
</style>
