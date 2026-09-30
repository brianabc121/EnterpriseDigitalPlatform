<script setup lang="ts">
import { errorMessage } from '@edp/api-client'
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed, reactive, ref, watch } from 'vue'

import { api, formatDateTime } from '../../api'
import {
  KIND_LABEL,
  qty,
  STATUS_TAG,
  stockAfterDocument,
  type WarehouseDocument,
} from '../../warehouse'
import DocumentEditor from './DocumentEditor.vue'

/**
 * 领料单、入库单的详情（设计文档 §25.13）：仓管确认（可以按实际数量修改，领料时库存不够也可以
 * 确认，库存变成负数）或退回（写明原因）；开单人可以修改后重新提交，或者作废还没生效的单据。
 */
const open = defineModel<boolean>({ required: true })
const props = defineProps<{ documentId: string | null }>()
const emit = defineEmits<{ changed: [document: WarehouseDocument] }>()

const doc = ref<WarehouseDocument | null>(null)
const loading = ref(false)
const acting = ref(false)
const quantities = reactive<Record<string, number>>({})
const editing = ref(false)

// 仓管按实际数量修改过的行（材料最多三位小数，提交时四舍五入）。
const adjusted = computed(() =>
  (doc.value?.lines ?? []).filter((line) => rounded(line.id) !== line.quantity),
)

function rounded(id: string): number {
  return Math.round((quantities[id] ?? 0) * 1000) / 1000
}

async function load(): Promise<void> {
  if (!props.documentId) return
  loading.value = true
  const { data, error } = await api.GET('/api/v1/warehouse/documents/{document_id}', {
    params: { path: { document_id: props.documentId } },
  })
  loading.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  show(data)
}

function show(data: WarehouseDocument): void {
  doc.value = data
  for (const key of Object.keys(quantities)) delete quantities[key]
  for (const line of data.lines) quantities[line.id] = line.quantity
}

watch(open, (value) => {
  if (!value) return
  doc.value = null
  void load()
})

function changed(data: WarehouseDocument): void {
  show(data)
  emit('changed', data)
}

async function confirm(): Promise<void> {
  const current = doc.value
  if (!current) return
  const lines = adjusted.value.map((line) => ({ id: line.id, quantity: rounded(line.id) }))
  const short =
    current.kind === 'requisition'
      ? current.lines.filter((line) => (line.stock ?? 0) < rounded(line.id)).map((l) => l.name)
      : []
  const message = [
    current.kind === 'requisition'
      ? '确认后扣减材料库存（已经把材料交给领料人）。'
      : `确认后增加成品库存${current.order_no ? `，订单 ${current.order_no} 交给客服发货` : ''}。`,
    lines.length ? `已按实际数量修改 ${lines.length} 行。` : '',
    short.length ? `库存不够：${short.join('、')}，确认后库存会是负数，请及时补货或盘点。` : '',
  ].join('')
  try {
    await ElMessageBox.confirm(message, `确认${current.kind_label} ${current.no}`, {
      confirmButtonText: '确认',
      type: short.length ? 'warning' : 'info',
    })
  } catch {
    return
  }
  acting.value = true
  const { data, error } = await api.POST('/api/v1/warehouse/documents/{document_id}/confirm', {
    params: { path: { document_id: current.id } },
    body: { lines: lines.length ? lines : null },
  })
  acting.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  ElMessage.success(`${data.kind_label} ${data.no} 已确认，库存已更新`)
  changed(data)
}

async function ask(title: string, placeholder: string, required: boolean): Promise<string | null> {
  try {
    const { value } = await ElMessageBox.prompt(placeholder, title, {
      confirmButtonText: '确定',
      inputPattern: required ? /\S/ : undefined,
      inputErrorMessage: required ? '请填写原因' : undefined,
      inputPlaceholder: required ? '必填' : '选填',
    })
    return (value ?? '').trim()
  } catch {
    return null
  }
}

async function reject(): Promise<void> {
  const current = doc.value
  if (!current) return
  const reason = await ask(`退回${current.kind_label} ${current.no}`, '退回的原因（开单人会收到提醒，修改后重新提交）', true)
  if (reason === null) return
  acting.value = true
  const { data, error } = await api.POST('/api/v1/warehouse/documents/{document_id}/reject', {
    params: { path: { document_id: current.id } },
    body: { reason },
  })
  acting.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  ElMessage.success('已退回')
  changed(data)
}

async function voidDocument(): Promise<void> {
  const current = doc.value
  if (!current) return
  const reason = await ask(`作废${current.kind_label} ${current.no}`, '作废的原因', false)
  if (reason === null) return
  acting.value = true
  const { data, error } = await api.POST('/api/v1/warehouse/documents/{document_id}/void', {
    params: { path: { document_id: current.id } },
    body: { reason },
  })
  acting.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  ElMessage.success('已作废')
  changed(data)
}

function edited(data: unknown): void {
  changed(data as WarehouseDocument)
}
</script>

<template>
  <el-drawer
    v-model="open"
    :title="doc ? `${KIND_LABEL[doc.kind]} ${doc.no}` : '单据'"
    size="min(760px, 100%)"
    append-to-body
    data-testid="document-drawer"
  >
    <div v-loading="loading">
      <template v-if="doc">
        <div class="head">
          <el-tag :type="STATUS_TAG[doc.status]" data-testid="document-status">{{ doc.status_label }}</el-tag>
          <span v-if="doc.order_no" class="muted" data-testid="document-order">订单 {{ doc.order_no }}</span>
          <span v-else class="muted">不关联订单</span>
        </div>
        <dl class="meta">
          <dt>开单</dt>
          <dd>{{ doc.created_by_name ?? '—' }} {{ formatDateTime(doc.submitted_at) }}</dd>
          <template v-if="doc.confirmed_at">
            <dt>确认</dt>
            <dd>{{ doc.confirmed_by_name ?? '—' }} {{ formatDateTime(doc.confirmed_at) }}</dd>
          </template>
          <template v-if="doc.status === 'rejected'">
            <dt>退回</dt>
            <dd class="danger" data-testid="document-reject-reason">
              {{ doc.rejected_by_name ?? '' }}：{{ doc.reject_reason }}
            </dd>
          </template>
          <template v-if="doc.status === 'voided'">
            <dt>作废</dt>
            <dd>{{ doc.voided_by_name ?? '系统' }} {{ doc.void_reason ?? '' }}</dd>
          </template>
          <template v-if="doc.note">
            <dt>备注</dt>
            <dd>{{ doc.note }}</dd>
          </template>
        </dl>
        <el-alert
          v-if="doc.short.length"
          type="warning"
          :closable="false"
          show-icon
          class="tip"
          data-testid="document-short"
          :title="`库存不够：${doc.short.join('、')}。可以照常确认，确认后库存会是负数，请及时补货或盘点。`"
        />
        <div class="lines" data-testid="document-detail-lines">
          <div v-for="row in doc.lines" :key="row.id" class="line">
            <div class="line-main">
              <div>
                <span data-testid="document-detail-line" :data-name="row.name">{{ row.name }}</span>
                <span v-if="row.spec" class="muted"> {{ row.spec }}</span>
              </div>
              <div class="muted">
                <template v-if="row.planned !== null">建议 {{ qty(row.planned) }} · </template>
                <span v-if="doc.status === 'confirmed'" data-testid="document-detail-stock"
                  >{{ qty(row.stock_before) }} → {{ qty(row.stock_after) }}</span
                >
                <template v-else-if="doc.status === 'pending' || doc.status === 'rejected'">
                  现有 {{ qty(row.stock) }}
                  <span
                    :class="{
                      minus:
                        doc.kind === 'requisition' &&
                        stockAfterDocument(doc.kind, row.stock, quantities[row.id] ?? row.quantity) < 0,
                    }"
                    >→ {{ qty(stockAfterDocument(doc.kind, row.stock, quantities[row.id] ?? row.quantity)) }}</span
                  >
                </template>
              </div>
            </div>
            <div class="line-qty">
              <el-input-number
                v-if="doc.can_confirm"
                v-model="quantities[row.id]"
                :min="0"
                :max="100000000"
                :precision="doc.kind === 'receipt' ? 0 : undefined"
                size="small"
                controls-position="right"
                class="qty"
                data-testid="document-confirm-quantity"
              />
              <b v-else data-testid="document-detail-quantity">{{ qty(row.quantity) }}</b>
              <span class="unit">{{ row.unit }}</span>
            </div>
          </div>
        </div>
        <p v-if="doc.can_confirm" class="muted">按实际交接的数量修改后再确认；为 0 的行不领（不入库）。</p>
      </template>
    </div>
    <template v-if="doc" #footer>
      <el-button v-if="doc.can_void" :disabled="acting" data-testid="document-void" @click="voidDocument"
        >作废</el-button
      >
      <el-button v-if="doc.can_edit && !doc.can_confirm" :disabled="acting" data-testid="document-edit" @click="editing = true"
        >修改后重新提交</el-button
      >
      <el-button v-if="doc.can_confirm" type="danger" plain :disabled="acting" data-testid="document-reject" @click="reject"
        >退回</el-button
      >
      <el-button v-if="doc.can_confirm" type="primary" :loading="acting" data-testid="document-confirm" @click="confirm"
        >确认{{ doc.kind === 'requisition' ? '领料' : '入库' }}</el-button
      >
    </template>
    <DocumentEditor
      v-if="doc"
      v-model="editing"
      :kind="doc.kind"
      mode="edit"
      :order-id="doc.order_id"
      :order-no="doc.order_no"
      :document="doc"
      @saved="edited"
    />
  </el-drawer>
</template>

<style scoped>
.head {
  display: flex;
  align-items: center;
  gap: 8px;
}

.meta {
  display: grid;
  grid-template-columns: 56px 1fr;
  gap: 4px 8px;
  margin: 12px 0;
  font-size: 13px;
}

.meta dt {
  color: var(--el-text-color-secondary);
}

.meta dd {
  margin: 0;
}

.tip {
  margin-bottom: 12px;
}

.lines {
  border-top: 1px solid var(--el-border-color-lighter);
}

.line {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px 12px;
  padding: 8px 0;
  border-bottom: 1px solid var(--el-border-color-lighter);
}

.line-main {
  flex: 1;
  min-width: 160px;
}

.line-qty {
  display: flex;
  align-items: center;
  gap: 6px;
}

.qty {
  width: 120px;
}

.unit {
  min-width: 42px;
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

.minus,
.danger {
  color: var(--el-color-danger);
}

.minus {
  font-weight: 600;
}

.muted {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}
</style>
