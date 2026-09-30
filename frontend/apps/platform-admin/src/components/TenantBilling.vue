<script setup lang="ts">
import {
  errorMessage,
  formatLimit,
  formatMoney,
  INVOICE_STATUS,
  SUBSCRIPTION_STATUS,
  usagePercent,
  type Schemas,
} from '@edp/api-client'
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed, onMounted, reactive, ref } from 'vue'

import { api, formatDateTime } from '../api'
import {
  FEATURE_KEYS,
  FEATURE_LABELS,
  type FeatureKey,
  LIMIT_KEYS,
  LIMIT_LABELS,
  type LimitKey,
} from '../labels'

const props = defineProps<{ tenantId: string }>()
const emit = defineEmits<{ changed: [] }>()

type LimitMode = 'plan' | 'unlimited' | 'custom'
type FeatureMode = 'plan' | 'on' | 'off'

const billing = ref<Schemas['TenantBilling'] | null>(null)
const plans = ref<Schemas['PlanOut'][]>([])
const loading = ref(false)
const saving = ref(false)
const subscribeOpen = ref(false)
const renewOpen = ref(false)
const overridesOpen = ref(false)
const subscribe = reactive({ planCode: '', status: 'active' as 'trial' | 'active', months: 12, note: '' })
const renew = reactive({ months: 12, note: '' })
const limitForm = reactive(
  Object.fromEntries(LIMIT_KEYS.map((k) => [k, { mode: 'plan', value: 0 }])) as Record<
    LimitKey,
    { mode: LimitMode; value: number }
  >,
)
const featureForm = reactive(
  Object.fromEntries(FEATURE_KEYS.map((k) => [k, 'plan'])) as Record<FeatureKey, FeatureMode>,
)

const current = computed(() => billing.value?.subscription ?? null)
const activePlans = computed(() => plans.value.filter((p) => p.status === 'active'))

async function load(): Promise<void> {
  loading.value = true
  const [detail, planList] = await Promise.all([
    api.GET('/platform/v1/tenants/{tenant_id}/billing', {
      params: { path: { tenant_id: props.tenantId } },
    }),
    api.GET('/platform/v1/plans'),
  ])
  loading.value = false
  if (!detail.data) {
    ElMessage.error(errorMessage(detail.error))
    return
  }
  billing.value = detail.data
  plans.value = planList.data?.items ?? []
}

function openSubscribe(): void {
  Object.assign(subscribe, { planCode: '', status: 'active', months: 12, note: '' })
  subscribeOpen.value = true
}

async function startSubscription(): Promise<void> {
  if (!subscribe.planCode) {
    ElMessage.warning('请选择套餐')
    return
  }
  saving.value = true
  const { data, error } = await api.POST('/platform/v1/tenants/{tenant_id}/subscriptions', {
    params: { path: { tenant_id: props.tenantId } },
    body: {
      plan_code: subscribe.planCode,
      status: subscribe.status,
      months: subscribe.status === 'trial' ? null : subscribe.months,
      note: subscribe.note || null,
    },
  })
  saving.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  ElMessage.success(`已开始「${data.plan_name}」，到期日 ${data.period_end}`)
  subscribeOpen.value = false
  await load()
  emit('changed')
}

async function renewCurrent(): Promise<void> {
  if (!current.value) return
  saving.value = true
  const { data, error } = await api.POST(
    '/platform/v1/tenants/{tenant_id}/subscriptions/{subscription_id}/renew',
    {
      params: { path: { tenant_id: props.tenantId, subscription_id: current.value.id } },
      body: { months: renew.months, note: renew.note || null },
    },
  )
  saving.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  ElMessage.success(`已续费至 ${data.period_end}`)
  renewOpen.value = false
  await load()
  emit('changed')
}

async function cancelCurrent(): Promise<void> {
  if (!current.value) return
  const confirmed = await ElMessageBox.confirm(
    '订阅截止到今天；宽限期过后租户会被停用。',
    '取消订阅',
    { type: 'warning', confirmButtonText: '取消订阅', cancelButtonText: '返回' },
  ).catch(() => false)
  if (!confirmed) return
  const { data, error } = await api.POST(
    '/platform/v1/tenants/{tenant_id}/subscriptions/{subscription_id}/cancel',
    { params: { path: { tenant_id: props.tenantId, subscription_id: current.value.id } } },
  )
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  await load()
  emit('changed')
}

function openOverrides(): void {
  const overrides = billing.value?.overrides ?? { limits: {}, features: {} }
  const limits = overrides.limits as Record<string, number | null>
  const features = overrides.features as Record<string, boolean>
  for (const key of LIMIT_KEYS) {
    if (!(key in limits)) limitForm[key] = { mode: 'plan', value: 0 }
    else if (limits[key] === null) limitForm[key] = { mode: 'unlimited', value: 0 }
    else limitForm[key] = { mode: 'custom', value: limits[key] ?? 0 }
  }
  for (const key of FEATURE_KEYS) {
    featureForm[key] = key in features ? (features[key] ? 'on' : 'off') : 'plan'
  }
  overridesOpen.value = true
}

async function saveOverrides(): Promise<void> {
  const limits: Record<string, number | null> = {}
  for (const [key, entry] of Object.entries(limitForm)) {
    if (entry.mode === 'unlimited') limits[key] = null
    else if (entry.mode === 'custom') limits[key] = entry.value
  }
  const features: Record<string, boolean> = {}
  for (const [key, mode] of Object.entries(featureForm)) {
    if (mode !== 'plan') features[key] = mode === 'on'
  }
  saving.value = true
  const { data, error } = await api.PUT('/platform/v1/tenants/{tenant_id}/overrides', {
    params: { path: { tenant_id: props.tenantId } },
    body: { limits, features } as Schemas['TenantOverrides'],
  })
  saving.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  ElMessage.success('已保存')
  overridesOpen.value = false
  await load()
}

async function setInvoice(invoice: Schemas['InvoiceOut'], status: 'paid' | 'void'): Promise<void> {
  const { data, error } = await api.PATCH('/platform/v1/invoices/{invoice_id}', {
    params: { path: { invoice_id: invoice.id } },
    body: { status },
  })
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  await load()
}

onMounted(load)
defineExpose({ load })
</script>

<template>
  <div v-loading="loading" data-testid="tenant-billing">
    <template v-if="billing">
      <el-descriptions :column="3" border size="small">
        <el-descriptions-item label="当前套餐">
          <span data-testid="current-plan">{{ billing.plan?.name ?? '不按套餐计费' }}</span>
        </el-descriptions-item>
        <el-descriptions-item label="订阅状态">
          <el-tag disable-transitions v-if="current" :type="current.status === 'active' ? 'success' : 'warning'">
            {{ SUBSCRIPTION_STATUS[current.status] ?? current.status }}
          </el-tag>
          <span v-else>—</span>
        </el-descriptions-item>
        <el-descriptions-item label="有效期">
          <span v-if="current">{{ current.period_start }} 至 {{ current.period_end }}</span>
          <span v-else>—</span>
        </el-descriptions-item>
      </el-descriptions>
      <el-alert
        v-if="billing.notice"
        :title="billing.notice"
        type="warning"
        :closable="false"
        show-icon
        class="block"
      />
      <div class="actions">
        <el-button type="primary" data-testid="subscribe-button" @click="openSubscribe">
          开始新订阅
        </el-button>
        <el-button v-if="current" data-testid="renew-button" @click="renewOpen = true">续费</el-button>
        <el-button
          v-if="current && (current.status === 'trial' || current.status === 'active')"
          @click="cancelCurrent"
        >
          取消订阅
        </el-button>
        <el-button data-testid="overrides-button" @click="openOverrides">单独调整额度与功能</el-button>
      </div>

      <h4>额度与用量</h4>
      <el-table :data="billing.limits" size="small" data-testid="tenant-limits">
        <el-table-column prop="label" label="额度" width="140" />
        <el-table-column label="已用 / 上限" width="200">
          <template #default="{ row }">
            {{ row.used.toLocaleString('zh-CN') }} / {{ formatLimit(row.limit, row.unit) }}
            <el-tag disable-transitions v-if="row.overridden" size="small" type="info">单独设置</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="使用比例">
          <template #default="{ row }">
            <el-progress
              v-if="row.limit !== null"
              :percentage="usagePercent(row.used, row.limit)"
              :status="row.used >= row.limit ? 'exception' : undefined"
            />
            <span v-else class="sub">不限</span>
          </template>
        </el-table-column>
      </el-table>

      <h4>功能</h4>
      <div class="features">
        <el-tag disable-transitions
          v-for="f in billing.features"
          :key="f.key"
          :type="f.enabled ? 'success' : 'info'"
          effect="plain"
        >
          {{ f.enabled ? '✓' : '✗' }} {{ f.label }}{{ f.overridden ? '（单独设置）' : '' }}
        </el-tag>
      </div>

      <h4>订阅记录</h4>
      <el-table :data="billing.subscriptions" size="small" data-testid="subscription-history">
        <el-table-column prop="plan_name" label="套餐" width="120" />
        <el-table-column label="状态" width="90">
          <template #default="{ row }">{{ SUBSCRIPTION_STATUS[row.status] ?? row.status }}</template>
        </el-table-column>
        <el-table-column label="有效期" width="220">
          <template #default="{ row }">{{ row.period_start }} 至 {{ row.period_end }}</template>
        </el-table-column>
        <el-table-column prop="note" label="备注" />
        <el-table-column label="创建时间" width="170">
          <template #default="{ row }">{{ formatDateTime(row.created_at) }}</template>
        </el-table-column>
      </el-table>

      <h4>账单</h4>
      <el-table :data="billing.invoices" size="small" empty-text="暂无账单">
        <el-table-column prop="number" label="编号" width="200" />
        <el-table-column label="账期" width="220">
          <template #default="{ row }">{{ row.period_start }} 至 {{ row.period_end }}</template>
        </el-table-column>
        <el-table-column label="金额" width="120">
          <template #default="{ row }">{{ formatMoney(row.amount) }}</template>
        </el-table-column>
        <el-table-column label="状态" width="90">
          <template #default="{ row }">{{ INVOICE_STATUS[row.status] ?? row.status }}</template>
        </el-table-column>
        <el-table-column label="操作">
          <template #default="{ row }">
            <template v-if="row.status === 'issued'">
              <el-button link type="primary" @click="setInvoice(row, 'paid')">标记已付款</el-button>
              <el-button link type="danger" @click="setInvoice(row, 'void')">作废</el-button>
            </template>
          </template>
        </el-table-column>
      </el-table>
    </template>

    <el-dialog v-model="subscribeOpen" title="开始新订阅" width="460px">
      <el-form label-width="90px">
        <el-form-item label="套餐" required>
          <el-select v-model="subscribe.planCode" data-testid="subscribe-plan">
            <el-option
              v-for="p in activePlans"
              :key="p.code"
              :label="`${p.name}（${formatMoney(p.price_monthly)}/月）`"
              :value="p.code"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="类型">
          <el-radio-group v-model="subscribe.status">
            <el-radio value="active">正式</el-radio>
            <el-radio value="trial">试用</el-radio>
          </el-radio-group>
        </el-form-item>
        <el-form-item v-if="subscribe.status === 'active'" label="月数">
          <el-input-number v-model="subscribe.months" :min="1" :max="60" />
        </el-form-item>
        <el-form-item label="备注">
          <el-input v-model="subscribe.note" placeholder="合同编号等" />
        </el-form-item>
        <p class="sub">当前订阅会被取消（截止到新订阅开始的前一天）；因到期停用的租户会自动恢复。</p>
      </el-form>
      <template #footer>
        <el-button @click="subscribeOpen = false">取消</el-button>
        <el-button
          type="primary"
          :loading="saving"
          data-testid="subscribe-save"
          @click="startSubscription"
        >
          确定
        </el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="renewOpen" title="续费" width="400px">
      <el-form label-width="90px">
        <el-form-item label="续费月数">
          <el-input-number v-model="renew.months" :min="1" :max="60" />
        </el-form-item>
        <el-form-item label="备注">
          <el-input v-model="renew.note" />
        </el-form-item>
        <p class="sub">从原到期日（{{ current?.period_end }}）起延长。</p>
      </el-form>
      <template #footer>
        <el-button @click="renewOpen = false">取消</el-button>
        <el-button type="primary" :loading="saving" data-testid="renew-save" @click="renewCurrent">
          续费
        </el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="overridesOpen" title="单独调整额度与功能" width="560px">
      <el-form label-width="110px">
        <el-form-item v-for="(meta, key) in LIMIT_LABELS" :key="key" :label="meta.label">
          <el-radio-group v-model="limitForm[key].mode" size="small">
            <el-radio-button value="plan">按套餐</el-radio-button>
            <el-radio-button value="unlimited">不限</el-radio-button>
            <el-radio-button value="custom">自定义</el-radio-button>
          </el-radio-group>
          <el-input-number
            v-if="limitForm[key].mode === 'custom'"
            v-model="limitForm[key].value"
            :min="0"
            :max="100000000"
            size="small"
            class="number"
            :data-testid="`override-${key}`"
          />
        </el-form-item>
        <el-divider />
        <el-form-item v-for="(label, key) in FEATURE_LABELS" :key="key" :label="label">
          <el-radio-group v-model="featureForm[key]" size="small">
            <el-radio-button value="plan">按套餐</el-radio-button>
            <el-radio-button value="on">开</el-radio-button>
            <el-radio-button value="off">关</el-radio-button>
          </el-radio-group>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="overridesOpen = false">取消</el-button>
        <el-button
          type="primary"
          :loading="saving"
          data-testid="overrides-save"
          @click="saveOverrides"
        >
          保存
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.block {
  margin-top: 12px;
}

.actions {
  margin: 12px 0;
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}

.features {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}

.number {
  margin-left: 12px;
}

.sub {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

h4 {
  margin: 20px 0 8px;
}
</style>
