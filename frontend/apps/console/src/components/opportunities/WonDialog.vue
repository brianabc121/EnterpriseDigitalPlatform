<script setup lang="ts">
import { errorMessage, type Schemas } from '@edp/api-client'
import { ElMessage } from 'element-plus'
import { reactive, ref, watch } from 'vue'

import { api } from '../../api'
import { STATUS_LABEL as CONTRACT_STATUS } from '../../contracts'
import { money, ORDER_STATUS } from '../../orders'
import type { Opportunity, OpportunitySummary } from '../../opportunities'
import { useAuthStore } from '../../stores/auth'

/**
 * 赢单（设计文档 §40.5）：可以关联这个客户的订单或合同（没有也行），写说明；预计金额没填时后端取订单或
 * 合同的金额。看板拖到"赢单"列和详情里点"赢单"都用这个对话框。
 */
const open = defineModel<boolean>({ required: true })
const props = defineProps<{
  opportunity: Pick<OpportunitySummary, 'id' | 'customer_id' | 'customer_name' | 'name'> | null
}>()
const emit = defineEmits<{ done: [opportunity: Opportunity] }>()

const auth = useAuthStore()
const form = reactive({ orderId: '', contractId: '', note: '' })
const orders = ref<Schemas['OrderOut'][]>([])
const contracts = ref<Schemas['ContractSummary'][]>([])
const saving = ref(false)

async function load(): Promise<void> {
  Object.assign(form, { orderId: '', contractId: '', note: '' })
  orders.value = []
  contracts.value = []
  const customerId = props.opportunity?.customer_id
  if (!customerId) return
  const [orderList, contractList] = await Promise.all([
    auth.can('order:read')
      ? api.GET('/api/v1/orders', {
          params: { query: { customer_id: customerId, limit: 20, offset: 0 } },
        })
      : Promise.resolve(null),
    auth.can('contract:use')
      ? api.GET('/api/v1/contracts', {
          params: { query: { customer_id: customerId, limit: 20, offset: 0 } },
        })
      : Promise.resolve(null),
  ])
  orders.value = (orderList?.data?.items ?? []).filter((o) => o.status !== 'cancelled')
  contracts.value = (contractList?.data?.items ?? []).filter((c) => c.status !== 'void')
}

async function confirm(): Promise<void> {
  if (!props.opportunity) return
  saving.value = true
  const { data, error } = await api.POST('/api/v1/opportunities/{opportunity_id}/won', {
    params: { path: { opportunity_id: props.opportunity.id } },
    body: {
      order_id: form.orderId || null,
      contract_id: form.contractId || null,
      note: form.note.trim() || null,
    },
  })
  saving.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  ElMessage.success('已赢单')
  open.value = false
  emit('done', data)
}

watch(open, (value) => {
  if (value) void load()
})
</script>

<template>
  <el-dialog
    v-model="open"
    :title="opportunity ? `赢单：${opportunity.name}` : '赢单'"
    width="480px"
    append-to-body
    data-testid="opp-won-dialog"
  >
    <p class="tip">
      订单确认、合同签署时会自动赢单；没有在系统里下单时可以手动赢单，可以关联这个客户的订单或合同。
    </p>
    <el-form label-width="84px" @submit.prevent="confirm">
      <el-form-item v-if="orders.length" label="关联订单">
        <el-select v-model="form.orderId" clearable placeholder="不关联" data-testid="opp-won-order">
          <el-option
            v-for="o in orders"
            :key="o.id"
            :label="`${o.no} · ${money(o.total)} · ${ORDER_STATUS[o.status] ?? o.status}`"
            :value="o.id"
          />
        </el-select>
      </el-form-item>
      <el-form-item v-if="contracts.length" label="关联合同">
        <el-select
          v-model="form.contractId"
          clearable
          placeholder="不关联"
          data-testid="opp-won-contract"
        >
          <el-option
            v-for="c in contracts"
            :key="c.id"
            :label="`${c.no} ${c.title} · ${CONTRACT_STATUS[c.status] ?? c.status}`"
            :value="c.id"
          />
        </el-select>
      </el-form-item>
      <el-form-item label="说明">
        <el-input
          v-model="form.note"
          type="textarea"
          :rows="2"
          maxlength="500"
          placeholder="怎么成交的、金额、备注（可以不填）"
          data-testid="opp-won-note"
        />
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="open = false">取消</el-button>
      <el-button type="success" :loading="saving" data-testid="opp-won-confirm" @click="confirm">
        赢单
      </el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.tip {
  margin: 0 0 12px;
  font-size: 13px;
  color: var(--el-text-color-secondary);
}
</style>
