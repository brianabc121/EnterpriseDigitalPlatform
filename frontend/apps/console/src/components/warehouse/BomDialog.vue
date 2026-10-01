<script setup lang="ts">
import { errorMessage, type Schemas } from '@edp/api-client'
import { ElMessage } from 'element-plus'
import { ref, watch } from 'vue'

import { api } from '../../api'
import { qty } from '../../warehouse'

/**
 * 成品的配方（设计文档 §25.13）：每一件用多少材料（按材料的单位，可以是小数）。工人开领料单时按
 * 订单数量乘配方用量预填。有维护商品库权限的员工可以修改，其他人只能查看。
 */
const open = defineModel<boolean>({ required: true })
const props = defineProps<{
  product: { id: string; name: string; unit: string } | null
  editable: boolean
}>()
const emit = defineEmits<{ saved: [count: number] }>()

interface Row {
  material_id: string
  name: string
  spec: string
  unit: string
  quantity: number
  stock: number | null
}

const rows = ref<Row[]>([])
const loading = ref(false)
const saving = ref(false)
const options = ref<Schemas['ProductOut'][]>([])
const searching = ref(false)
const picked = ref('')

watch(open, async (value) => {
  if (!value || !props.product) return
  rows.value = []
  picked.value = ''
  loading.value = true
  const { data, error } = await api.GET('/api/v1/products/{product_id}/materials', {
    params: { path: { product_id: props.product.id } },
  })
  loading.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  rows.value = data.items.map((i) => ({
    material_id: i.material_id,
    name: i.name,
    spec: i.spec,
    unit: i.unit,
    quantity: i.quantity,
    stock: i.stock,
  }))
})

async function search(q: string): Promise<void> {
  searching.value = true
  const { data } = await api.GET('/api/v1/products', {
    params: { query: { kind: 'material', q: q.trim() || undefined, limit: 20 } },
  })
  searching.value = false
  options.value = data?.items ?? []
}

function add(id: string): void {
  picked.value = ''
  const material = options.value.find((o) => o.id === id)
  if (!material) return
  if (rows.value.some((r) => r.material_id === id)) {
    ElMessage.warning(`「${material.name}」已经在配方里了`)
    return
  }
  rows.value.push({
    material_id: material.id,
    name: material.name,
    spec: material.spec,
    unit: material.unit,
    quantity: 1,
    stock: material.stock,
  })
}

async function save(): Promise<void> {
  if (!props.product) return
  if (rows.value.some((r) => !(r.quantity > 0))) {
    ElMessage.warning('用量要大于 0（不需要的材料请删除）')
    return
  }
  saving.value = true
  const { data, error } = await api.PUT('/api/v1/products/{product_id}/materials', {
    params: { path: { product_id: props.product.id } },
    body: {
      items: rows.value.map((r) => ({
        material_id: r.material_id,
        quantity: Math.round(r.quantity * 1000) / 1000,
      })),
    },
  })
  saving.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  ElMessage.success('配方已保存')
  open.value = false
  emit('saved', data.items.length)
}
</script>

<template>
  <el-dialog
    v-model="open"
    :title="product ? `配方：${product.name}` : '配方'"
    width="min(640px, 96vw)"
    append-to-body
    data-testid="bom-dialog"
  >
    <p class="muted">每{{ product?.unit || '件' }}成品用多少材料；开领料单时按订单数量乘这里的用量预填。</p>
    <el-table v-loading="loading" :data="rows" size="small" empty-text="还没有配方" data-testid="bom-lines">
      <el-table-column label="材料" min-width="160">
        <template #default="{ row }">
          <span data-testid="bom-line" :data-name="row.name">{{ row.name }}</span>
          <span v-if="row.spec" class="muted"> {{ row.spec }}</span>
        </template>
      </el-table-column>
      <el-table-column label="用量" width="190">
        <template #default="{ row }">
          <el-input-number
            v-if="editable"
            v-model="row.quantity"
            :min="0"
            :max="100000000"
            :step="1"
            size="small"
            controls-position="right"
            class="qty"
            data-testid="bom-quantity"
          />
          <b v-else>{{ qty(row.quantity) }}</b>
          <span class="unit">{{ row.unit }}</span>
        </template>
      </el-table-column>
      <el-table-column label="现有库存" width="100" align="right">
        <template #default="{ row }">{{ qty(row.stock) }}</template>
      </el-table-column>
      <el-table-column v-if="editable" label="" width="56">
        <template #default="{ $index }">
          <el-button link type="danger" size="small" data-testid="bom-remove" @click="rows.splice($index, 1)"
            >删除</el-button
          >
        </template>
      </el-table-column>
    </el-table>
    <div v-if="editable" class="add">
      <el-select
        v-model="picked"
        filterable
        remote
        clearable
        :remote-method="search"
        :loading="searching"
        placeholder="添加材料（名称、代码或拼音首字母）"
        class="picker"
        data-testid="bom-add"
        @focus="search('')"
        @change="add"
      >
        <el-option v-for="o in options" :key="o.id" :label="o.name" :value="o.id">
          <span>{{ o.name }}</span>
          <span class="muted"> {{ o.spec }} {{ o.unit }}</span>
        </el-option>
      </el-select>
    </div>
    <template #footer>
      <el-button @click="open = false">{{ editable ? '取消' : '关闭' }}</el-button>
      <el-button v-if="editable" type="primary" :loading="saving" data-testid="bom-save" @click="save"
        >保存配方</el-button
      >
    </template>
  </el-dialog>
</template>

<style scoped>
.qty {
  width: 130px;
}

.unit {
  margin-left: 6px;
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

.add {
  margin-top: 10px;
}

.picker {
  width: min(360px, 100%);
}

.muted {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}
</style>
