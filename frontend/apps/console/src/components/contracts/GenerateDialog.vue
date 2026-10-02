<script setup lang="ts">
import { errorMessage, type Schemas } from '@edp/api-client'
import { ElMessage } from 'element-plus'
import { computed, reactive, ref, watch } from 'vue'

import { api } from '../../api'
import { categoryOptions, categoryPath, categoryTree, type Category, type TemplateSummary } from '../../contracts'
import { money } from '../../orders'

/**
 * AI 生成合同（§34.3）：写需求，可以选模板、客户和订单；AI 按需求、企业知识库（规章制度优先）、
 * 订单和模板起草，金额和标的清单取自订单。生成的是草稿，在编辑页修改、填写、定稿。
 * manual 时是"新建合同"：空白或者按模板（只填内置填写项），不调用 AI。
 */
const open = defineModel<boolean>({ required: true })
const props = defineProps<{
  categories: Category[]
  manual?: boolean
  /** 打开时预填（来自订单详情的"生成合同"、模板的"按模板新建"或左侧选中的分类）。 */
  initial?: { order_id?: string; customer_id?: string; category_id?: string | null; template_id?: string } | null
}>()
const emit = defineEmits<{ created: [id: string] }>()

type Customer = Schemas['CustomerOut']
type OrderRow = Schemas['OrderOut']

const REQUIREMENT_HINT =
  '例如：给华东分公司定制 200 套工服，单价 120 元，30 天交货，预付 30%，验收后付清。' +
  '写清楚标的、数量、价格、交付、付款和特别的约定，AI 会结合知识库里的公司规定起草。'

const form = reactive({
  requirement: '',
  title: '',
  category: [] as string[],
  templateId: '' as string,
  customerId: '' as string,
  orderId: '' as string,
})
const templates = ref<TemplateSummary[]>([])
const customers = ref<Customer[]>([])
const orders = ref<OrderRow[]>([])
const searching = ref(false)
const saving = ref(false)
const options = computed(() => categoryOptions(categoryTree(props.categories)))

async function loadTemplates(): Promise<void> {
  const { data } = await api.GET('/api/v1/contracts/templates', {
    params: { query: { status: 'active', limit: 200 } },
  })
  templates.value = data?.items ?? []
}

async function searchCustomers(q: string): Promise<void> {
  searching.value = true
  const { data } = await api.GET('/api/v1/customers', { params: { query: { q: q || undefined, limit: 20 } } })
  searching.value = false
  customers.value = data?.items ?? []
}

async function loadOrders(): Promise<void> {
  orders.value = []
  if (!form.customerId) return
  const { data } = await api.GET('/api/v1/orders', {
    params: { query: { customer_id: form.customerId, limit: 20 } },
  })
  orders.value = (data?.items ?? []).filter((o) => o.status !== 'cancelled')
}

async function prefillOrder(orderId: string): Promise<void> {
  const { data } = await api.GET('/api/v1/orders/{order_id}', { params: { path: { order_id: orderId } } })
  if (!data) return
  if (data.customer_id) {
    form.customerId = data.customer_id
    customers.value = [
      {
        id: data.customer_id,
        display_name: data.customer_name ?? '客户',
      } as Customer,
    ]
    await loadOrders()
  }
  form.orderId = orderId
}

watch(open, async (value) => {
  if (!value) return
  form.requirement = ''
  form.title = ''
  form.templateId = ''
  form.customerId = props.initial?.customer_id ?? ''
  form.orderId = ''
  form.category = categoryPath(props.categories, props.initial?.category_id ?? null)
  customers.value = []
  orders.value = []
  await Promise.all([loadTemplates(), searchCustomers('')])
  // 模板列表到了再选上，下拉框直接显示模板名称。
  form.templateId = props.initial?.template_id ?? ''
  if (props.initial?.order_id) await prefillOrder(props.initial.order_id)
  else if (form.customerId) await loadOrders()
})

watch(
  () => form.customerId,
  (value, old) => {
    if (value !== old && old !== undefined) {
      form.orderId = ''
      void loadOrders()
    }
  },
)

const chosenTemplate = computed(() => templates.value.find((t) => t.id === form.templateId) ?? null)

async function submit(): Promise<void> {
  if (!props.manual && form.requirement.trim().length < 4) {
    ElMessage.warning('先写下需求')
    return
  }
  saving.value = true
  const common = {
    title: form.title.trim() || null,
    category_id: form.category.at(-1) ?? null,
    template_id: form.templateId || null,
    customer_id: form.customerId || null,
    order_id: form.orderId || null,
  }
  const { data, error } = props.manual
    ? await api.POST('/api/v1/contracts', { body: common })
    : await api.POST('/api/v1/contracts/generate', {
        body: { ...common, requirement: form.requirement.trim() },
      })
  saving.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  ElMessage.success(props.manual ? '已新建合同草稿' : 'AI 已起草合同，请核对后定稿')
  open.value = false
  emit('created', data.id)
}
</script>

<template>
  <el-dialog
    v-model="open"
    :title="manual ? '新建合同' : 'AI 生成合同'"
    width="640px"
    append-to-body
    :close-on-click-modal="!saving"
    data-testid="contract-generate-dialog"
  >
    <el-form label-width="84px" :disabled="saving">
      <el-form-item v-if="!manual" label="需求" required>
        <el-input
          v-model="form.requirement"
          type="textarea"
          :rows="5"
          maxlength="4000"
          show-word-limit
          :placeholder="REQUIREMENT_HINT"
          data-testid="contract-requirement"
        />
      </el-form-item>
      <el-form-item label="模板">
        <el-select
          v-model="form.templateId"
          clearable
          filterable
          :placeholder="manual ? '不选时新建空白合同' : '不选时 AI 按常见的合同结构起草'"
          class="wide"
          data-testid="contract-template-select"
        >
          <el-option v-for="t in templates" :key="t.id" :label="t.name" :value="t.id">
            <span>{{ t.name }}</span>
            <span class="option-hint">{{ t.category_path || '未分类' }} · {{ t.field_count }} 个填写项</span>
          </el-option>
        </el-select>
        <div v-if="chosenTemplate?.description" class="hint">{{ chosenTemplate.description }}</div>
      </el-form-item>
      <el-form-item label="客户">
        <el-select
          v-model="form.customerId"
          clearable
          filterable
          remote
          :remote-method="searchCustomers"
          :loading="searching"
          placeholder="输入名称或公司搜索"
          class="wide"
          data-testid="contract-customer-select"
        >
          <el-option
            v-for="c in customers"
            :key="c.id"
            :label="c.company ? `${c.company}（${c.display_name}）` : c.display_name"
            :value="c.id"
          />
        </el-select>
      </el-form-item>
      <el-form-item label="订单">
        <el-select
          v-model="form.orderId"
          clearable
          :disabled="!form.customerId"
          :placeholder="form.customerId ? '标的清单和金额取自订单' : '先选客户'"
          class="wide"
          data-testid="contract-order-select"
        >
          <el-option v-for="o in orders" :key="o.id" :label="`${o.no} · ${money(o.total)}`" :value="o.id" />
        </el-select>
      </el-form-item>
      <el-form-item label="分类">
        <el-cascader
          v-model="form.category"
          :options="options"
          :props="{ checkStrictly: true }"
          clearable
          placeholder="不选时用模板的分类"
          class="wide"
          data-testid="contract-category-select"
        />
      </el-form-item>
      <el-form-item label="名称">
        <el-input v-model="form.title" maxlength="200" placeholder="不填时用 AI 起的名称或模板名称" data-testid="contract-title-input" />
      </el-form-item>
    </el-form>
    <el-alert
      v-if="saving && !manual"
      type="info"
      :closable="false"
      show-icon
      title="AI 正在查知识库、起草合同，大约需要十几秒……"
    />
    <template #footer>
      <el-button :disabled="saving" @click="open = false">取消</el-button>
      <el-button type="primary" :loading="saving" data-testid="contract-generate-submit" @click="submit">
        {{ manual ? '新建' : '生成' }}
      </el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.wide {
  width: 100%;
}

.hint {
  margin-top: 4px;
  font-size: 12px;
  line-height: 1.5;
  color: var(--el-text-color-secondary);
}

.option-hint {
  float: right;
  margin-left: 12px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
</style>
