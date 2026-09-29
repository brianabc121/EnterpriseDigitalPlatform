<script setup lang="ts">
import { errorMessage, formatLimit, formatMoney, type Schemas } from '@edp/api-client'
import { ElMessage } from 'element-plus'
import { onMounted, reactive, ref } from 'vue'

import { api } from '../api'
import {
  FEATURE_KEYS,
  FEATURE_LABELS,
  LIMIT_KEYS,
  LIMIT_LABELS,
  type LimitKey,
  OVERAGE_POLICY,
  toFen,
  toYuan,
} from '../labels'

type Plan = Schemas['PlanOut']

const plans = ref<Plan[]>([])
const loading = ref(false)
const saving = ref(false)
const editing = ref<Plan | null>(null)
const dialogOpen = ref(false)

function emptyForm() {
  return {
    code: '',
    name: '',
    description: '',
    priceYuan: 0,
    limits: Object.fromEntries(LIMIT_KEYS.map((k) => [k, { unlimited: true, value: 0 }])) as Record<
      LimitKey,
      { unlimited: boolean; value: number }
    >,
    features: { ai: true, wecom: true, broadcast: true, extraction: true, zone: false },
    policy: 'degrade' as 'degrade' | 'warn',
    aiReplyPriceYuan: 0,
    trialDays: 0,
    public: true,
    sort: 0,
    archived: false,
  }
}

const form = reactive(emptyForm())

async function load(): Promise<void> {
  loading.value = true
  const { data, error } = await api.GET('/platform/v1/plans')
  loading.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  plans.value = data.items
}

function openCreate(): void {
  editing.value = null
  Object.assign(form, emptyForm())
  dialogOpen.value = true
}

function openEdit(plan: Plan): void {
  editing.value = plan
  Object.assign(form, emptyForm(), {
    code: plan.code,
    name: plan.name,
    description: plan.description ?? '',
    priceYuan: toYuan(plan.price_monthly),
    policy: plan.overage.policy,
    aiReplyPriceYuan: toYuan(plan.overage.ai_reply_price),
    trialDays: plan.trial_days,
    public: plan.public,
    sort: plan.sort,
    archived: plan.status === 'archived',
  })
  for (const key of LIMIT_KEYS) {
    const value = plan.limits[key]
    form.limits[key] = { unlimited: value === null || value === undefined, value: value ?? 0 }
  }
  for (const key of FEATURE_KEYS) {
    form.features[key] = plan.features[key] ?? true
  }
  dialogOpen.value = true
}

function payloadLimits(): Schemas['PlanLimits'] {
  const limits: Record<string, number | null> = {}
  for (const [key, entry] of Object.entries(form.limits)) {
    limits[key] = entry.unlimited ? null : entry.value
  }
  return limits as Schemas['PlanLimits']
}

async function save(): Promise<void> {
  saving.value = true
  const common = {
    name: form.name.trim(),
    description: form.description.trim() || null,
    price_monthly: toFen(form.priceYuan),
    limits: payloadLimits(),
    features: { ...form.features } as Schemas['PlanFeatures'],
    overage: { policy: form.policy, ai_reply_price: toFen(form.aiReplyPriceYuan) },
    trial_days: form.trialDays,
    public: form.public,
    sort: form.sort,
  }
  const result = editing.value
    ? await api.PATCH('/platform/v1/plans/{plan_id}', {
        params: { path: { plan_id: editing.value.id } },
        body: { ...common, status: form.archived ? 'archived' : 'active' },
      })
    : await api.POST('/platform/v1/plans', { body: { ...common, code: form.code.trim() } })
  saving.value = false
  if (!result.data) {
    ElMessage.error(errorMessage(result.error))
    return
  }
  ElMessage.success('已保存')
  dialogOpen.value = false
  await load()
}

onMounted(load)
</script>

<template>
  <div>
    <div class="page-header">
      <h2>套餐</h2>
      <el-button type="primary" data-testid="plan-create" @click="openCreate">新建套餐</el-button>
    </div>
    <el-table v-loading="loading" :data="plans" data-testid="plan-table">
      <el-table-column label="套餐" min-width="160">
        <template #default="{ row }">
          <strong>{{ row.name }}</strong>
          <span class="sub"> {{ row.code }}</span>
          <el-tag disable-transitions v-if="row.status === 'archived'" size="small" type="info">已下架</el-tag>
          <el-tag disable-transitions v-else-if="!row.public" size="small">不公开</el-tag>
          <div class="sub">{{ row.description }}</div>
        </template>
      </el-table-column>
      <el-table-column label="月费" width="120">
        <template #default="{ row }">{{ formatMoney(row.price_monthly) }}</template>
      </el-table-column>
      <el-table-column label="额度" min-width="220">
        <template #default="{ row }">
          <div v-for="(meta, key) in LIMIT_LABELS" :key="key" class="sub">
            {{ meta.label }}：{{ formatLimit(row.limits[key], meta.unit) }}
          </div>
        </template>
      </el-table-column>
      <el-table-column label="功能" min-width="220">
        <template #default="{ row }">
          <el-tag disable-transitions
            v-for="(label, key) in FEATURE_LABELS"
            :key="key"
            size="small"
            :type="row.features[key] ? 'success' : 'info'"
            class="feature"
          >
            {{ label }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="超额" width="170">
        <template #default="{ row }">
          <div class="sub">{{ OVERAGE_POLICY[row.overage.policy] }}</div>
          <div v-if="row.overage.policy === 'warn'" class="sub">
            {{ formatMoney(row.overage.ai_reply_price) }}/条
          </div>
        </template>
      </el-table-column>
      <el-table-column label="试用" width="80">
        <template #default="{ row }">{{ row.trial_days ? `${row.trial_days} 天` : '—' }}</template>
      </el-table-column>
      <el-table-column label="操作" width="80">
        <template #default="{ row }">
          <el-button link type="primary" @click="openEdit(row)">编辑</el-button>
        </template>
      </el-table-column>
    </el-table>

    <el-dialog v-model="dialogOpen" :title="editing ? `编辑套餐 · ${editing.name}` : '新建套餐'" width="620px">
      <el-form label-width="110px">
        <el-form-item label="代码" required>
          <el-input v-model="form.code" :disabled="editing !== null" placeholder="小写字母开头，如 pro" />
        </el-form-item>
        <el-form-item label="名称" required>
          <el-input v-model="form.name" data-testid="plan-name" />
        </el-form-item>
        <el-form-item label="说明">
          <el-input v-model="form.description" />
        </el-form-item>
        <el-form-item label="月费（元）">
          <el-input-number v-model="form.priceYuan" :min="0" :precision="2" :step="100" />
        </el-form-item>
        <el-form-item v-for="(meta, key) in LIMIT_LABELS" :key="key" :label="meta.label">
          <el-checkbox v-model="form.limits[key].unlimited">不限</el-checkbox>
          <el-input-number
            v-if="!form.limits[key].unlimited"
            v-model="form.limits[key].value"
            :min="0"
            :max="100000000"
            class="number"
            :data-testid="`plan-limit-${key}`"
          />
        </el-form-item>
        <el-form-item label="功能">
          <el-checkbox v-for="(label, key) in FEATURE_LABELS" :key="key" v-model="form.features[key]">
            {{ label }}
          </el-checkbox>
        </el-form-item>
        <el-form-item label="超出 AI 额度">
          <el-radio-group v-model="form.policy">
            <el-radio v-for="(label, key) in OVERAGE_POLICY" :key="key" :value="key">{{ label }}</el-radio>
          </el-radio-group>
        </el-form-item>
        <el-form-item v-if="form.policy === 'warn'" label="超额单价（元）">
          <el-input-number v-model="form.aiReplyPriceYuan" :min="0" :precision="2" :step="0.01" />
          <span class="sub unit">每条 AI 回复</span>
        </el-form-item>
        <el-form-item label="试用天数">
          <el-input-number v-model="form.trialDays" :min="0" :max="365" />
          <span class="sub unit">大于 0 时开通即试用</span>
        </el-form-item>
        <el-form-item label="公开">
          <el-switch v-model="form.public" />
          <span class="sub unit">在租户的套餐页展示</span>
        </el-form-item>
        <el-form-item label="排序">
          <el-input-number v-model="form.sort" :min="-1000" :max="1000" />
        </el-form-item>
        <el-form-item v-if="editing" label="下架">
          <el-switch v-model="form.archived" />
          <span class="sub unit">下架后不能用于新订阅，已有订阅不受影响</span>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogOpen = false">取消</el-button>
        <el-button type="primary" :loading="saving" data-testid="plan-save" @click="save">保存</el-button>
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

.sub {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.feature {
  margin: 0 4px 4px 0;
}

.number {
  margin-left: 12px;
}

.unit {
  margin-left: 8px;
}
</style>
