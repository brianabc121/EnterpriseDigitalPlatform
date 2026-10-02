<script setup lang="ts">
import { errorMessage } from '@edp/api-client'
import { ElMessage } from 'element-plus'
import { onMounted, reactive, ref } from 'vue'

import { api } from '../../api'
import { printerOptions } from '../../printer-options'
import { printedText, type PrinterOptions } from '../../printing'

/**
 * 手工打印加工单、领料单（设计文档 §29.5）：没有接入打印机时不显示；只有一台时直接打，多台时先选。
 * 旁边显示"已打印 N 次"。打印成功后告诉父组件这是第几次。
 */
const props = withDefaults(
  defineProps<{
    kind: 'order' | 'requisition'
    refId: string
    count: number
    label?: string
    size?: 'small' | 'default'
    disabled?: boolean
  }>(),
  { label: '打印', size: 'default', disabled: false },
)
const emit = defineEmits<{ printed: [seq: number] }>()

const options = ref<PrinterOptions | null>(null)
const sending = ref(false)
const pick = reactive({ open: false, printerId: '' })
const KIND_LABEL = { order: '加工单', requisition: '领料单' } as const

onMounted(async () => {
  options.value = await printerOptions(props.kind)
})

function start(): void {
  const items = options.value?.items ?? []
  if (!items.length || sending.value) return
  if (items.length === 1) {
    void send(items[0]!.id)
    return
  }
  pick.printerId = items[0]!.id
  pick.open = true
}

async function send(printerId: string): Promise<void> {
  sending.value = true
  const body = { printer_id: printerId }
  const { data, error } =
    props.kind === 'order'
      ? await api.POST('/api/v1/print/orders/{order_id}', {
          params: { path: { order_id: props.refId } },
          body,
        })
      : await api.POST('/api/v1/print/documents/{document_id}', {
          params: { path: { document_id: props.refId } },
          body,
        })
  sending.value = false
  pick.open = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  const seq = data.jobs[0]?.seq ?? props.count + 1
  ElMessage.success(`${KIND_LABEL[props.kind]}已发往打印机（第 ${seq} 次）`)
  emit('printed', seq)
}
</script>

<template>
  <span v-if="options?.items.length" class="print-button">
    <el-button
      :size="size"
      :disabled="disabled"
      :loading="sending"
      :data-testid="`print-${kind}`"
      @click="start"
    >
      {{ label }}
    </el-button>
    <el-tag v-if="count" size="small" type="info" :data-testid="`print-count-${kind}`">
      {{ printedText(count) }}
    </el-tag>
    <el-dialog
      v-model="pick.open"
      title="选择打印机"
      width="min(400px, 92vw)"
      append-to-body
      data-testid="print-pick-dialog"
    >
      <el-select v-model="pick.printerId" class="picker" data-testid="print-pick-printer">
        <el-option
          v-for="p in options?.items ?? []"
          :key="p.id"
          :label="`${p.name}（${p.status_label}）`"
          :value="p.id"
        />
      </el-select>
      <template #footer>
        <el-button @click="pick.open = false">取消</el-button>
        <el-button
          type="primary"
          :loading="sending"
          data-testid="print-pick-submit"
          @click="send(pick.printerId)"
        >
          打印
        </el-button>
      </template>
    </el-dialog>
  </span>
</template>

<style scoped>
.print-button {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}

.picker {
  width: 100%;
}
</style>
