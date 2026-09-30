<script setup lang="ts">
import { errorMessage, type Schemas } from '@edp/api-client'
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { api, formatDateTime } from '../api'
import HistoryDrawer from '../components/history/HistoryDrawer.vue'
import ProductDialog from '../components/orders/ProductDialog.vue'
import ProductImportDialog from '../components/orders/ProductImportDialog.vue'
import StockDialog from '../components/orders/StockDialog.vue'
import StockHistory from '../components/orders/StockHistory.vue'
import BomDialog from '../components/warehouse/BomDialog.vue'
import DocumentDrawer from '../components/warehouse/DocumentDrawer.vue'
import DocumentEditor from '../components/warehouse/DocumentEditor.vue'
import KeeperDialog from '../components/warehouse/KeeperDialog.vue'
import { downloadBlob } from '../download'
import { changeText, deltaText } from '../inventory'
import { ordersChanged, type Product } from '../orders'
import { useAuthStore } from '../stores/auth'
import {
  DOCUMENT_FILTERS,
  ITEM_KIND_LABEL,
  KIND_LABEL,
  qty,
  STATUS_TAG,
  WAREHOUSE_TABS,
  type DocumentKind,
  type DocumentStatus,
  type ItemKind,
  type StockItem,
  type WarehouseDocument,
  type WarehouseTab,
} from '../warehouse'

/**
 * 仓库（设计文档 §25.13）：材料库存和成品库存（现有、占用或待领、可用，库存不足的标出来；盘点、
 * 入库、出库，库存记录）、成品的配方、领料单和入库单（仓管确认或退回）、全部库存记录，以及仓管
 * 设置。站内信里的链接带 doc（待确认的单据），打开后直接显示这张单据。
 */
const PAGE_SIZE = 50
const auth = useAuthStore()
const route = useRoute()
const router = useRouter()

const canConfig = computed(() => auth.can('order:config'))
const canMaterial = computed(() => auth.can('inventory:manage') || auth.can('product:manage'))
const canBom = computed(() => auth.can('product:manage'))

// 首页的链接（§25.15）：?tab=…打开这个标签页，?low=1 只看库存不足，?new=requisition|receipt 开单。
function linkedTab(): WarehouseTab | null {
  const { tab: target, new: kind } = route.query
  if (kind === 'requisition' || kind === 'receipt') return kind
  return WAREHOUSE_TABS.some(([name]) => name === target) ? (target as WarehouseTab) : null
}

const linked = linkedTab()
const tab = ref<WarehouseTab>(linked ?? 'material')
const settings = ref<Schemas['WarehouseSettingsOut'] | null>(null)
const counts = ref<Schemas['WarehouseCounts'] | null>(null)

const items = ref<StockItem[]>([])
const itemTotal = ref(0)
const itemPage = ref(1)
const itemLoading = ref(false)
const itemFilters = reactive({ q: '', low: route.query.low === '1' })
const itemKind = computed<ItemKind>(() => (tab.value === 'goods' ? 'goods' : 'material'))

const docs = ref<WarehouseDocument[]>([])
const docTotal = ref(0)
const docPage = ref(1)
const docLoading = ref(false)
const docFilters = reactive({ status: '' as DocumentStatus | '', q: '', mine: false })
const docKind = computed<DocumentKind>(() => (tab.value === 'receipt' ? 'receipt' : 'requisition'))

const moves = ref<Schemas['StockMovementOut'][]>([])
const moveTotal = ref(0)
const movePage = ref(1)
const moveLoading = ref(false)
const moveFilters = reactive({ kind: '' as ItemKind | '', q: '' })

const stocking = reactive({ open: false, item: null as StockItem | null })
const history = reactive({ open: false, item: null as StockItem | null })
// 修改历史（§25.14）。
const versions = reactive({ open: false, id: null as string | null, kind: 'material' as 'material' | 'goods', title: '' })
const bom = reactive({ open: false, item: null as StockItem | null })
const editing = reactive({ open: false, product: null as Product | null })
const importing = ref(false)
const creating = reactive({ open: false, kind: 'requisition' as DocumentKind })
const drawer = reactive({ open: false, id: null as string | null })
const keeper = ref(false)
const exporting = ref(false)

function badge(name: WarehouseTab): number {
  const c = counts.value
  if (!c) return 0
  return (
    { material: c.low_materials, goods: c.low_goods, requisition: c.pending_requisitions, receipt: c.pending_receipts }[
      name as 'material'
    ] ?? 0
  )
}

async function loadSettings(): Promise<void> {
  const { data } = await api.GET('/api/v1/warehouse/settings')
  settings.value = data ?? null
}

async function loadCounts(): Promise<void> {
  const { data } = await api.GET('/api/v1/warehouse/counts')
  if (data) counts.value = data
}

async function loadItems(): Promise<void> {
  itemLoading.value = true
  const { data, error } = await api.GET('/api/v1/warehouse/items', {
    params: {
      query: {
        kind: itemKind.value,
        q: itemFilters.q.trim() || undefined,
        stock: itemFilters.low ? 'low' : undefined,
        limit: PAGE_SIZE,
        offset: (itemPage.value - 1) * PAGE_SIZE,
      },
    },
  })
  itemLoading.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  items.value = data.items
  itemTotal.value = data.total
}

async function loadDocs(): Promise<void> {
  docLoading.value = true
  const { data, error } = await api.GET('/api/v1/warehouse/documents', {
    params: {
      query: {
        kind: docKind.value,
        status: docFilters.status || undefined,
        q: docFilters.q.trim() || undefined,
        mine: docFilters.mine || undefined,
        limit: PAGE_SIZE,
        offset: (docPage.value - 1) * PAGE_SIZE,
      },
    },
  })
  docLoading.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  docs.value = data.items
  docTotal.value = data.total
}

async function loadMoves(): Promise<void> {
  moveLoading.value = true
  const { data, error } = await api.GET('/api/v1/warehouse/movements', {
    params: {
      query: {
        kind: moveFilters.kind || undefined,
        q: moveFilters.q.trim() || undefined,
        limit: PAGE_SIZE,
        offset: (movePage.value - 1) * PAGE_SIZE,
      },
    },
  })
  moveLoading.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  moves.value = data.items
  moveTotal.value = data.total
}

function loadTab(): Promise<void> {
  if (tab.value === 'material' || tab.value === 'goods') return loadItems()
  if (tab.value === 'movements') return loadMoves()
  return loadDocs()
}

async function refresh(): Promise<void> {
  await Promise.all([loadTab(), loadCounts()])
}

function searchItems(): void {
  itemPage.value = 1
  void loadItems()
}

function searchDocs(): void {
  docPage.value = 1
  void loadDocs()
}

function searchMoves(): void {
  movePage.value = 1
  void loadMoves()
}

function linesText(doc: WarehouseDocument): string {
  return doc.lines.map((l) => `${l.name} ${qty(l.quantity)}${l.unit}`).join('、')
}

function openDoc(id: string): void {
  Object.assign(drawer, { open: true, id })
}

function onDocChanged(): void {
  ordersChanged()
  void refresh()
}

function newDocument(kind: DocumentKind): void {
  Object.assign(creating, { open: true, kind })
}

function onCreated(result: unknown): void {
  const doc = result as WarehouseDocument
  ordersChanged()
  if (tab.value === doc.kind) void refresh()
  else tab.value = doc.kind
}

async function editMaterial(item: StockItem): Promise<void> {
  const { data, error } = await api.GET('/api/v1/products/{product_id}', {
    params: { path: { product_id: item.id } },
  })
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  Object.assign(editing, { open: true, product: data })
}

function newMaterial(): void {
  Object.assign(editing, { open: true, product: null })
}

async function removeMaterial(item: StockItem): Promise<void> {
  try {
    await ElMessageBox.confirm(
      `删除材料「${item.name}」？已经领过料、或者配方里用到的材料不能删除，可以改为停用。`,
      '删除材料',
      { confirmButtonText: '删除', cancelButtonText: '取消', type: 'warning' },
    )
  } catch {
    return
  }
  const { error } = await api.DELETE('/api/v1/products/{product_id}', {
    params: { path: { product_id: item.id } },
  })
  if (error) {
    ElMessage.error(errorMessage(error))
    return
  }
  ElMessage.success('已删除')
  await refresh()
}

async function exportItems(): Promise<void> {
  exporting.value = true
  const { data, error } = await api.GET('/api/v1/products/export', {
    params: { query: { kind: itemKind.value, q: itemFilters.q.trim() || undefined } },
    parseAs: 'blob',
  })
  exporting.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  const name = itemKind.value === 'material' ? '材料' : '成品'
  downloadBlob(data as Blob, `${name}-${new Date().toISOString().slice(0, 10)}.xlsx`)
}

function onBomSaved(count: number): void {
  if (bom.item) bom.item.materials = count
}

// 站内信链接（/warehouse?doc=…）：切到单据所在的标签页，打开这张单据。
async function openLinked(): Promise<void> {
  const id = route.query.doc
  if (typeof id !== 'string' || !id) return
  void router.replace({ query: { ...route.query, doc: undefined } })
  const { data, error } = await api.GET('/api/v1/warehouse/documents/{document_id}', {
    params: { path: { document_id: id } },
  })
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  tab.value = data.kind
  openDoc(data.id)
}

watch(tab, () => {
  itemPage.value = docPage.value = movePage.value = 1
  void loadTab()
})
watch(() => [itemFilters.low], searchItems)
watch(() => [docFilters.status, docFilters.mine], searchDocs)
watch(() => moveFilters.kind, searchMoves)
watch(
  () => route.query.doc,
  (id) => {
    if (id) void openLinked()
  },
)

onMounted(async () => {
  await Promise.all([loadSettings(), loadCounts()])
  const { tab: target, low, new: kind } = route.query
  if (target !== undefined || low !== undefined || kind !== undefined) {
    void router.replace({ query: { ...route.query, tab: undefined, low: undefined, new: undefined } })
    if (kind === 'requisition' || kind === 'receipt') newDocument(kind)
  }
  if (route.query.doc) {
    await openLinked()
  } else if (!linked && counts.value?.pending_requisitions && auth.can('warehouse:confirm')) {
    // 仓管打开时先看待确认的领料单。
    tab.value = 'requisition'
    return
  }
  await loadTab()
})
</script>

<template>
  <div>
    <div class="page-header">
      <h2>仓库</h2>
      <span v-if="settings" class="keeper" data-testid="warehouse-keeper">
        仓管：<b>{{ settings.effective_keeper_name ?? '未设置（由管理员确认）' }}</b>
        <span v-if="settings.by_role" class="muted">（“仓管”角色）</span>
        <span v-else-if="settings.fallback && settings.effective_keeper_name" class="muted">（最早创建的工人）</span>
        <span v-if="!settings.confirm_required" class="muted">· 单据开单即生效</span>
        <el-button v-if="canConfig" link type="primary" data-testid="warehouse-settings" @click="keeper = true"
          >设置</el-button
        >
      </span>
    </div>

    <el-tabs v-model="tab" data-testid="warehouse-tabs">
      <el-tab-pane v-for="[name, label] in WAREHOUSE_TABS" :key="name" :name="name">
        <template #label>
          <span :data-testid="`warehouse-tab-${name}`">
            {{ label }}
            <el-badge
              v-if="badge(name)"
              :value="badge(name)"
              :max="99"
              :type="name === 'material' || name === 'goods' ? 'danger' : 'warning'"
              class="badge"
            />
          </span>
        </template>
      </el-tab-pane>
    </el-tabs>

    <!-- 材料库存、成品库存 -->
    <template v-if="tab === 'material' || tab === 'goods'">
      <div class="toolbar">
        <el-input
          v-model="itemFilters.q"
          clearable
          :placeholder="tab === 'material' ? '材料名称、代码、规格' : '成品名称、代码、规格'"
          class="search"
          data-testid="warehouse-item-search"
          @keyup.enter="searchItems"
          @clear="searchItems"
        />
        <el-checkbox v-model="itemFilters.low" data-testid="warehouse-low-only">只看库存不足</el-checkbox>
        <span class="spacer" />
        <template v-if="tab === 'material'">
          <el-button :loading="exporting" @click="exportItems">导出</el-button>
          <el-button v-if="canMaterial" data-testid="warehouse-import" @click="importing = true">导入 Excel</el-button>
          <el-button v-if="canMaterial" data-testid="warehouse-new-material" @click="newMaterial">新建材料</el-button>
          <el-button type="primary" data-testid="warehouse-new-requisition" @click="newDocument('requisition')"
            >开领料单</el-button
          >
        </template>
        <el-button v-else type="primary" data-testid="warehouse-new-receipt" @click="newDocument('receipt')"
          >开入库单</el-button
        >
      </div>
      <el-table
        v-loading="itemLoading"
        :data="items"
        row-key="id"
        :empty-text="tab === 'material' ? '还没有材料：新建或用 Excel 导入' : '还没有成品'"
        data-testid="warehouse-items"
      >
        <el-table-column :label="tab === 'material' ? '材料' : '成品'" min-width="200">
          <template #default="{ row }">
            <div class="name" data-testid="warehouse-item" :data-name="row.name">
              {{ row.name }}
              <el-tag v-if="row.ready_made" size="small" type="success" effect="plain">现货</el-tag>
              <el-tag v-if="row.status === 'off'" size="small" type="info">{{
                tab === 'material' ? '停用' : '下架'
              }}</el-tag>
            </div>
            <div class="muted">{{ [row.code, row.model, row.spec].filter(Boolean).join(' · ') }}</div>
          </template>
        </el-table-column>
        <el-table-column prop="category" label="分类" width="130" />
        <el-table-column label="单位" width="70">
          <template #default="{ row }">{{ row.unit || '—' }}</template>
        </el-table-column>
        <el-table-column label="现有" width="100" align="right">
          <template #default="{ row }">
            <span data-testid="warehouse-item-stock">{{ qty(row.stock) }}</span>
          </template>
        </el-table-column>
        <el-table-column :label="tab === 'material' ? '待领' : '占用'" width="90" align="right">
          <template #default="{ row }">{{ row.stock === null ? '' : qty(row.stock_reserved) }}</template>
        </el-table-column>
        <el-table-column label="可用" width="130">
          <template #default="{ row }">
            <template v-if="row.stock !== null">
              <b :class="{ low: row.stock_low }" data-testid="warehouse-item-available">{{
                qty(row.stock_available)
              }}</b>
              <el-tag v-if="row.stock_low" size="small" type="danger" class="gap" data-testid="warehouse-item-low"
                >不足</el-tag
              >
            </template>
            <span v-else class="muted">不管理</span>
          </template>
        </el-table-column>
        <el-table-column label="预警" width="80" align="right">
          <template #default="{ row }">{{ row.stock_alert === null ? '' : qty(row.stock_alert) }}</template>
        </el-table-column>
        <el-table-column v-if="tab === 'goods'" label="配方" width="90">
          <template #default="{ row }">
            <span v-if="row.ready_made" class="muted" title="现货不需要加工">—</span>
            <el-button
              v-else
              link
              :type="row.materials ? 'primary' : 'info'"
              size="small"
              data-testid="warehouse-bom"
              @click="Object.assign(bom, { open: true, item: row })"
              >{{ row.materials ? `${row.materials} 种材料` : canBom ? '设置' : '无' }}</el-button
            >
          </template>
        </el-table-column>
        <el-table-column label="" :width="tab === 'material' && canMaterial ? 240 : 150" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" size="small" data-testid="warehouse-adjust" @click="Object.assign(stocking, { open: true, item: row })"
              >库存</el-button
            >
            <el-button link size="small" data-testid="warehouse-history" @click="Object.assign(history, { open: true, item: row })"
              >记录</el-button
            >
            <template v-if="tab === 'material' && canMaterial">
              <el-button link type="primary" size="small" data-testid="warehouse-edit-material" @click="editMaterial(row)"
                >修改</el-button
              >
              <el-button link type="danger" size="small" @click="removeMaterial(row)">删除</el-button>
            </template>
            <el-button
              link
              size="small"
              data-testid="warehouse-versions"
              @click="
                Object.assign(versions, {
                  open: true,
                  id: row.id,
                  kind: row.kind,
                  title: `${row.kind === 'material' ? '材料' : '成品'} ${row.name}`,
                })
              "
              >历史</el-button
            >
          </template>
        </el-table-column>
      </el-table>
      <div v-if="itemTotal > PAGE_SIZE" class="page-footer">
        <el-pagination
          v-model:current-page="itemPage"
          :page-size="PAGE_SIZE"
          :total="itemTotal"
          layout="total, prev, pager, next"
          @current-change="loadItems"
        />
      </div>
    </template>

    <!-- 领料单、入库单 -->
    <template v-else-if="tab === 'requisition' || tab === 'receipt'">
      <div class="toolbar">
        <el-radio-group v-model="docFilters.status" size="small" data-testid="warehouse-doc-status">
          <el-radio-button v-for="[value, label] in DOCUMENT_FILTERS" :key="value" :value="value">{{
            label
          }}</el-radio-button>
        </el-radio-group>
        <el-input
          v-model="docFilters.q"
          clearable
          placeholder="单号、订单号或名称"
          class="search"
          data-testid="warehouse-doc-search"
          @keyup.enter="searchDocs"
          @clear="searchDocs"
        />
        <el-checkbox v-model="docFilters.mine">只看我开的</el-checkbox>
        <span class="spacer" />
        <el-button type="primary" data-testid="warehouse-new-document" @click="newDocument(docKind)"
          >开{{ KIND_LABEL[docKind] }}</el-button
        >
      </div>
      <el-table
        v-loading="docLoading"
        :data="docs"
        row-key="id"
        :empty-text="`还没有${KIND_LABEL[docKind]}`"
        data-testid="warehouse-documents"
      >
        <el-table-column label="单号" width="170">
          <template #default="{ row }">
            <el-button link type="primary" data-testid="warehouse-document" :data-no="row.no" @click="openDoc(row.id)">{{
              row.no
            }}</el-button>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="90">
          <template #default="{ row }">
            <el-tag size="small" :type="STATUS_TAG[row.status as DocumentStatus]" data-testid="warehouse-document-status">{{
              row.status_label
            }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="订单" width="150">
          <template #default="{ row }">{{ row.order_no ?? '—' }}</template>
        </el-table-column>
        <el-table-column label="内容" min-width="220">
          <template #default="{ row }">
            <span>{{ linesText(row) }}</span>
            <div v-if="row.short.length" class="warn">库存不够：{{ row.short.join('、') }}</div>
            <div v-if="row.status === 'rejected'" class="warn">退回：{{ row.reject_reason }}</div>
          </template>
        </el-table-column>
        <el-table-column label="开单" width="170">
          <template #default="{ row }">
            <div>{{ row.created_by_name ?? '—' }}</div>
            <div class="muted">{{ formatDateTime(row.submitted_at) }}</div>
          </template>
        </el-table-column>
        <el-table-column label="确认" width="170">
          <template #default="{ row }">
            <template v-if="row.confirmed_at">
              <div>{{ row.confirmed_by_name ?? '—' }}</div>
              <div class="muted">{{ formatDateTime(row.confirmed_at) }}</div>
            </template>
          </template>
        </el-table-column>
        <el-table-column label="" width="80" fixed="right">
          <template #default="{ row }">
            <el-button
              link
              :type="row.can_confirm ? 'primary' : 'default'"
              size="small"
              data-testid="warehouse-document-open"
              @click="openDoc(row.id)"
              >{{ row.can_confirm ? '处理' : '查看' }}</el-button
            >
          </template>
        </el-table-column>
      </el-table>
      <div v-if="docTotal > PAGE_SIZE" class="page-footer">
        <el-pagination
          v-model:current-page="docPage"
          :page-size="PAGE_SIZE"
          :total="docTotal"
          layout="total, prev, pager, next"
          @current-change="loadDocs"
        />
      </div>
    </template>

    <!-- 库存记录 -->
    <template v-else>
      <div class="toolbar">
        <el-radio-group v-model="moveFilters.kind" size="small" data-testid="warehouse-move-kind">
          <el-radio-button value="">全部</el-radio-button>
          <el-radio-button value="material">材料</el-radio-button>
          <el-radio-button value="goods">成品</el-radio-button>
        </el-radio-group>
        <el-input
          v-model="moveFilters.q"
          clearable
          placeholder="名称或代码"
          class="search"
          @keyup.enter="searchMoves"
          @clear="searchMoves"
        />
      </div>
      <el-table v-loading="moveLoading" :data="moves" size="small" empty-text="还没有库存记录" data-testid="warehouse-movements">
        <el-table-column label="时间" width="160">
          <template #default="{ row }">{{ formatDateTime(row.created_at) }}</template>
        </el-table-column>
        <el-table-column label="商品" min-width="160">
          <template #default="{ row }">
            <span data-testid="warehouse-movement-product">{{ row.product_name }}</span>
            <span class="muted"> {{ ITEM_KIND_LABEL[row.product_kind as ItemKind] }}</span>
          </template>
        </el-table-column>
        <el-table-column label="类型" width="100">
          <template #default="{ row }">
            <span data-testid="warehouse-movement-kind">{{ row.kind_label }}</span>
          </template>
        </el-table-column>
        <el-table-column label="变化" width="100" align="right">
          <template #default="{ row }">
            <span :class="row.delta < 0 ? 'minus' : row.delta > 0 ? 'plus' : ''">{{ deltaText(row.delta) }}</span>
            <span class="muted"> {{ row.unit }}</span>
          </template>
        </el-table-column>
        <el-table-column label="库存" width="120">
          <template #default="{ row }">{{ changeText(row) }}</template>
        </el-table-column>
        <el-table-column label="单据 / 订单" min-width="170">
          <template #default="{ row }">
            <el-button v-if="row.document_id" link type="primary" size="small" data-testid="warehouse-movement-document" @click="openDoc(row.document_id)">{{
              row.document_no
            }}</el-button>
            <span v-if="row.order_no" class="muted"> 订单 {{ row.order_no }}</span>
            <span v-if="!row.document_id && !row.order_no" class="muted">{{ row.note }}</span>
          </template>
        </el-table-column>
        <el-table-column label="操作人" width="100">
          <template #default="{ row }">{{ row.actor_name ?? '系统' }}</template>
        </el-table-column>
      </el-table>
      <div v-if="moveTotal > PAGE_SIZE" class="page-footer">
        <el-pagination
          v-model:current-page="movePage"
          :page-size="PAGE_SIZE"
          :total="moveTotal"
          layout="total, prev, pager, next"
          @current-change="loadMoves"
        />
      </div>
    </template>

    <StockDialog v-model="stocking.open" :product="stocking.item" @saved="refresh" />
    <StockHistory v-model="history.open" :product="history.item" />
    <HistoryDrawer v-model="versions.open" :record-type="versions.kind" :record-id="versions.id" :title="versions.title" />
    <BomDialog v-model="bom.open" :product="bom.item" :editable="canBom" @saved="onBomSaved" />
    <ProductDialog v-model="editing.open" :product="editing.product" kind="material" @saved="refresh" />
    <ProductImportDialog v-model="importing" default-kind="material" @imported="refresh" />
    <DocumentEditor v-model="creating.open" :kind="creating.kind" mode="create" @saved="onCreated" />
    <DocumentDrawer v-model="drawer.open" :document-id="drawer.id" @changed="onDocChanged" />
    <KeeperDialog v-model="keeper" :settings="settings" @saved="(s) => (settings = s)" />
  </div>
</template>

<style scoped>
.keeper {
  display: flex;
  align-items: center;
  gap: 4px;
  font-size: 13px;
}

.badge {
  margin-left: 4px;
}

.toolbar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  margin-bottom: 12px;
}

.spacer {
  flex: 1;
}

.toolbar .el-button + .el-button {
  margin-left: 0;
}

.search {
  width: 220px;
}

.name {
  font-weight: 500;
  display: flex;
  align-items: center;
  gap: 6px;
}

.low,
.warn,
.minus {
  color: var(--el-color-danger);
}

.warn {
  font-size: 12px;
}

.plus {
  color: var(--el-color-success);
}

.gap {
  margin-left: 6px;
}

.muted {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

@media (max-width: 768px) {
  .page-header {
    flex-wrap: wrap;
    gap: 8px;
  }

  .search {
    width: 100%;
  }
}
</style>
