<script setup lang="ts">
import { errorMessage, type Schemas } from '@edp/api-client'
import { ElMessage } from 'element-plus'
import { computed, onMounted, reactive, ref } from 'vue'

import { api, formatDateTime } from '../api'
import OwnerHistoryDrawer from '../components/customers/OwnerHistoryDrawer.vue'
import OwnerTransferDialog from '../components/customers/OwnerTransferDialog.vue'
import { CUSTOMER_SOURCE } from '../labels'
import { useAuthStore } from '../stores/auth'

const PAGE_SIZE = 20

const auth = useAuthStore()
const items = ref<Schemas['CustomerOut'][]>([])
const total = ref(0)
const page = ref(1)
const loading = ref(false)

const canCreate = computed(() => auth.can('customer:create'))
const canAssign = computed(() => auth.can('customer:assign') && auth.can('staff:read'))
const seesAll = computed(() => auth.can('customer:read_all'))
const canTransfer = computed(() => auth.can('customer:assign'))

const selected = ref<Schemas['CustomerOut'][]>([])
const transferOpen = ref(false)
const historyOpen = ref(false)
const historyOf = ref<Schemas['CustomerOut'] | null>(null)

function showHistory(customer: Schemas['CustomerOut']): void {
  historyOf.value = customer
  historyOpen.value = true
}

const dialogVisible = ref(false)
const saving = ref(false)
const staffOptions = ref<Schemas['StaffOut'][]>([])
const form = reactive<{ displayName: string; ownerId: string | null }>({
  displayName: '',
  ownerId: null,
})

async function load(): Promise<void> {
  loading.value = true
  const { data, error } = await api.GET('/api/v1/customers', {
    params: { query: { limit: PAGE_SIZE, offset: (page.value - 1) * PAGE_SIZE } },
  })
  loading.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  items.value = data.items
  total.value = data.total
}

async function openCreate(): Promise<void> {
  form.displayName = ''
  form.ownerId = null
  dialogVisible.value = true
  if (canAssign.value && staffOptions.value.length === 0) {
    const { data } = await api.GET('/api/v1/staff')
    staffOptions.value = data?.items.filter((s) => s.status === 'active') ?? []
  }
}

async function create(): Promise<void> {
  if (!form.displayName.trim()) {
    ElMessage.warning('请输入客户名称')
    return
  }
  saving.value = true
  const { data, error } = await api.POST('/api/v1/customers', {
    body: { display_name: form.displayName.trim(), owner_id: form.ownerId },
  })
  saving.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  ElMessage.success('已创建客户')
  dialogVisible.value = false
  page.value = 1
  await load()
}

onMounted(load)
</script>

<template>
  <div>
    <div class="page-header">
      <h2>客户</h2>
      <div>
        <el-button
          v-if="canTransfer"
          :disabled="selected.length === 0"
          data-testid="transfer-owner"
          @click="transferOpen = true"
        >
          转移归属
        </el-button>
        <el-button v-if="canCreate" type="primary" @click="openCreate">新建客户</el-button>
      </div>
    </div>
    <el-alert
      v-if="!seesAll"
      type="info"
      :closable="false"
      show-icon
      title="只显示归属于你的客户"
      class="scope-tip"
    />
    <el-table
      v-loading="loading"
      :data="items"
      data-testid="customer-table"
      empty-text="暂无客户"
      @selection-change="(rows: Schemas['CustomerOut'][]) => (selected = rows)"
    >
      <el-table-column v-if="canTransfer" type="selection" width="44" />
      <el-table-column prop="display_name" label="客户名称" min-width="180" />
      <el-table-column label="归属坐席" min-width="120">
        <template #default="{ row }">{{ row.owner_display_name ?? '—' }}</template>
      </el-table-column>
      <el-table-column label="来源" width="120">
        <template #default="{ row }">
          {{ CUSTOMER_SOURCE[row.source_channel] ?? row.source_channel }}
        </template>
      </el-table-column>
      <el-table-column label="标签" min-width="160">
        <template #default="{ row }">
          <el-tag v-for="tag in row.tags" :key="tag" size="small" class="tag">{{ tag }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="创建时间" width="200">
        <template #default="{ row }">{{ formatDateTime(row.created_at) }}</template>
      </el-table-column>
      <el-table-column label="" width="100">
        <template #default="{ row }">
          <el-button link type="primary" size="small" @click="showHistory(row)">归属记录</el-button>
        </template>
      </el-table-column>
    </el-table>
    <OwnerTransferDialog
      v-model="transferOpen"
      :customer-ids="selected.map((c) => c.id)"
      @done="load"
    />
    <OwnerHistoryDrawer v-model="historyOpen" :customer="historyOf" />
    <div class="page-footer">
      <el-pagination
        v-model:current-page="page"
        layout="total, prev, pager, next"
        :page-size="PAGE_SIZE"
        :total="total"
        @current-change="load"
      />
    </div>

    <el-dialog v-model="dialogVisible" title="新建客户" width="440px">
      <el-form label-width="84px" @submit.prevent="create">
        <el-form-item label="客户名称" required>
          <el-input v-model="form.displayName" maxlength="128" />
        </el-form-item>
        <el-form-item v-if="canAssign" label="归属坐席">
          <el-select v-model="form.ownerId" clearable placeholder="默认归属自己">
            <el-option
              v-for="s in staffOptions"
              :key="s.id"
              :label="s.display_name"
              :value="s.id"
            />
          </el-select>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="create">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.scope-tip {
  margin-bottom: 12px;
}

.tag {
  margin-right: 4px;
}
</style>
