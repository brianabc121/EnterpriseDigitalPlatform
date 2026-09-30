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
      <el-table
        v-loading="loading"
        :data="docs"
        row-key="id"
        empty-text="没有待确认的单据"
        class="docs"
        data-testid="home-pending-documents"
        @row-click="openDoc"
      >
        <el-table-column label="单号" min-width="150">
          <template #default="{ row }">
            <span class="no" data-testid="home-pending-document" :data-no="row.no">{{ row.no }}</span>
            <el-tag size="small" :type="row.kind === 'requisition' ? 'primary' : 'success'" class="gap">{{
              KIND_LABEL[row.kind as WarehouseDocument['kind']]
            }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="明细" min-width="180">
          <template #default="{ row }">{{ lineSummary(row) }}</template>
        </el-table-column>
        <el-table-column label="关联订单" width="150">
          <template #default="{ row }">{{ row.order_no ?? '—' }}</template>
        </el-table-column>
        <el-table-column label="开单" width="200">
          <template #default="{ row }">{{ row.created_by_name ?? '' }} {{ formatDateTime(row.submitted_at) }}</template>
        </el-table-column>
        <el-table-column width="90" align="right">
          <template #default="{ row }">
            <el-button link type="primary" :data-testid="`home-open-document-${row.no}`" @click.stop="openDoc(row)"
              >{{ row.can_confirm ? '去确认' : '查看' }}</el-button
            >
          </template>
        </el-table-column>
      </el-table>
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
.docs :deep(.el-table__row) {
  cursor: pointer;
}

.no {
  font-family: var(--el-font-family);
  font-weight: 500;
}

.gap {
  margin-left: 6px;
}

.more {
  margin-top: 8px;
  text-align: right;
}
</style>
