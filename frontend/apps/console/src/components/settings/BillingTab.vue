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
import { ElMessage } from 'element-plus'
import { onMounted, ref } from 'vue'

import { api, formatDateTime } from '../../api'

const overview = ref<Schemas['BillingOverview'] | null>(null)
const invoices = ref<Schemas['InvoiceOut'][]>([])
const plans = ref<Schemas['PlanOut'][]>([])
const loading = ref(false)

async function load(): Promise<void> {
  loading.value = true
  const [billing, bills, options] = await Promise.all([
    api.GET('/api/v1/billing'),
    api.GET('/api/v1/billing/invoices'),
    api.GET('/api/v1/billing/plans'),
  ])
  loading.value = false
  if (!billing.data) {
    ElMessage.error(errorMessage(billing.error))
    return
  }
  overview.value = billing.data
  invoices.value = bills.data?.items ?? []
  plans.value = options.data?.items ?? []
}

onMounted(load)
</script>

<template>
  <div v-loading="loading" data-testid="billing-tab">
    <template v-if="overview">
      <el-card shadow="never" class="plan">
        <div class="plan-head">
          <div>
            <div class="plan-name" data-testid="billing-plan">
              {{ overview.plan?.name ?? '不按套餐计费' }}
            </div>
            <div v-if="overview.subscription" class="sub">
              {{ SUBSCRIPTION_STATUS[overview.subscription.status] ?? overview.subscription.status }}
              · {{ overview.subscription.period_start }} 至 {{ overview.subscription.period_end }}
              <span v-if="overview.days_left !== null">（剩余 {{ overview.days_left }} 天）</span>
            </div>
            <div v-else class="sub">没有额度限制；升级或续费请联系平台</div>
          </div>
          <div v-if="overview.plan" class="price">
            {{ formatMoney(overview.plan.price_monthly) }}<span class="sub">/月</span>
          </div>
        </div>
        <el-alert
          v-if="overview.notice"
          :title="overview.notice"
          type="warning"
          :closable="false"
          show-icon
          class="notice"
          data-testid="billing-notice"
        />
      </el-card>

      <h4>额度与用量</h4>
      <div class="limits" data-testid="billing-limits">
        <el-card v-for="limit in overview.limits" :key="limit.key" shadow="never" class="limit">
          <div class="limit-label">{{ limit.label }}</div>
          <div class="limit-value">
            {{ limit.used.toLocaleString('zh-CN') }}
            <span class="sub">/ {{ formatLimit(limit.limit, limit.unit) }}</span>
          </div>
          <el-progress
            v-if="limit.limit !== null"
            :percentage="usagePercent(limit.used, limit.limit)"
            :status="limit.used >= limit.limit ? 'exception' : undefined"
            :show-text="false"
          />
        </el-card>
      </div>
      <p v-if="overview.overage_policy === 'warn'" class="sub">
        超出每月 AI 回复额度后继续回复，按 {{ formatMoney(overview.ai_reply_price) }}/条计入账单。
      </p>
      <p v-else-if="overview.metered" class="sub">每月 AI 回复额度用完后，AI 接待暂停，会话直接转人工。</p>

      <h4>功能</h4>
      <div class="features">
        <el-tag disable-transitions
          v-for="f in overview.features"
          :key="f.key"
          :type="f.enabled ? 'success' : 'info'"
          effect="plain"
        >
          {{ f.enabled ? '✓' : '✗' }} {{ f.label }}
        </el-tag>
      </div>

      <h4>账单</h4>
      <el-table :data="invoices" size="small" empty-text="暂无账单" data-testid="billing-invoices">
        <el-table-column prop="number" label="编号" width="220" />
        <el-table-column label="账期" width="220">
          <template #default="{ row }">{{ row.period_start }} 至 {{ row.period_end }}</template>
        </el-table-column>
        <el-table-column label="明细">
          <template #default="{ row }">
            <div v-for="(item, i) in row.items" :key="i" class="sub">
              {{ item.description }}：{{ formatMoney(item.amount) }}
            </div>
          </template>
        </el-table-column>
        <el-table-column label="金额" width="120">
          <template #default="{ row }">{{ formatMoney(row.amount) }}</template>
        </el-table-column>
        <el-table-column label="状态" width="110">
          <template #default="{ row }">
            {{ INVOICE_STATUS[row.status] ?? row.status }}
            <div v-if="row.paid_at" class="sub">{{ formatDateTime(row.paid_at) }}</div>
          </template>
        </el-table-column>
      </el-table>

      <h4>可选套餐</h4>
      <p class="sub">升级、续费请联系平台运营（一期不支持在线支付）。</p>
      <div class="plans">
        <el-card
          v-for="p in plans"
          :key="p.id"
          shadow="never"
          :class="['option', { current: p.code === overview.plan?.code }]"
        >
          <div class="plan-name">{{ p.name }}</div>
          <div class="price">{{ formatMoney(p.price_monthly) }}<span class="sub">/月</span></div>
          <div class="sub">{{ p.description }}</div>
          <div class="sub">坐席 {{ formatLimit(p.limits.seats, '个') }}</div>
          <div class="sub">每月 AI 回复 {{ formatLimit(p.limits.ai_replies_monthly, '条') }}</div>
          <div class="sub">知识条目 {{ formatLimit(p.limits.kb_items, '条') }}</div>
          <div class="sub">企业资料存储 {{ formatLimit(p.limits.material_gb, 'GB') }}</div>
          <el-tag disable-transitions v-if="p.code === overview.plan?.code" size="small" type="success">当前套餐</el-tag>
        </el-card>
      </div>
    </template>
  </div>
</template>

<style scoped>
.plan-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.plan-name {
  font-size: 16px;
  font-weight: 600;
}

.price {
  font-size: 20px;
  font-weight: 600;
  color: var(--el-color-primary);
}

.notice {
  margin-top: 12px;
}

.limits {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
  gap: 12px;
}

.limit-label {
  font-size: 13px;
  color: var(--el-text-color-secondary);
}

.limit-value {
  font-size: 20px;
  font-weight: 600;
  margin: 4px 0 8px;
}

.features {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}

.plans {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
  gap: 12px;
}

.option.current {
  border-color: var(--el-color-primary);
}

.sub {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

h4 {
  margin: 20px 0 8px;
}
</style>
