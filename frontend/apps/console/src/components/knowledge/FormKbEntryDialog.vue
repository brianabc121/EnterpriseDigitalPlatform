<script setup lang="ts">
import { errorMessage, type Schemas } from '@edp/api-client'
import { ElMessage } from 'element-plus'
import { computed, reactive, ref, watch } from 'vue'

import { api } from '../../api'
import { FORM_LABEL, KIND_HINT, KIND_LABEL, type FormKbDetail, type FormKbForm, type FormKbKind } from '../../formkb'

/**
 * 手工填写表单知识（设计文档 §25.18）：叫法（输入的文字、对应的商品）、用量（成品、材料、每件用多少）、
 * 搭配（商品、常一起开的商品、表单）。手工添加的立即生效、固定；修改学到的知识后也变为固定。
 */
const props = defineProps<{ modelValue: boolean; entry: FormKbDetail | null }>()
const emit = defineEmits<{ 'update:modelValue': [value: boolean]; saved: [entry: FormKbDetail] }>()

type Product = Schemas['ProductBrief']
type ItemKind = 'goods' | 'material'

const open = computed({
  get: () => props.modelValue,
  set: (value) => emit('update:modelValue', value),
})
const editing = computed(() => props.entry !== null)
const saving = ref(false)
const form = reactive({
  kind: 'alias' as FormKbKind,
  aliasKind: 'goods' as ItemKind,
  form: 'order' as FormKbForm,
  text: '',
  productId: '',
  relatedId: '',
  value: null as number | null,
})
// 每个选择框的候选（按输入联想）。
const options = reactive<{ product: Product[]; related: Product[] }>({ product: [], related: [] })
const searching = ref(false)

const productKind = computed<ItemKind>(() => {
  if (form.kind === 'alias') return form.aliasKind
  if (form.kind === 'usage') return 'goods'
  return form.form === 'requisition' ? 'material' : 'goods'
})
const relatedKind = computed<ItemKind>(() =>
  form.kind === 'usage' ? 'material' : productKind.value,
)
const title = computed(() =>
  editing.value ? `修改${KIND_LABEL[form.kind]}` : '新增表单知识',
)
const unit = computed(() => options.related.find((p) => p.id === form.relatedId)?.unit ?? '')
const per = computed(() => options.product.find((p) => p.id === form.productId)?.unit || '件')

function label(p: Product): string {
  return [p.name, p.spec].filter(Boolean).join(' ') + (p.code ? `（${p.code}）` : '')
}

async function search(which: 'product' | 'related', q: string): Promise<void> {
  searching.value = true
  const kind = which === 'product' ? productKind.value : relatedKind.value
  const { data } = await api.GET('/api/v1/form-kb/products', {
    params: { query: { kind, q, limit: 10 } },
  })
  searching.value = false
  const keep = options[which].filter((p) => p.id === (which === 'product' ? form.productId : form.relatedId))
  options[which] = [...keep, ...(data?.items ?? []).filter((p) => !keep.some((k) => k.id === p.id))]
}

watch(open, (value) => {
  if (!value) return
  const e = props.entry
  options.product = e ? [e.product] : []
  options.related = e?.related ? [e.related] : []
  Object.assign(form, {
    kind: e?.kind ?? 'alias',
    aliasKind: e?.product.kind ?? 'goods',
    form: e?.form ?? 'order',
    text: e ? e.label || e.text : '',
    productId: e?.product.id ?? '',
    relatedId: e?.related?.id ?? '',
    value: e?.value ?? null,
  })
})

// 换了类型或表单：已经选的商品类别可能不对，清空。
watch([() => form.kind, () => form.form, () => form.aliasKind], () => {
  if (editing.value) return
  form.productId = ''
  form.relatedId = ''
  options.product = []
  options.related = []
})

async function save(): Promise<void> {
  if (!form.productId) {
    ElMessage.warning(form.kind === 'usage' ? '请选择成品' : '请选择商品')
    return
  }
  if (form.kind === 'alias' && form.text.trim().length < 2) {
    ElMessage.warning('叫法至少 2 个字符')
    return
  }
  if (form.kind !== 'alias' && !form.relatedId && !editing.value) {
    ElMessage.warning(form.kind === 'usage' ? '请选择材料' : '请选择常一起开的商品')
    return
  }
  if (form.kind === 'usage' && !(form.value && form.value > 0)) {
    ElMessage.warning('请填写每件用多少')
    return
  }
  saving.value = true
  const result = props.entry
    ? await api.PUT('/api/v1/form-kb/entries/{entry_id}', {
        params: { path: { entry_id: props.entry.id } },
        body:
          form.kind === 'alias'
            ? { text: form.text.trim(), product_id: form.productId }
            : { value: form.value },
      })
    : await api.POST('/api/v1/form-kb/entries', {
        body: {
          kind: form.kind,
          form: form.kind === 'companion' ? form.form : null,
          text: form.kind === 'alias' ? form.text.trim() : null,
          product_id: form.productId,
          related_id: form.kind === 'alias' ? null : form.relatedId,
          value: form.kind === 'usage' ? form.value : null,
        },
      })
  saving.value = false
  if (!result.data) {
    ElMessage.error(errorMessage(result.error))
    return
  }
  ElMessage.success(props.entry ? '已修改（固定，学习不再改动）' : '已添加，立即生效')
  emit('saved', result.data)
  open.value = false
}
</script>

<template>
  <el-dialog v-model="open" :title="title" width="560px" append-to-body data-testid="formkb-dialog">
    <el-form label-width="96px" @submit.prevent>
      <el-form-item v-if="!editing" label="类型">
        <el-radio-group v-model="form.kind" data-testid="formkb-dialog-kind">
          <el-radio-button v-for="(text, value) in KIND_LABEL" :key="value" :value="value">
            {{ text }}
          </el-radio-button>
        </el-radio-group>
        <div class="hint">{{ KIND_HINT[form.kind] }}</div>
      </el-form-item>

      <template v-if="form.kind === 'alias'">
        <el-form-item label="输入的文字">
          <el-input
            v-model="form.text"
            maxlength="32"
            placeholder="例如：大窗（员工或客户常用的叫法）"
            data-testid="formkb-dialog-text"
          />
        </el-form-item>
        <el-form-item v-if="!editing" label="商品类别">
          <el-radio-group v-model="form.aliasKind">
            <el-radio value="goods">成品（下单、入库单）</el-radio>
            <el-radio value="material">材料（领料单）</el-radio>
          </el-radio-group>
        </el-form-item>
      </template>
      <el-form-item v-if="form.kind === 'companion' && !editing" label="表单">
        <el-select v-model="form.form" data-testid="formkb-dialog-form">
          <el-option v-for="(text, value) in FORM_LABEL" :key="value" :label="text" :value="value" />
        </el-select>
      </el-form-item>

      <el-form-item :label="form.kind === 'usage' ? '成品' : form.kind === 'companion' ? '开这个商品时' : '对应的商品'">
        <el-select
          v-model="form.productId"
          filterable
          remote
          :remote-method="(q: string) => search('product', q)"
          :loading="searching"
          :disabled="editing && form.kind !== 'alias'"
          placeholder="输入名称、代码或拼音首字母"
          data-testid="formkb-dialog-product"
          @focus="search('product', '')"
        >
          <el-option v-for="p in options.product" :key="p.id" :label="label(p)" :value="p.id" />
        </el-select>
      </el-form-item>
      <el-form-item v-if="form.kind !== 'alias'" :label="form.kind === 'usage' ? '材料' : '常一起开'">
        <el-select
          v-model="form.relatedId"
          filterable
          remote
          :remote-method="(q: string) => search('related', q)"
          :loading="searching"
          :disabled="editing"
          placeholder="输入名称、代码或拼音首字母"
          data-testid="formkb-dialog-related"
          @focus="search('related', '')"
        >
          <el-option v-for="p in options.related" :key="p.id" :label="label(p)" :value="p.id" />
        </el-select>
      </el-form-item>
      <el-form-item v-if="form.kind === 'usage'" :label="`每${per}用`">
        <el-input-number
          v-model="form.value"
          :min="0.001"
          :precision="3"
          :step="1"
          controls-position="right"
          data-testid="formkb-dialog-value"
        />
        <span class="unit">{{ unit }}</span>
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="open = false">取消</el-button>
      <el-button type="primary" :loading="saving" data-testid="formkb-dialog-save" @click="save">
        {{ editing ? '保存（固定）' : '添加（立即生效）' }}
      </el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.hint {
  width: 100%;
  margin-top: 4px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.unit {
  margin-left: 8px;
  color: var(--el-text-color-secondary);
}

.el-select {
  width: 100%;
}
</style>
