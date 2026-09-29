<script setup lang="ts">
import { errorMessage, formatUsage, type Schemas } from '@edp/api-client'
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed, onMounted, reactive, ref } from 'vue'

import { api, formatDateTime } from '../api'
import TenantUsageDrawer from '../components/TenantUsageDrawer.vue'

type Tenant = Schemas['TenantOut']

const tenants = ref<Tenant[]>([])
const usage = ref<Schemas['TenantUsageList'] | null>(null)
const usageOf = ref<Tenant | null>(null)
const loading = ref(false)

/** 列表里展示的用量（最近 30 天）。 */
const USAGE_COLUMNS = ['seats', 'messages_in', 'human_sessions', 'file_bytes']
const usageColumns = computed(() =>
  (usage.value?.metrics ?? []).filter((m) => USAGE_COLUMNS.includes(m.key)),
)
const totals = computed(
  () => new Map((usage.value?.items ?? []).map((item) => [item.tenant_id, item.totals])),
)
const dialogVisible = ref(false)
const saving = ref(false)
/** 正在设置 AI 额度的租户；unlimited 为不限。 */
const quota = reactive({ tenant: null as Tenant | null, value: 10000, unlimited: true })
const form = reactive({
  code: '',
  name: '',
  adminUsername: 'admin',
  adminDisplayName: '管理员',
  adminPassword: '',
})

async function load(): Promise<void> {
  loading.value = true
  const [list, summary] = await Promise.all([
    api.GET('/platform/v1/tenants'),
    api.GET('/platform/v1/usage'),
  ])
  loading.value = false
  if (!list.data) {
    ElMessage.error(errorMessage(list.error))
    return
  }
  tenants.value = list.data.items
  usage.value = summary.data ?? null
}

function openCreate(): void {
  Object.assign(form, {
    code: '',
    name: '',
    adminUsername: 'admin',
    adminDisplayName: '管理员',
    adminPassword: '',
  })
  dialogVisible.value = true
}

async function create(): Promise<void> {
  saving.value = true
  const { data, error } = await api.POST('/platform/v1/tenants', {
    body: {
      code: form.code.trim(),
      name: form.name.trim(),
      admin: {
        username: form.adminUsername.trim(),
        display_name: form.adminDisplayName.trim(),
        password: form.adminPassword,
      },
    },
  })
  saving.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  ElMessage.success(`已开通：企业代码 ${data.code}，管理员 ${form.adminUsername}`)
  dialogVisible.value = false
  await load()
}

async function toggleStatus(tenant: Tenant): Promise<void> {
  const suspending = tenant.status === 'active'
  if (suspending) {
    const confirmed = await ElMessageBox.confirm(
      `停用后「${tenant.name}」的所有员工将无法登录，已登录的会话也会失效。`,
      '停用租户',
      { type: 'warning', confirmButtonText: '停用', cancelButtonText: '取消' },
    ).catch(() => false)
    if (!confirmed) return
  }
  const { data, error } = await api.PATCH('/platform/v1/tenants/{tenant_id}', {
    params: { path: { tenant_id: tenant.id } },
    body: { status: suspending ? 'suspended' : 'active' },
  })
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  await load()
}

function editQuota(tenant: Tenant): void {
  quota.tenant = tenant
  quota.unlimited = tenant.ai_monthly_quota === null || tenant.ai_monthly_quota === undefined
  quota.value = tenant.ai_monthly_quota ?? 10000
}

async function saveQuota(): Promise<void> {
  if (!quota.tenant) return
  saving.value = true
  const { data, error } = await api.PATCH('/platform/v1/tenants/{tenant_id}', {
    params: { path: { tenant_id: quota.tenant.id } },
    body: { ai_monthly_quota: quota.unlimited ? null : quota.value },
  })
  saving.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  ElMessage.success('已保存')
  quota.tenant = null
  await load()
}

function quotaText(tenant: Tenant): string {
  const q = tenant.ai_monthly_quota
  return q === null || q === undefined ? '不限' : `${q.toLocaleString('zh-CN')} 条/月`
}

onMounted(load)
</script>

<template>
  <div>
    <div class="page-header">
      <h2>租户</h2>
      <el-button type="primary" @click="openCreate">开通租户</el-button>
    </div>
    <el-table v-loading="loading" :data="tenants" data-testid="tenant-table" empty-text="暂无租户">
      <el-table-column prop="code" label="企业代码" width="130" />
      <el-table-column prop="name" label="企业名称" min-width="200" />
      <el-table-column label="状态" width="100">
        <template #default="{ row }">
          <el-tag :type="row.status === 'active' ? 'success' : 'danger'">
            {{ row.status === 'active' ? '正常' : '已停用' }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="开通时间" width="170">
        <template #default="{ row }">{{ formatDateTime(row.created_at) }}</template>
      </el-table-column>
      <el-table-column
        v-for="m in usageColumns"
        :key="m.key"
        :label="m.kind === 'snapshot' ? m.label : `近 30 天${m.label}`"
        min-width="110"
      >
        <template #default="{ row }">{{ formatUsage(m, totals.get(row.id)?.[m.key]) }}</template>
      </el-table-column>
      <el-table-column label="AI 回复额度" width="120">
        <template #default="{ row }">{{ quotaText(row) }}</template>
      </el-table-column>
      <el-table-column label="操作" width="170" fixed="right">
        <template #default="{ row }">
          <el-button link type="primary" data-testid="tenant-usage-button" @click="usageOf = row">
            用量
          </el-button>
          <el-button link type="primary" data-testid="tenant-quota-button" @click="editQuota(row)">
            AI 额度
          </el-button>
          <el-button
            link
            :type="row.status === 'active' ? 'danger' : 'primary'"
            @click="toggleStatus(row)"
          >
            {{ row.status === 'active' ? '停用' : '启用' }}
          </el-button>
        </template>
      </el-table-column>
    </el-table>

    <TenantUsageDrawer :tenant="usageOf" @close="usageOf = null" />

    <el-dialog
      :model-value="quota.tenant !== null"
      :title="`AI 回复额度 · ${quota.tenant?.name ?? ''}`"
      width="420px"
      @update:model-value="(v: boolean) => !v && (quota.tenant = null)"
    >
      <el-form label-width="96px" @submit.prevent="saveQuota">
        <el-form-item label="每月上限">
          <el-checkbox v-model="quota.unlimited" data-testid="quota-unlimited">不限</el-checkbox>
        </el-form-item>
        <el-form-item v-if="!quota.unlimited" label="回复条数">
          <el-input-number
            v-model="quota.value"
            :min="0"
            :max="10000000"
            :step="1000"
            data-testid="quota-value"
          />
        </el-form-item>
        <p class="hint">按自然月计算 AI 回复条数；用完后新会话直接转人工，下月恢复。</p>
      </el-form>
      <template #footer>
        <el-button @click="quota.tenant = null">取消</el-button>
        <el-button type="primary" :loading="saving" data-testid="quota-save" @click="saveQuota">
          保存
        </el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="dialogVisible" title="开通租户" width="480px">
      <el-form label-width="96px" @submit.prevent="create">
        <el-form-item label="企业代码" required>
          <el-input v-model="form.code" placeholder="小写字母开头，3-32 位，可含数字和 -" />
        </el-form-item>
        <el-form-item label="企业名称" required>
          <el-input v-model="form.name" />
        </el-form-item>
        <el-divider content-position="left">首个管理员</el-divider>
        <el-form-item label="用户名" required>
          <el-input v-model="form.adminUsername" />
        </el-form-item>
        <el-form-item label="姓名" required>
          <el-input v-model="form.adminDisplayName" />
        </el-form-item>
        <el-form-item label="初始密码" required>
          <el-input
            v-model="form.adminPassword"
            type="password"
            show-password
            placeholder="至少 8 位"
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="create">开通</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.page-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 16px;
}

.page-header h2 {
  margin: 0;
  font-size: 18px;
}

.hint {
  margin: 0;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
</style>
