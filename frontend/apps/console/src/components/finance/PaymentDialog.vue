<script setup lang="ts">
import { errorMessage, type Schemas } from '@edp/api-client'
import { ElMessage } from 'element-plus'
import { reactive, ref, watch } from 'vue'

import { api } from '../../api'
import { ordersChanged, PAYMENT_CHANNELS } from '../../orders'

/**
 * 登记收款、退款的对话框（§25.5），订单详情和应收账款页共用：金额默认是未收金额（退款默认是
 * 已收减已退），渠道、时间、流水号、凭证链接和备注。
 */
const props = defineProps<{
  modelValue: boolean
  orderId: string | null
  kind?: 'payment' | 'refund'
  /** 未收金额：登记收款时的默认金额。 */
  outstanding?: string | number | null
  /** 已收减已退：登记退款时的默认金额。 */
  paid?: string | number | null
}>()
const emit = defineEmits<{ 'update:modelValue': [boolean]; saved: [Schemas['OrderDetail']] }>()

const acting = ref(false)
const form = reactive({
  amount: '',
  channel: 'wechat' as Schemas['PaymentIn']['channel'],
  paidAt: '',
  referenceNo: '',
  proofUrl: '',
  note: '',
})

function reset(): void {
  const paid = Number(props.paid ?? 0)
  Object.assign(form, {
    amount:
      (props.kind ?? 'payment') === 'payment'
        ? String(props.outstanding ?? '')
        : paid > 0
          ? paid.toFixed(2)
          : '',
    channel: 'wechat',
    paidAt: '',
    referenceNo: '',
    proofUrl: '',
    note: '',
  })
}

watch(
  () => props.modelValue,
  (open) => {
    if (open) reset()
  },
)

function close(): void {
  emit('update:modelValue', false)
}

async function submit(): Promise<void> {
  if (!props.orderId) return
  if (!(Number(form.amount) > 0)) {
    ElMessage.warning('请填写金额')
    return
  }
  acting.value = true
  const { data, error } = await api.POST('/api/v1/orders/{order_id}/payments', {
    params: { path: { order_id: props.orderId } },
    body: {
      kind: props.kind ?? 'payment',
      amount: form.amount,
      channel: form.channel,
      paid_at: form.paidAt || null,
      reference_no: form.referenceNo.trim() || null,
      proof_url: form.proofUrl.trim() || null,
      note: form.note.trim() || null,
    },
  })
  acting.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  ElMessage.success(props.kind === 'refund' ? '已登记退款' : '已登记收款')
  ordersChanged()
  emit('saved', data)
  close()
}
</script>

<template>
  <el-dialog
    :model-value="modelValue"
    :title="kind === 'refund' ? '登记退款' : '登记收款'"
    width="460px"
    append-to-body
    data-testid="payment-dialog"
    @update:model-value="emit('update:modelValue', $event)"
  >
    <el-form label-width="80px">
      <el-form-item label="金额" required>
        <el-input v-model="form.amount" data-testid="payment-amount"><template #prefix>¥</template></el-input>
      </el-form-item>
      <el-form-item label="渠道" required>
        <el-select v-model="form.channel" data-testid="payment-channel">
          <el-option v-for="[value, label] in PAYMENT_CHANNELS" :key="value" :label="label" :value="value" />
        </el-select>
      </el-form-item>
      <el-form-item label="时间">
        <el-date-picker
          v-model="form.paidAt"
          type="datetime"
          value-format="YYYY-MM-DDTHH:mm:ssZ"
          placeholder="不填为现在"
        />
      </el-form-item>
      <el-form-item label="流水号">
        <el-input v-model="form.referenceNo" maxlength="64" />
      </el-form-item>
      <el-form-item label="凭证链接">
        <el-input v-model="form.proofUrl" maxlength="1024" placeholder="https://" />
      </el-form-item>
      <el-form-item label="备注">
        <el-input v-model="form.note" maxlength="500" />
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="close">取消</el-button>
      <el-button type="primary" :loading="acting" data-testid="payment-submit" @click="submit">登记</el-button>
    </template>
  </el-dialog>
</template>
