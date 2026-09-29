<script setup lang="ts">
import { errorMessage, type Schemas } from '@edp/api-client'
import { ElMessage } from 'element-plus'
import { computed, onMounted, reactive, ref } from 'vue'

import { api, formatDateTime } from '../api'
import ExportDialog from '../components/customers/ExportDialog.vue'
import MergeDialog from '../components/customers/MergeDialog.vue'
import OwnerHistoryDrawer from '../components/customers/OwnerHistoryDrawer.vue'
import OwnerTransferDialog from '../components/customers/OwnerTransferDialog.vue'
import PrivacyDialog from '../components/customers/PrivacyDialog.vue'
import PrivacyRequestsDrawer from '../components/customers/PrivacyRequestsDrawer.vue'
import CustomerPanel from '../components/workbench/CustomerPanel.vue'
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
const canExport = computed(() => auth.can('customer:export'))
const canManage = computed(() => auth.can('customer:manage'))

const q = ref('')
const exportOpen = ref(false)
const mergeOpen = ref(false)
const privacyOpen = ref(false)
const requestsOpen = ref(false)
const profileOpen = ref(false)
const current = ref<Schemas['CustomerOut'] | null>(null)

function openWith(customer: Schemas['CustomerOut'], dialog: 'merge' | 'privacy' | 'profile'): void {
  current.value = customer
  if (dialog === 'merge') mergeOpen.value = true
  else if (dialog === 'privacy') privacyOpen.value = true
  else profileOpen.value = true
}

function onAction(command: string, customer: Schemas['CustomerOut']): void {
  if (command === 'history') showHistory(customer)
  else openWith(customer, command as 'merge' | 'privacy')
}

async function search(): Promise<void> {
  page.value = 1
  await load()
}

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
const form = reactive<{
  displayName: string
  ownerId: string | null
  phone: string
  email: string
  company: string
}>({
  displayName: '',
  ownerId: null,
  phone: '',
  email: '',
  company: '',
})

async function load(): Promise<void> {
  loading.value = true
  const { data, error } = await api.GET('/api/v1/customers', {
    params: {
      query: {
        limit: PAGE_SIZE,
        offset: (page.value - 1) * PAGE_SIZE,
        q: q.value.trim() || undefined,
      },
    },
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
  Object.assign(form, { displayName: '', ownerId: null, phone: '', email: '', company: '' })
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
    body: {
      display_name: form.displayName.trim(),
      owner_id: form.ownerId,
      phone: form.phone.trim() || null,
      email: form.email.trim() || null,
      company: form.company.trim() || null,
    },
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
      <div class="toolbar">
        <el-input
          v-model="q"
          placeholder="名称、公司，或完整手机号、邮箱"
          clearable
          class="search"
          data-testid="customer-search"
          @keyup.enter="search"
          @clear="search"
        />
        <el-button v-if="canManage" @click="requestsOpen = true">个人信息请求</el-button>
        <el-button v-if="canExport" data-testid="export-customers" @click="exportOpen = true">
          导出
        </el-button>
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
      <el-table-column label="客户名称" min-width="160">
        <template #default="{ row }">
          <el-button link type="primary" @click="openWith(row, 'profile')">
            {{ row.display_name }}
          </el-button>
        </template>
      </el-table-column>
      <el-table-column label="联系方式" min-width="170">
        <template #default="{ row }">
          <div v-if="row.phone">{{ row.phone }}</div>
          <div v-if="row.email" class="muted">{{ row.email }}</div>
          <span v-if="!row.phone && !row.email" class="muted">—</span>
        </template>
      </el-table-column>
      <el-table-column label="公司" min-width="120">
        <template #default="{ row }">{{ row.company ?? '—' }}</template>
      </el-table-column>
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
      <el-table-column label="" width="90">
        <template #default="{ row }">
          <el-dropdown trigger="click" @command="(c: string) => onAction(c, row)">
            <el-button link type="primary" size="small" :data-testid="`customer-more-${row.id}`">
              更多
            </el-button>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item command="history">归属记录</el-dropdown-item>
                <el-dropdown-item v-if="canManage" command="merge">合并重复客户</el-dropdown-item>
                <el-dropdown-item v-if="canManage" command="privacy">个人信息请求</el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
        </template>
      </el-table-column>
    </el-table>
    <OwnerTransferDialog
      v-model="transferOpen"
      :customer-ids="selected.map((c) => c.id)"
      @done="load"
    />
    <OwnerHistoryDrawer v-model="historyOpen" :customer="historyOf" />
    <ExportDialog v-model="exportOpen" :q="q" />
    <MergeDialog v-model="mergeOpen" :target="current" @done="load" />
    <PrivacyDialog v-model="privacyOpen" :customer="current" @erased="load" />
    <PrivacyRequestsDrawer v-model="requestsOpen" />
    <el-drawer v-model="profileOpen" :title="current?.display_name" size="380px" @closed="load">
      <CustomerPanel v-if="current && profileOpen" :key="current.id" :customer-id="current.id" />
    </el-drawer>
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
        <el-form-item label="手机号">
          <el-input v-model="form.phone" maxlength="32" />
        </el-form-item>
        <el-form-item label="邮箱">
          <el-input v-model="form.email" maxlength="254" />
        </el-form-item>
        <el-form-item label="公司">
          <el-input v-model="form.company" maxlength="128" />
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

.toolbar {
  display: flex;
  gap: 8px;
  align-items: center;
}

.search {
  width: 260px;
}

.muted {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}
</style>
