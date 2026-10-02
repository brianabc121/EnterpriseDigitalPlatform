<script setup lang="ts">
import { errorMessage } from '@edp/api-client'
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed, reactive, ref, watch } from 'vue'

import { api, formatDateTime } from '../../api'
import { quantityTotal } from '../../documents'
import { documentHtml, printHtml } from '../../print'
import { useAuthStore } from '../../stores/auth'
import {
  KIND_LABEL,
  qty,
  STATUS_TAG,
  stockAfterDocument,
  type WarehouseDocument,
} from '../../warehouse'
import DocSheet, { type SheetStep } from '../documents/DocSheet.vue'
import HistoryDrawer from '../history/HistoryDrawer.vue'
import PrintButton from '../printing/PrintButton.vue'
import DocumentEditor from './DocumentEditor.vue'

/**
 * 领料单、入库单的详情（设计文档 §25.13、§25.14）：和开单同样的版式，只读。仓管确认时直接在表格的
 * "实际数量"里修改（领料时库存不够也可以确认，库存变成负数），或者退回（写明原因）；开单人可以修改
 * 后重新提交，或者作废还没生效的单据。可以查看修改历史、打印。
 */
const open = defineModel<boolean>({ required: true })
const props = defineProps<{ documentId: string | null }>()
const emit = defineEmits<{ changed: [document: WarehouseDocument] }>()

const auth = useAuthStore()
const doc = ref<WarehouseDocument | null>(null)
const loading = ref(false)
const acting = ref(false)
const quantities = reactive<Record<string, number>>({})
const editing = ref(false)
const history = ref(false)

// 仓管按实际数量修改过的行（材料最多三位小数，提交时四舍五入）。
const adjusted = computed(() =>
  (doc.value?.lines ?? []).filter((line) => rounded(line.id) !== line.quantity),
)
const verb = computed(() => (doc.value?.kind === 'requisition' ? '领用' : '入库'))
const quantitySum = computed(() =>
  quantityTotal((doc.value?.lines ?? []).map((l) => ({ quantity: quantities[l.id] ?? l.quantity, unit: l.unit }))),
)
const steps = computed<SheetStep[]>(() => {
  const d = doc.value
  if (!d) return []
  const opened: SheetStep = {
    label: '开单',
    state: 'done',
    note: `${d.created_by_name ?? ''} ${formatDateTime(d.submitted_at)}`.trim(),
  }
  if (d.status === 'confirmed') {
    return [
      opened,
      {
        label: '仓管确认',
        state: 'done',
        note: `${d.confirmed_by_name ?? ''} ${d.confirmed_at ? formatDateTime(d.confirmed_at) : ''}`,
      },
      { label: '已生效', state: 'done' },
    ]
  }
  if (d.status === 'rejected') {
    return [opened, { label: '已退回', state: 'error', note: d.reject_reason ?? '' }, { label: '修改后重新提交', state: 'todo' }]
  }
  if (d.status === 'voided') {
    return [opened, { label: '已作废', state: 'error', note: d.void_reason ?? '' }]
  }
  return [opened, { label: '仓管确认', state: 'current', note: '等待确认' }, { label: '已生效', state: 'todo' }]
})

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
  if (!current || acting.value) return
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

function print(): void {
  if (!doc.value) return
  if (!printHtml(documentHtml(doc.value, auth.me?.tenant.name ?? ''))) {
    ElMessage.warning('浏览器拦截了打印窗口，请允许弹出窗口后再试')
  }
}
</script>

<template>
  <DocSheet
    v-model="open"
    :title="doc ? KIND_LABEL[doc.kind] : '单据'"
    :no="doc?.no ?? ''"
    :status="doc ? { label: doc.status_label, type: STATUS_TAG[doc.status] } : null"
    :steps="steps"
    :loading="loading"
    testid="document-drawer"
    status-testid="document-status"
    @save="doc?.can_confirm && confirm()"
  >
    <template #actions>
      <el-button v-if="doc" size="small" data-testid="document-history" @click="history = true">历史</el-button>
      <el-button v-if="doc" size="small" data-testid="document-print" @click="print">打印</el-button>
      <PrintButton
        v-if="doc?.kind === 'requisition'"
        kind="requisition"
        :ref-id="doc.id"
        :count="doc.print_count"
        label="云打印"
        size="small"
        @printed="(seq) => doc && (doc.print_count = seq)"
      />
    </template>
    <template v-if="doc">
      <section class="doc-section">
        <div class="doc-section-head"><h4>基本信息</h4></div>
        <div class="doc-fields">
          <div class="doc-field">
            <label>关联订单</label>
            <span class="value" data-testid="document-order">{{ doc.order_no ?? '不关联订单' }}</span>
          </div>
          <div class="doc-field">
            <label>开单人</label>
            <span class="value">{{ doc.created_by_name ?? '—' }}</span>
          </div>
          <div class="doc-field">
            <label>开单时间</label>
            <span class="value">{{ formatDateTime(doc.submitted_at) }}</span>
          </div>
          <div v-if="doc.confirmed_at" class="doc-field">
            <label>确认人</label>
            <span class="value">{{ doc.confirmed_by_name ?? '—' }} {{ formatDateTime(doc.confirmed_at) }}</span>
          </div>
          <div v-if="doc.status === 'rejected'" class="doc-field wide">
            <label>退回原因</label>
            <span class="value danger" data-testid="document-reject-reason"
              >{{ doc.rejected_by_name ?? '' }}：{{ doc.reject_reason }}</span
            >
          </div>
          <div v-if="doc.status === 'voided'" class="doc-field wide">
            <label>作废</label>
            <span class="value">{{ doc.voided_by_name ?? '系统' }} {{ doc.void_reason ?? '' }}</span>
          </div>
          <div v-if="doc.note" class="doc-field full">
            <label>备注</label>
            <span class="value">{{ doc.note }}</span>
          </div>
        </div>
      </section>

      <section class="doc-section">
        <div class="doc-section-head"><h4>明细</h4></div>
        <el-alert
          v-if="doc.short.length"
          type="warning"
          :closable="false"
          show-icon
          class="tip"
          data-testid="document-short"
          :title="`库存不够：${doc.short.join('、')}。可以照常确认，确认后库存会是负数，请及时补货或盘点。`"
        />
        <table class="doc-grid" data-testid="document-detail-lines">
          <colgroup>
            <col class="c-seq" />
            <col />
            <col class="c-unit" />
            <col class="c-num" />
            <col class="c-qty" />
            <col class="c-change" />
          </colgroup>
          <thead>
            <tr>
              <th class="seq">#</th>
              <th>{{ doc.kind === 'requisition' ? '材料' : '成品' }}</th>
              <th>单位</th>
              <th class="num">建议数量</th>
              <th>{{ doc.can_confirm ? `实际${verb}` : '数量' }}</th>
              <th class="num">库存变化</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="(row, index) in doc.lines" :key="row.id" class="line">
              <td class="seq">{{ index + 1 }}</td>
              <td class="main">
                <div class="item-name" data-testid="document-detail-line" :data-name="row.name">{{ row.name }}</div>
                <div class="item-sub">{{ [row.code, row.spec].filter(Boolean).join(' · ') }}</div>
              </td>
              <td data-label="单位">{{ row.unit || '—' }}</td>
              <td class="num" data-label="建议">{{ row.planned === null ? '—' : qty(row.planned) }}</td>
              <td :data-label="doc.can_confirm ? `实际${verb}` : '数量'">
                <el-input-number
                  v-if="doc.can_confirm"
                  v-model="quantities[row.id]"
                  :min="0"
                  :max="100000000"
                  :precision="doc.kind === 'receipt' ? 0 : undefined"
                  size="small"
                  controls-position="right"
                  data-testid="document-confirm-quantity"
                />
                <b v-else data-testid="document-detail-quantity">{{ qty(row.quantity) }}</b>
              </td>
              <td class="num" data-label="库存">
                <span v-if="doc.status === 'confirmed'" data-testid="document-detail-stock"
                  >{{ qty(row.stock_before) }} → {{ qty(row.stock_after) }}</span
                >
                <template v-else-if="doc.status === 'pending' || doc.status === 'rejected'">
                  {{ qty(row.stock) }}
                  <span
                    :class="{
                      warn:
                        doc.kind === 'requisition' &&
                        stockAfterDocument(doc.kind, row.stock, quantities[row.id] ?? row.quantity) < 0,
                    }"
                    >→ {{ qty(stockAfterDocument(doc.kind, row.stock, quantities[row.id] ?? row.quantity)) }}</span
                  >
                </template>
                <template v-else>—</template>
              </td>
            </tr>
          </tbody>
          <tfoot>
            <tr>
              <td class="seq"></td>
              <td>合计</td>
              <td colspan="2">共 {{ doc.lines.length }} 项</td>
              <td>{{ quantitySum }}</td>
              <td></td>
            </tr>
          </tfoot>
        </table>
        <p v-if="doc.can_confirm" class="doc-muted hint">按实际交接的数量修改后再确认；为 0 的行不领（不入库）。</p>
      </section>
    </template>
    <template v-if="doc" #footer>
      <span class="doc-foot-summary">共 {{ doc.lines.length }} 项</span>
      <div class="doc-foot-buttons">
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
      </div>
    </template>
  </DocSheet>
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
  <HistoryDrawer
    v-if="doc"
    v-model="history"
    :record-type="doc.kind"
    :record-id="doc.id"
    :title="`${doc.kind_label} ${doc.no}`"
  />
</template>

<style scoped>
.tip {
  margin-bottom: 12px;
}

.hint {
  margin: 8px 0 0;
}

.danger {
  color: var(--el-color-danger);
}
</style>
