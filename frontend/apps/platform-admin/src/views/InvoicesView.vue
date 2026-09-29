<script setup lang="ts">
import { errorMessage, formatMoney, INVOICE_STATUS, type Schemas } from '@edp/api-client'
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed, onMounted, reactive, ref } from 'vue'

import { api, formatDateTime } from '../api'
import { monthOf } from '../labels'

const invoices = ref<Schemas['InvoiceOut'][]>([])
const loading = ref(false)
const filters = reactive({ status: '' as '' | 'issued' | 'paid' | 'void', month: '' })
const lastMonth = monthOf(new Date(new Date().getFullYear(), new Date().getMonth() - 1, 1))
const generating = ref(false)
const total = computed(() => invoices.value.reduce((sum, i) => sum + (i.status === 'void' ? 0 : i.amount), 0))

async function load(): Promise<void> {
  loading.value = true
  const { data, error } = await api.GET('/platform/v1/invoices', {
    params: {
      query: {
        status: filters.status || undefined,
        month: filters.month ? `${filters.month}-01` : undefined,
      },
    },
  })
  loading.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  invoices.value = data.items
}

async function generate(): Promise<void> {
  const result = await ElMessageBox.prompt('生成或重算这个月的账单（已付款、已作废的不变）', '生成账单', {
    inputValue: lastMonth,
    inputPattern: /^\d{4}-(0[1-9]|1[0-2])$/,
    inputErrorMessage: '格式为 YYYY-MM',
    confirmButtonText: '生成',
    cancelButtonText: '取消',
  }).catch(() => null)
  if (!result) return
  generating.value = true
  const { data, error } = await api.POST('/platform/v1/invoices/generate', {
    body: { month: result.value },
  })
  generating.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  ElMessage.success(`新生成 ${data.created} 张，重算 ${data.updated} 张，未变 ${data.unchanged} 张`)
  await load()
}

async function setStatus(invoice: Schemas['InvoiceOut'], status: 'paid' | 'void'): Promise<void> {
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
</script>

<template>
  <div>
    <div class="page-header">
      <h2>账单</h2>
      <div class="filters">
        <el-select v-model="filters.status" placeholder="全部状态" clearable @change="load">
          <el-option v-for="(label, key) in INVOICE_STATUS" :key="key" :label="label" :value="key" />
        </el-select>
        <el-date-picker
          v-model="filters.month"
          type="month"
          value-format="YYYY-MM"
          placeholder="账单月份"
          @change="load"
        />
        <el-button type="primary" :loading="generating" data-testid="invoice-generate" @click="generate">
          生成账单
        </el-button>
      </div>
    </div>
    <p class="sub">一期只做账单展示：线下收款后在这里标记已付款。合计（不含作废）：{{ formatMoney(total) }}</p>
    <el-table v-loading="loading" :data="invoices" data-testid="invoice-table" empty-text="暂无账单">
      <el-table-column prop="number" label="编号" width="220" />
      <el-table-column label="租户" min-width="160">
        <template #default="{ row }">{{ row.tenant_name }}<span class="sub"> {{ row.tenant_code }}</span></template>
      </el-table-column>
      <el-table-column label="账期" width="210">
        <template #default="{ row }">{{ row.period_start }} 至 {{ row.period_end }}</template>
      </el-table-column>
      <el-table-column label="明细" min-width="260">
        <template #default="{ row }">
          <div v-for="(item, i) in row.items" :key="i" class="sub">
            {{ item.description }}：{{ item.quantity }} {{ item.unit }}，{{ formatMoney(item.amount) }}
          </div>
        </template>
      </el-table-column>
      <el-table-column label="金额" width="120">
        <template #default="{ row }">{{ formatMoney(row.amount) }}</template>
      </el-table-column>
      <el-table-column label="状态" width="120">
        <template #default="{ row }">
          <el-tag disable-transitions :type="row.status === 'paid' ? 'success' : row.status === 'void' ? 'info' : 'warning'">
            {{ INVOICE_STATUS[row.status] ?? row.status }}
          </el-tag>
          <div v-if="row.paid_at" class="sub">{{ formatDateTime(row.paid_at) }}</div>
        </template>
      </el-table-column>
      <el-table-column label="操作" width="170">
        <template #default="{ row }">
          <template v-if="row.status === 'issued'">
            <el-button link type="primary" data-testid="invoice-paid" @click="setStatus(row, 'paid')">
              标记已付款
            </el-button>
            <el-button link type="danger" @click="setStatus(row, 'void')">作废</el-button>
          </template>
        </template>
      </el-table-column>
    </el-table>
  </div>
</template>

<style scoped>
.page-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 8px;
}

.page-header h2 {
  margin: 0;
  font-size: 18px;
}

.filters {
  display: flex;
  gap: 8px;
}

.sub {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
</style>
