<script setup lang="ts">
import { errorMessage } from '@edp/api-client'
import { ElMessage } from 'element-plus'
import { onMounted, reactive, ref, watch } from 'vue'
import { useRouter } from 'vue-router'

import { api, formatDateTime } from '../../api'
import { ordersChanged } from '../../orders'
import { KIND_LABEL, type WarehouseDocument } from '../../warehouse'
import DocumentDrawer from '../warehouse/DocumentDrawer.vue'
import type { HomeData } from './home'
import HomeSection from './HomeSection.vue'
import LinkTile from './LinkTile.vue'

/**
 * 仓管的首页（§25.15）：待确认的领料单和入库单（点开直接确认或退回）、库存不足的材料和成品，以及
 * 快捷入口（开领料单、开入库单、库存记录）。
 */
const props = defineProps<{ data: HomeData }>()
const emit = defineEmits<{ changed: [] }>()

const LIMIT = 10
const router = useRouter()
const docs = ref<WarehouseDocument[]>([])
const total = ref(0)
const loading = ref(false)
const drawer = reactive({ open: false, id: null as string | null })

async function load(): Promise<void> {
  loading.value = true
  const { data, error } = await api.GET('/api/v1/warehouse/documents', {
    params: { query: { status: 'pending', limit: LIMIT } },
  })
  loading.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  docs.value = data.items
  total.value = data.total
}

function openDoc(doc: WarehouseDocument): void {
  Object.assign(drawer, { open: true, id: doc.id })
}

// 确认、退回或作废后：刷新列表、首页的数字和菜单角标。
function onChanged(): void {
  ordersChanged()
  emit('changed')
  void load()
}

function go(query: Record<string, string>): void {
  void router.push({ path: '/warehouse', query })
}

function lineSummary(doc: WarehouseDocument): string {
  const [first] = doc.lines
  if (!first) return ''
  return doc.lines.length > 1 ? `${first.name} 等 ${doc.lines.length} 项` : first.name
}

// 首页每分钟刷新数字；待确认的单据数变了时重新取列表。
watch(
  () => {
    const w = props.data.warehouse
    return w ? w.pending_requisitions + w.pending_receipts : null
  },
  (count, before) => {
    if (count !== null && before !== undefined && count !== before) void load()
  },
)
onMounted(load)
</script>

<template>
  <div data-testid="home-keeper">
    <HomeSection :title="`待确认的单据${total ? `（${total}）` : ''}`" testid="home-keeper-documents">
      <template #extra>
        <el-button type="primary" data-testid="home-new-requisition" @click="go({ new: 'requisition' })"
          >开领料单</el-button
        >
        <el-button data-testid="home-new-receipt" @click="go({ new: 'receipt' })">开入库单</el-button>
        <el-button link type="primary" @click="go({ tab: 'movements' })">库存记录</el-button>
      </template>
      <ul v-loading="loading" class="list" data-testid="home-pending-documents">
        <li v-if="!docs.length" class="empty" data-testid="home-pending-empty">没有待确认的单据</li>
        <li
          v-for="doc in docs"
          :key="doc.id"
          class="row"
          data-testid="home-pending-document"
          :data-no="doc.no"
          @click="openDoc(doc)"
        >
          <span class="no">{{ doc.no }}</span>
          <el-tag size="small" :type="doc.kind === 'requisition' ? 'primary' : 'success'">{{
            KIND_LABEL[doc.kind]
          }}</el-tag>
          <span class="lines">{{ lineSummary(doc) }}</span>
          <span class="muted"
            >{{ doc.order_no ? `订单 ${doc.order_no} · ` : '' }}{{ doc.created_by_name ?? '' }}
            {{ formatDateTime(doc.submitted_at) }}</span
          >
          <el-button link type="primary" :data-testid="`home-open-document-${doc.no}`" @click.stop="openDoc(doc)">{{
            doc.can_confirm ? '去确认' : '查看'
          }}</el-button>
        </li>
      </ul>
      <div v-if="total > docs.length" class="more">
        <el-button link type="primary" @click="go({ tab: 'requisition' })">查看全部 {{ total }} 张</el-button>
      </div>
    </HomeSection>

    <HomeSection v-if="data.warehouse" title="库存不足" testid="home-keeper-low">
      <div class="tiles">
        <LinkTile
          label="材料"
          :value="data.warehouse.low_materials"
          hint="可用数量低于预警值"
          tone="danger"
          :to="{ path: '/warehouse', query: { tab: 'material', low: '1' } }"
          testid="home-low-materials"
        />
        <LinkTile
          label="成品"
          :value="data.warehouse.low_goods"
          hint="可用数量低于预警值"
          tone="danger"
          :to="{ path: '/warehouse', query: { tab: 'goods', low: '1' } }"
          testid="home-low-goods"
        />
      </div>
    </HomeSection>

    <DocumentDrawer v-model="drawer.open" :document-id="drawer.id" @changed="onChanged" />
  </div>
</template>

<style scoped>
.list {
  margin: 0;
  padding: 0;
  list-style: none;
  background: var(--el-bg-color);
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 8px;
}

.list li {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 4px 10px;
  padding: 10px 14px;
  border-bottom: 1px solid var(--el-border-color-lighter);
}

.list li:last-child {
  border-bottom: none;
}

.row {
  cursor: pointer;
}

.row:hover {
  background: var(--el-fill-color-light);
}

.empty {
  justify-content: center;
  color: var(--el-text-color-secondary);
  font-size: 13px;
}

.no {
  font-weight: 500;
}

.lines {
  flex: 1;
  min-width: 120px;
}

.muted {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

.more {
  margin-top: 8px;
  text-align: right;
}
</style>
