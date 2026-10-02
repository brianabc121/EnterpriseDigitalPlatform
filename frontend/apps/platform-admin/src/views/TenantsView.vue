<script setup lang="ts">
import { errorMessage, formatUsage, SUBSCRIPTION_STATUS, type Schemas } from '@edp/api-client'
import { ElMessage, ElMessageBox } from 'element-plus'
import type { FormInstance, FormRules } from 'element-plus'
import { computed, nextTick, onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'

import { api, formatDateTime } from '../api'
import TenantUsageDrawer from '../components/TenantUsageDrawer.vue'
import { TENANT_STATUS } from '../labels'
import { tenantFieldError, type TenantField } from '../tenant-validation'

type Tenant = Schemas['TenantOut']

const router = useRouter()
const tenants = ref<Tenant[]>([])
const plans = ref<Schemas['PlanOut'][]>([])
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
  planCode: '',
  months: 12,
})
const activePlans = computed(() => plans.value.filter((p) => p.status === 'active'))
const chosenPlan = computed(() => plans.value.find((p) => p.code === form.planCode) ?? null)
const createForm = ref<FormInstance>()
const codeServerError = ref('')
const fields: TenantField[] = ['code', 'name', 'adminUsername', 'adminDisplayName', 'adminPassword', 'months']
const createRules: FormRules = Object.fromEntries(fields.map((field) => [field, [{
  validator: (_rule: unknown, value: unknown, callback: (error?: Error) => void) => {
    const message = tenantFieldError(field, value)
    callback(message ? new Error(message) : undefined)
  },
  trigger: ['blur', 'change'],
}]]))

async function validateInput(field: TenantField): Promise<void> {
  if (field === 'code') codeServerError.value = ''
  await nextTick()
  await createForm.value?.validateField(field).catch(() => false)
}

async function load(): Promise<void> {
  loading.value = true
  const [list, summary, planList] = await Promise.all([
    api.GET('/platform/v1/tenants'),
    api.GET('/platform/v1/usage'),
    api.GET('/platform/v1/plans'),
  ])
  loading.value = false
  if (!list.data) {
    ElMessage.error(errorMessage(list.error))
    return
  }
  tenants.value = list.data.items
  usage.value = summary.data ?? null
  plans.value = planList.data?.items ?? []
}

function openCreate(): void {
  Object.assign(form, {
    code: '',
    name: '',
    adminUsername: 'admin',
    adminDisplayName: '管理员',
    adminPassword: '',
    planCode: '',
    months: 12,
  })
  dialogVisible.value = true
  codeServerError.value = ''
  void nextTick(() => createForm.value?.clearValidate())
}

async function create(): Promise<void> {
  if (saving.value) return
  codeServerError.value = ''
  const valid = await createForm.value?.validate().catch(() => false)
  if (!valid) {
    ElMessage.warning('请按字段下方的提示修正后再开通')
    await nextTick()
    document.querySelector<HTMLInputElement>('.tenant-create-form .is-error input')?.focus()
    return
  }
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
      plan_code: form.planCode || null,
      months: chosenPlan.value?.trial_days === 0 ? form.months : 12,
    },
  })
  saving.value = false
  if (!data) {
    const message = errorMessage(error)
    if (message === '企业代码已被使用') codeServerError.value = message
    ElMessage.error(message)
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

function statusText(tenant: Tenant): string {
  if (tenant.status === 'active' && tenant.closing_requested_at) return '注销中'
  return TENANT_STATUS[tenant.status] ?? tenant.status
}

function statusType(tenant: Tenant): 'success' | 'warning' | 'danger' | 'info' {
  if (tenant.status === 'closed') return 'info'
  if (tenant.status !== 'active') return 'danger'
  return tenant.closing_requested_at ? 'warning' : 'success'
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
          <el-tag disable-transitions :type="statusType(row)">{{ statusText(row) }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="套餐" min-width="150">
        <template #default="{ row }">
          <template v-if="row.plan_name">
            {{ row.plan_name }}
            <el-tag disable-transitions size="small" :type="row.subscription_status === 'active' ? 'success' : 'warning'">
              {{ SUBSCRIPTION_STATUS[row.subscription_status ?? ''] ?? row.subscription_status }}
            </el-tag>
            <div class="sub">至 {{ row.period_end }}</div>
          </template>
          <span v-else class="sub">不按套餐计费</span>
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
      <el-table-column label="操作" width="220" fixed="right">
        <template #default="{ row }">
          <el-button
            link
            type="primary"
            data-testid="tenant-detail-button"
            @click="router.push({ name: 'tenant', params: { id: row.id } })"
          >
            详情
          </el-button>
          <el-button link type="primary" data-testid="tenant-usage-button" @click="usageOf = row">
            用量
          </el-button>
          <el-button link type="primary" data-testid="tenant-quota-button" @click="editQuota(row)">
            AI 额度
          </el-button>
          <el-button
            v-if="row.status !== 'closed'"
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
      <el-form ref="createForm" class="tenant-create-form" :model="form" :rules="createRules" status-icon label-width="96px" @submit.prevent="create">
        <el-form-item label="企业代码" prop="code" :error="codeServerError" required>
          <el-input v-model="form.code" placeholder="小写字母开头，3-32 位，可含数字和 -" @input="validateInput('code')" />
        </el-form-item>
        <el-form-item label="企业名称" prop="name" required>
          <el-input v-model="form.name" placeholder="1～128 个字符" @input="validateInput('name')" />
        </el-form-item>
        <el-form-item label="套餐">
          <el-select v-model="form.planCode" placeholder="不按套餐计费" clearable data-testid="create-plan">
            <el-option v-for="p in activePlans" :key="p.code" :label="p.name" :value="p.code" />
          </el-select>
        </el-form-item>
        <el-form-item v-if="chosenPlan && chosenPlan.trial_days === 0" label="订阅月数" prop="months">
          <el-input-number v-model="form.months" :min="1" :max="60" />
        </el-form-item>
        <p v-if="chosenPlan && chosenPlan.trial_days > 0" class="hint">
          开通后先试用 {{ chosenPlan.trial_days }} 天。
        </p>
        <el-divider content-position="left">首个管理员</el-divider>
        <el-form-item label="用户名" prop="adminUsername" required>
          <el-input v-model="form.adminUsername" placeholder="3～64 位，字母、数字、_、. 或 -" @input="validateInput('adminUsername')" />
        </el-form-item>
        <el-form-item label="姓名" prop="adminDisplayName" required>
          <el-input v-model="form.adminDisplayName" placeholder="1～64 个字符" @input="validateInput('adminDisplayName')" />
        </el-form-item>
        <el-form-item label="初始密码" prop="adminPassword" required>
          <el-input
            v-model="form.adminPassword"
            type="password"
            show-password
            placeholder="8～128 位"
            @input="validateInput('adminPassword')"
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
.tenant-create-form :deep(.el-form-item__error) {
  position: static;
  line-height: 1.5;
}

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

.sub {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.hint {
  margin: 0;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
</style>
