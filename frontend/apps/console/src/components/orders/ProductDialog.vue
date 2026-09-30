<script setup lang="ts">
import { errorMessage, type Schemas } from '@edp/api-client'
import { ElMessage } from 'element-plus'
import { computed, reactive, ref, watch } from 'vue'

import { api } from '../../api'
import type { Product } from '../../orders'
import { useAuthStore } from '../../stores/auth'

/**
 * 新建或修改商品（设计文档 §25.2）。成本价只有有查看成本价权限的员工能看到和填写，
 * 永远不会告诉客户或交给 AI；建议零售价是 AI 唯一可以告诉客户的价格。
 */
const open = defineModel<boolean>({ required: true })
const props = defineProps<{ product?: Product | null; name?: string }>()
const emit = defineEmits<{ saved: [product: Product] }>()

const auth = useAuthStore()
const viewCost = computed(() => auth.can('product:view_cost'))
const saving = ref(false)
const form = reactive({
  name: '',
  code: '',
  model: '',
  spec: '',
  category: '',
  imageUrl: '',
  retailPrice: '',
  costPrice: '',
  aliases: '',
  remark: '',
  status: 'on' as 'on' | 'off',
})

watch(open, (value) => {
  if (!value) return
  const p = props.product
  Object.assign(form, {
    name: p?.name ?? props.name ?? '',
    code: p?.code ?? '',
    model: p?.model ?? '',
    spec: p?.spec ?? '',
    category: p?.category ?? '',
    imageUrl: p?.image_url ?? '',
    retailPrice: p?.retail_price ?? '',
    costPrice: p?.cost_price ?? '',
    aliases: (p?.aliases ?? []).join('、'),
    remark: p?.remark ?? '',
    status: p?.status ?? 'on',
  })
})

function price(value: string): string | null {
  const text = value.trim().replace(/,/g, '')
  return text === '' ? null : text
}

async function save(): Promise<void> {
  if (!form.name.trim()) {
    ElMessage.warning('请填写商品名称')
    return
  }
  const body: Schemas['ProductWrite'] = {
    name: form.name.trim(),
    code: form.code.trim() || null,
    model: form.model.trim(),
    spec: form.spec.trim(),
    category: form.category.trim(),
    image_url: form.imageUrl.trim() || null,
    retail_price: price(form.retailPrice),
    remark: form.remark.trim(),
    aliases: form.aliases
      .split(/[、,，;；\n]/)
      .map((a) => a.trim())
      .filter(Boolean),
    status: form.status,
  }
  // 不传成本价时保持原值（没有权限时后端也会忽略）。
  if (viewCost.value) body.cost_price = price(form.costPrice)
  saving.value = true
  const { data, error } = props.product
    ? await api.PUT('/api/v1/products/{product_id}', {
        params: { path: { product_id: props.product.id } },
        body,
      })
    : await api.POST('/api/v1/products', { body })
  saving.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  ElMessage.success('已保存')
  open.value = false
  emit('saved', data)
}
</script>

<template>
  <el-dialog
    v-model="open"
    :title="product ? `修改商品：${product.name}` : '新建商品'"
    width="560px"
    append-to-body
    data-testid="product-dialog"
  >
    <el-form label-width="96px">
      <el-form-item label="名称" required>
        <el-input v-model="form.name" maxlength="128" data-testid="product-name" />
      </el-form-item>
      <el-form-item label="代码">
        <el-input v-model="form.code" maxlength="64" placeholder="企业的商品编码，不能重复" data-testid="product-code" />
      </el-form-item>
      <el-form-item label="型号">
        <el-input v-model="form.model" maxlength="64" />
      </el-form-item>
      <el-form-item label="规格">
        <el-input v-model="form.spec" maxlength="128" placeholder="例如：黑色 / L 码" data-testid="product-spec" />
      </el-form-item>
      <el-form-item label="分类">
        <el-input v-model="form.category" maxlength="128" placeholder="多级用 / 分隔，例如：家电/空调" />
      </el-form-item>
      <el-form-item label="图片链接">
        <el-input v-model="form.imageUrl" maxlength="1024" placeholder="https://" />
      </el-form-item>
      <el-form-item label="建议零售价">
        <el-input v-model="form.retailPrice" placeholder="AI 只会告诉客户这个价格" data-testid="product-retail">
          <template #prefix>¥</template>
        </el-input>
      </el-form-item>
      <el-form-item v-if="viewCost" label="成本价">
        <el-input v-model="form.costPrice" placeholder="内部价格，不会告诉客户" data-testid="product-cost">
          <template #prefix>¥</template>
        </el-input>
      </el-form-item>
      <el-form-item label="别名">
        <el-input v-model="form.aliases" placeholder="客户常用的俗称，多个用、分隔" data-testid="product-aliases" />
      </el-form-item>
      <el-form-item label="备注">
        <el-input v-model="form.remark" type="textarea" :rows="2" maxlength="2000" placeholder="内部备注，不给 AI 和客户" />
      </el-form-item>
      <el-form-item label="状态">
        <el-radio-group v-model="form.status">
          <el-radio value="on">上架</el-radio>
          <el-radio value="off">下架（AI 不推荐，也不能下单）</el-radio>
        </el-radio-group>
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="open = false">取消</el-button>
      <el-button type="primary" :loading="saving" data-testid="product-save" @click="save">保存</el-button>
    </template>
  </el-dialog>
</template>
