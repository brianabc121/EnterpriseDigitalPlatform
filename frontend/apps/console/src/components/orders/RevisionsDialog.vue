<script setup lang="ts">
import { errorMessage } from '@edp/api-client'
import { ElMessage } from 'element-plus'
import { computed, reactive, ref, watch } from 'vue'

import { api, formatDateTime } from '../../api'
import {
  CHANGE_REASON,
  describeChanges,
  money,
  ORDER_STATUS,
  PAYMENT_METHOD,
  PAYMENT_STATUS,
  REVISION_KIND,
  type OrderDetail,
  type OrderRevision,
} from '../../orders'

/**
 * 修改记录（设计文档 §25.5）：每个版本的修改人、时间、原因和差异（只追加，不能编辑或删除），
 * 可以任选两个版本对比完整内容，默认对比 AI 最初生成的版本和最新版本。
 */
const open = defineModel<boolean>({ required: true })
const props = defineProps<{ order: OrderDetail }>()

type Snapshot = Record<string, unknown>
const compare = reactive({ left: 1, right: 1 })
const snapshots = ref<Record<number, Snapshot>>({})
const loading = ref(false)

const revisions = computed<OrderRevision[]>(() => [...props.order.revisions].reverse())
const firstByAi = computed(
  () => props.order.revisions.find((r) => r.actor_type === 'ai')?.version ?? 1,
)

function actor(r: OrderRevision): string {
  if (r.actor_type === 'ai') return 'AI'
  if (r.actor_type === 'system') return '系统'
  if (r.actor_type === 'api') return '企业系统'
  return r.actor_name ?? '员工'
}

async function snapshot(version: number): Promise<void> {
  if (snapshots.value[version]) return
  const { data, error } = await api.GET('/api/v1/orders/{order_id}/revisions/{version}', {
    params: { path: { order_id: props.order.id, version } },
  })
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  snapshots.value = { ...snapshots.value, [version]: data.snapshot }
}

async function load(): Promise<void> {
  loading.value = true
  await Promise.all([snapshot(compare.left), snapshot(compare.right)])
  loading.value = false
}

watch(open, (value) => {
  if (!value) return
  snapshots.value = {}
  compare.left = firstByAi.value
  compare.right = props.order.version
  void load()
})
watch(() => [compare.left, compare.right], load)

interface SnapshotItem {
  name: string
  spec: string
  raw_text: string | null
  quantity: number
  unit_price: string | null
}

function items(s: Snapshot | undefined): string {
  const list = (s?.items as SnapshotItem[] | undefined) ?? []
  return list
    .map((i) => `${[i.name, i.spec].filter(Boolean).join(' ')} × ${i.quantity}（${money(i.unit_price)}）`)
    .join('\n')
}

function receiver(s: Snapshot | undefined): string {
  const r = (s?.receiver as Record<string, string> | undefined) ?? {}
  return [r.name, r.phone, r.address].filter(Boolean).join(' ')
}

const ROWS: [string, (s: Snapshot | undefined) => string][] = [
  ['状态', (s) => ORDER_STATUS[String(s?.status ?? '')] ?? ''],
  ['商品', items],
  ['商品金额', (s) => money(s?.items_amount as string)],
  ['优惠', (s) => money(s?.discount as string)],
  ['合计', (s) => money(s?.total as string)],
  ['收款方式', (s) => PAYMENT_METHOD[String(s?.payment_method ?? '')] ?? '待定'],
  ['收款状态', (s) => PAYMENT_STATUS[String(s?.payment_status ?? '')] ?? ''],
  ['已收', (s) => money(s?.paid_amount as string)],
  ['收货信息', receiver],
  ['客户要求', (s) => String(s?.customer_note ?? '')],
  ['内部备注', (s) => String(s?.internal_note ?? '')],
  ['物流', (s) => `${s?.shipping_company ?? ''} ${s?.tracking_no ?? ''}`.trim()],
]

const rows = computed(() =>
  ROWS.map(([label, show]) => {
    const left = show(snapshots.value[compare.left])
    const right = show(snapshots.value[compare.right])
    return { label, left, right, changed: left !== right }
  }),
)
</script>

<template>
  <el-dialog
    v-model="open"
    :title="`修改记录 · ${order.no}`"
    width="860px"
    append-to-body
    data-testid="order-revisions"
  >
    <el-timeline class="timeline">
      <el-timeline-item
        v-for="r in revisions"
        :key="r.version"
        :timestamp="formatDateTime(r.created_at)"
        placement="top"
      >
        <div class="head" data-testid="order-revision">
          <b>v{{ r.version }}</b>
          <el-tag size="small" type="info">{{ REVISION_KIND[r.kind] ?? r.kind }}</el-tag>
          <span>{{ actor(r) }}</span>
          <el-tag v-if="r.reason" size="small" :type="r.reason === 'ai_error' ? 'danger' : 'warning'">{{
            CHANGE_REASON[r.reason]
          }}</el-tag>
          <span v-if="r.note" class="muted">{{ r.note }}</span>
        </div>
        <ul class="changes">
          <li v-for="(line, i) in describeChanges(r.changes)" :key="i">{{ line }}</li>
        </ul>
      </el-timeline-item>
    </el-timeline>

    <h4>对比两个版本</h4>
    <div class="pick">
      <el-select v-model="compare.left" size="small" data-testid="compare-left">
        <el-option
          v-for="r in order.revisions"
          :key="r.version"
          :value="r.version"
          :label="`v${r.version} ${actor(r)}${r.version === firstByAi ? '（AI 生成）' : ''}`"
        />
      </el-select>
      <span>对比</span>
      <el-select v-model="compare.right" size="small" data-testid="compare-right">
        <el-option
          v-for="r in order.revisions"
          :key="r.version"
          :value="r.version"
          :label="`v${r.version} ${actor(r)}${r.version === order.version ? '（最新）' : ''}`"
        />
      </el-select>
    </div>
    <el-table v-loading="loading" :data="rows" size="small" data-testid="compare-table">
      <el-table-column prop="label" label="" width="90" />
      <el-table-column :label="`v${compare.left}`">
        <template #default="{ row }"><span class="pre">{{ row.left }}</span></template>
      </el-table-column>
      <el-table-column :label="`v${compare.right}`">
        <template #default="{ row }">
          <span class="pre" :class="{ changed: row.changed }">{{ row.right }}</span>
        </template>
      </el-table-column>
    </el-table>
  </el-dialog>
</template>

<style scoped>
.timeline {
  max-height: 320px;
  overflow: auto;
  padding: 4px 8px 0 0;
}

.head {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  align-items: center;
}

.changes {
  margin: 4px 0 0;
  padding-left: 18px;
  font-size: 13px;
}

.muted {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

.pick {
  display: flex;
  gap: 8px;
  align-items: center;
  margin-bottom: 8px;
}

.pre {
  white-space: pre-wrap;
}

.changed {
  color: var(--el-color-warning-dark-2);
  font-weight: 500;
}

h4 {
  margin: 16px 0 8px;
  font-size: 13px;
}
</style>
