<script setup lang="ts">
import { errorMessage } from '@edp/api-client'
import { ElMessage } from 'element-plus'
import { ref, watch } from 'vue'

import { api, formatDateTime } from '../../api'
import { changeText, deltaText, type StockMovement } from '../../inventory'
import type { Product } from '../../orders'

/** 库存记录（设计文档 §25.12）：每一次变化、变化前后的数量、原因和操作人；订单出库关联订单号，
 * 领料和生产入库关联单据（§25.13）。 */
const open = defineModel<boolean>({ required: true })
const props = defineProps<{ product: Pick<Product, 'id' | 'name'> | null }>()

const PAGE_SIZE = 20
const items = ref<StockMovement[]>([])
const total = ref(0)
const page = ref(1)
const loading = ref(false)

async function load(): Promise<void> {
  if (!props.product) return
  loading.value = true
  const { data, error } = await api.GET('/api/v1/products/{product_id}/stock-movements', {
    params: {
      path: { product_id: props.product.id },
      query: { limit: PAGE_SIZE, offset: (page.value - 1) * PAGE_SIZE },
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

watch(open, (value) => {
  if (!value) return
  page.value = 1
  items.value = []
  void load()
})
</script>

<template>
  <el-drawer
    v-model="open"
    :title="product ? `库存记录：${product.name}` : '库存记录'"
    size="min(720px, 100%)"
    append-to-body
    data-testid="stock-history"
  >
    <el-table v-loading="loading" :data="items" size="small" empty-text="还没有库存记录" data-testid="stock-movements">
      <el-table-column label="时间" width="160">
        <template #default="{ row }">{{ formatDateTime(row.created_at) }}</template>
      </el-table-column>
      <el-table-column label="类型" width="110">
        <template #default="{ row }">
          <span data-testid="stock-movement-kind">{{ row.kind_label }}</span>
        </template>
      </el-table-column>
      <el-table-column label="变化" width="80" align="right">
        <template #default="{ row }">
          <span :class="row.delta < 0 ? 'minus' : row.delta > 0 ? 'plus' : ''" data-testid="stock-movement-delta">{{
            deltaText(row.delta)
          }}</span>
        </template>
      </el-table-column>
      <el-table-column label="现有库存" width="110">
        <template #default="{ row }">{{ changeText(row) }}</template>
      </el-table-column>
      <el-table-column label="说明" min-width="160">
        <template #default="{ row }">
          <span v-if="row.document_no" data-testid="stock-movement-document">{{ row.document_no }}</span>
          <span v-if="row.document_no && row.order_no"> · </span>
          <span v-if="row.order_no" data-testid="stock-movement-order">订单 {{ row.order_no }}</span>
          <span v-if="!row.order_no && !row.document_no">{{ row.note }}</span>
        </template>
      </el-table-column>
      <el-table-column label="操作人" width="100">
        <template #default="{ row }">{{ row.actor_name ?? '系统' }}</template>
      </el-table-column>
    </el-table>
    <div v-if="total > PAGE_SIZE" class="page-footer">
      <el-pagination
        v-model:current-page="page"
        :page-size="PAGE_SIZE"
        :total="total"
        layout="total, prev, pager, next"
        @current-change="load"
      />
    </div>
  </el-drawer>
</template>

<style scoped>
.plus {
  color: var(--el-color-success);
}

.minus {
  color: var(--el-color-danger);
}
</style>
