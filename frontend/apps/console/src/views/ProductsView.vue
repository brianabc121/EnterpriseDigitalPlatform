<script setup lang="ts">
import { errorMessage, type Schemas } from '@edp/api-client'
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed, onMounted, reactive, ref, watch } from 'vue'

import { api, formatDateTime } from '../api'
import ProductDialog from '../components/orders/ProductDialog.vue'
import ProductImportDialog from '../components/orders/ProductImportDialog.vue'
import { downloadBlob } from '../download'
import { money, type Product } from '../orders'
import { useAuthStore } from '../stores/auth'

/**
 * 商品库（设计文档 §25.2、§25.9）：商品列表（成本价列按权限显示）、新建和修改、Excel 模板下载与
 * 导入（先预览再确认）、导出（可以改完再导入）、导入记录，以及客户问到但商品库里没有的"商品缺口"。
 */
const PAGE_SIZE = 50
const auth = useAuthStore()
const manage = computed(() => auth.can('product:manage'))

const tab = ref<'products' | 'imports' | 'gaps'>('products')
const items = ref<Product[]>([])
const total = ref(0)
const page = ref(1)
const loading = ref(false)
const categories = ref<string[]>([])
const filters = reactive({ q: '', category: '', status: '' as 'on' | 'off' | '' })
const editing = reactive({ open: false, product: null as Product | null, name: '' })
const importing = reactive({ open: false, id: null as string | null })
const imports = ref<Schemas['ProductImportSummary'][]>([])
const gaps = ref<Schemas['ProductGapOut'][]>([])
const exporting = ref(false)
const showCost = computed(() => items.value.some((p) => p.cost_visible))

async function load(): Promise<void> {
  loading.value = true
  const { data, error } = await api.GET('/api/v1/products', {
    params: {
      query: {
        q: filters.q.trim() || undefined,
        category: filters.category || undefined,
        status: filters.status || undefined,
        limit: PAGE_SIZE,
        offset: (page.value - 1) * PAGE_SIZE,
      },
    },
  })
  loading.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  items.value = data.items
  total.value = data.total
}

async function loadCategories(): Promise<void> {
  const { data } = await api.GET('/api/v1/products/categories')
  categories.value = data?.items ?? []
}

async function loadImports(): Promise<void> {
  const { data } = await api.GET('/api/v1/products/imports')
  imports.value = data?.items ?? []
}

async function loadGaps(): Promise<void> {
  const { data } = await api.GET('/api/v1/products/gaps', { params: { query: { limit: 50 } } })
  gaps.value = data?.items ?? []
}

async function refresh(): Promise<void> {
  await Promise.all([load(), loadCategories()])
}

function create(name = ''): void {
  Object.assign(editing, { open: true, product: null, name })
}

function edit(product: Product): void {
  Object.assign(editing, { open: true, product, name: '' })
}

async function remove(product: Product): Promise<void> {
  try {
    await ElMessageBox.confirm(`删除商品「${product.name}」？已经下过单的商品不能删除，可以改为下架。`, '删除商品', {
      confirmButtonText: '删除',
      cancelButtonText: '取消',
      type: 'warning',
    })
  } catch {
    return
  }
  const { error } = await api.DELETE('/api/v1/products/{product_id}', {
    params: { path: { product_id: product.id } },
  })
  if (error) {
    ElMessage.error(errorMessage(error))
    return
  }
  ElMessage.success('已删除')
  await refresh()
}

async function exportProducts(): Promise<void> {
  exporting.value = true
  const { data, error } = await api.GET('/api/v1/products/export', {
    params: {
      query: {
        q: filters.q.trim() || undefined,
        category: filters.category || undefined,
        status: filters.status || undefined,
      },
    },
    parseAs: 'blob',
  })
  exporting.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  downloadBlob(data as Blob, `商品-${new Date().toISOString().slice(0, 10)}.xlsx`)
}

async function resolveGap(gap: Schemas['ProductGapOut']): Promise<void> {
  const { error } = await api.POST('/api/v1/products/gaps/{gap_id}/resolve', {
    params: { path: { gap_id: gap.id } },
  })
  if (error) {
    ElMessage.error(errorMessage(error))
    return
  }
  await loadGaps()
}

function openImport(id: string | null = null): void {
  Object.assign(importing, { open: true, id })
}

watch(
  () => [filters.category, filters.status],
  () => {
    page.value = 1
    void load()
  },
)
watch(tab, (value) => {
  if (value === 'imports') void loadImports()
  if (value === 'gaps') void loadGaps()
})

onMounted(refresh)
</script>

<template>
  <div>
    <div class="page-header">
      <h2>商品</h2>
      <span v-if="manage">
        <el-button :loading="exporting" data-testid="products-export" @click="exportProducts">导出</el-button>
        <el-button data-testid="products-import" @click="openImport()">导入 Excel</el-button>
        <el-button type="primary" data-testid="new-product" @click="create()">新建商品</el-button>
      </span>
    </div>

    <el-tabs v-model="tab">
      <el-tab-pane label="商品列表" name="products">
        <div class="filters">
          <el-input
            v-model="filters.q"
            clearable
            placeholder="名称、代码、型号、规格、别名"
            class="search"
            data-testid="product-search"
            @keyup.enter="load"
            @clear="load"
          />
          <el-select v-model="filters.category" clearable filterable placeholder="分类" class="filter">
            <el-option v-for="c in categories" :key="c" :label="c" :value="c" />
          </el-select>
          <el-select v-model="filters.status" clearable placeholder="状态" class="filter">
            <el-option label="上架" value="on" />
            <el-option label="下架" value="off" />
          </el-select>
        </div>
        <el-table v-loading="loading" :data="items" row-key="id" data-testid="products-table" empty-text="商品库还是空的">
          <el-table-column label="" width="56">
            <template #default="{ row }">
              <el-image v-if="row.image_url" :src="row.image_url" fit="cover" class="thumb" lazy />
            </template>
          </el-table-column>
          <el-table-column label="商品" min-width="200">
            <template #default="{ row }">
              <div class="name">{{ row.name }}</div>
              <div class="muted">{{ [row.model, row.spec].filter(Boolean).join(' · ') }}</div>
              <div v-if="row.aliases.length" class="muted">也叫：{{ row.aliases.join('、') }}</div>
            </template>
          </el-table-column>
          <el-table-column prop="code" label="代码" width="130" />
          <el-table-column prop="category" label="分类" width="140" />
          <el-table-column label="建议零售价" width="120" align="right">
            <template #default="{ row }">{{ money(row.retail_price) }}</template>
          </el-table-column>
          <el-table-column v-if="showCost" label="成本价" width="110" align="right">
            <template #default="{ row }">
              <span data-testid="product-cost-cell">{{ money(row.cost_price) }}</span>
            </template>
          </el-table-column>
          <el-table-column label="状态" width="80">
            <template #default="{ row }">
              <el-tag size="small" :type="row.status === 'on' ? 'success' : 'info'">{{
                row.status === 'on' ? '上架' : '下架'
              }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="更新" width="150">
            <template #default="{ row }">{{ formatDateTime(row.updated_at) }}</template>
          </el-table-column>
          <el-table-column v-if="manage" label="" width="110">
            <template #default="{ row }">
              <el-button link type="primary" size="small" @click="edit(row)">修改</el-button>
              <el-button link type="danger" size="small" @click="remove(row)">删除</el-button>
            </template>
          </el-table-column>
        </el-table>
        <div class="page-footer">
          <el-pagination
            v-model:current-page="page"
            :page-size="PAGE_SIZE"
            :total="total"
            layout="total, prev, pager, next"
            @current-change="load"
          />
        </div>
      </el-tab-pane>

      <el-tab-pane v-if="manage" label="导入记录" name="imports">
        <el-table :data="imports" size="small" empty-text="还没有导入过" data-testid="product-imports">
          <el-table-column prop="file_name" label="文件" min-width="180" />
          <el-table-column label="状态" width="90">
            <template #default="{ row }">{{
              { preview: '待确认', done: '已导入', cancelled: '已放弃' }[row.status as string]
            }}</template>
          </el-table-column>
          <el-table-column label="结果" min-width="200">
            <template #default="{ row }">
              <span v-if="row.status === 'done'"
                >新增 {{ row.created }}，更新 {{ row.updated }}，跳过 {{ row.skipped }}</span
              >
              <span v-else class="muted">共 {{ row.total }} 行</span>
            </template>
          </el-table-column>
          <el-table-column label="上传时间" width="160">
            <template #default="{ row }">{{ formatDateTime(row.created_at) }}</template>
          </el-table-column>
          <el-table-column label="" width="80">
            <template #default="{ row }">
              <el-button link type="primary" size="small" @click="openImport(row.id)">查看</el-button>
            </template>
          </el-table-column>
        </el-table>
      </el-tab-pane>

      <el-tab-pane v-if="manage" name="gaps">
        <template #label>商品缺口</template>
        <el-alert
          type="info"
          :closable="false"
          show-icon
          class="tip"
          title="客户问到、商品库里却找不到的商品（按说法累计次数）。补充到商品库后点“已补充”。"
        />
        <el-table :data="gaps" size="small" empty-text="暂时没有缺口" data-testid="product-gaps">
          <el-table-column prop="term" label="客户的说法" min-width="160" />
          <el-table-column prop="sample" label="原话" min-width="200" />
          <el-table-column prop="count" label="次数" width="70" align="right" />
          <el-table-column label="最近一次" width="160">
            <template #default="{ row }">{{ formatDateTime(row.last_seen_at) }}</template>
          </el-table-column>
          <el-table-column label="" width="160">
            <template #default="{ row }">
              <el-button link type="primary" size="small" @click="create(row.sample)">新建商品</el-button>
              <el-button link size="small" data-testid="gap-resolve" @click="resolveGap(row)">已补充</el-button>
            </template>
          </el-table-column>
        </el-table>
      </el-tab-pane>
    </el-tabs>

    <ProductDialog v-model="editing.open" :product="editing.product" :name="editing.name" @saved="refresh" />
    <ProductImportDialog
      v-model="importing.open"
      :import-id="importing.id"
      @imported="
        () => {
          void refresh()
          void loadImports()
        }
      "
    />
  </div>
</template>

<style scoped>
.filters {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 12px;
}

.filter {
  width: 160px;
}

.search {
  width: 240px;
}

.tip {
  margin-bottom: 12px;
}

.thumb {
  width: 40px;
  height: 40px;
  border-radius: 4px;
}

.name {
  font-weight: 500;
}

.muted {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}
</style>
