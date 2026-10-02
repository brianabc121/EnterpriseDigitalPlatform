<script setup lang="ts">
import { errorMessage } from '@edp/api-client'
import { ElMessage } from 'element-plus'
import { computed, reactive, ref, watch } from 'vue'

import { api } from '../../api'
import { isoDate, KIND_LABEL, type CategoryOptions, type EntryKind, type ProfitEntry } from '../../profit'

/**
 * 登记、修改一笔费用（支出）或其他收入（设计文档 §30.4）。类别可以选常用的，也可以自己写；日期最晚
 * 到本月最后一天；"每月固定"的可以在下个月一键登记。
 */
export interface EntryDraft {
  kind: EntryKind
  category?: string
  amount?: string
  occurred_on?: string
  note?: string
  recurring?: boolean
}

const props = defineProps<{
  modelValue: boolean
  /** 修改哪一笔；为空时是新登记。 */
  entry: ProfitEntry | null
  /** 新登记时预填的内容（登记支出 / 收入、复制）。 */
  draft: EntryDraft | null
  categories: CategoryOptions | null
}>()
const emit = defineEmits<{ 'update:modelValue': [value: boolean]; saved: [entry: ProfitEntry] }>()

const today = new Date()
const maxDate = new Date(today.getFullYear(), today.getMonth() + 1, 0)
const form = reactive({
  kind: 'expense' as EntryKind,
  category: '',
  amount: undefined as number | undefined,
  occurred_on: isoDate(today),
  note: '',
  recurring: false,
})
const saving = ref(false)

const title = computed(() =>
  props.entry ? `修改${KIND_LABEL[form.kind]}` : `登记${KIND_LABEL[form.kind]}`,
)
const options = computed(() =>
  form.kind === 'income' ? (props.categories?.income ?? []) : (props.categories?.expense ?? []),
)

watch(
  () => props.modelValue,
  (open) => {
    if (!open) return
    const source = props.entry ?? props.draft
    form.kind = source?.kind ?? 'expense'
    form.category = source?.category ?? ''
    form.amount = source?.amount ? Number(source.amount) : undefined
    form.occurred_on = source?.occurred_on ?? isoDate(today)
    form.note = source?.note ?? ''
    form.recurring = source?.recurring ?? false
  },
)

function suggest(query: string, callback: (items: { value: string }[]) => void): void {
  const q = query.trim()
  callback(options.value.filter((c) => !q || c.includes(q)).map((value) => ({ value })))
}

function disabledDate(day: Date): boolean {
  return day > maxDate
}

async function save(): Promise<void> {
  const category = form.category.trim()
  if (!category) {
    ElMessage.warning('请填写类别')
    return
  }
  if (!form.amount || form.amount <= 0) {
    ElMessage.warning('请填写金额')
    return
  }
  const body = {
    kind: form.kind,
    category,
    amount: form.amount.toFixed(2),
    occurred_on: form.occurred_on,
    note: form.note.trim(),
    recurring: form.recurring,
  }
  saving.value = true
  const { data, error } = props.entry
    ? await api.PUT('/api/v1/profit/entries/{profit_entry_id}', {
        params: { path: { profit_entry_id: props.entry.id } },
        body,
      })
    : await api.POST('/api/v1/profit/entries', { body })
  saving.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  ElMessage.success(props.entry ? '已修改' : '已登记')
  emit('saved', data)
  emit('update:modelValue', false)
}
</script>

<template>
  <el-dialog
    :model-value="modelValue"
    :title="title"
    width="min(460px, 96vw)"
    data-testid="entry-dialog"
    @update:model-value="(v: boolean) => emit('update:modelValue', v)"
  >
    <el-form label-width="80px" @submit.prevent="save">
      <el-form-item label="类型">
        <el-radio-group v-model="form.kind" data-testid="entry-kind">
          <el-radio-button value="expense">支出</el-radio-button>
          <el-radio-button value="income">收入</el-radio-button>
        </el-radio-group>
      </el-form-item>
      <el-form-item label="日期">
        <el-date-picker
          v-model="form.occurred_on"
          type="date"
          value-format="YYYY-MM-DD"
          :clearable="false"
          :disabled-date="disabledDate"
          data-testid="entry-date"
        />
      </el-form-item>
      <el-form-item label="类别">
        <el-autocomplete
          v-model="form.category"
          :fetch-suggestions="suggest"
          :maxlength="20"
          placeholder="例如 房租物业，可以自己写"
          class="wide"
          data-testid="entry-category"
        />
      </el-form-item>
      <el-form-item label="金额">
        <el-input-number
          v-model="form.amount"
          :min="0.01"
          :max="99999999.99"
          :precision="2"
          :controls="false"
          placeholder="元"
          class="wide"
          data-testid="entry-amount"
        />
      </el-form-item>
      <el-form-item label="备注">
        <el-input
          v-model="form.note"
          type="textarea"
          :rows="2"
          maxlength="200"
          show-word-limit
          data-testid="entry-note"
        />
      </el-form-item>
      <el-form-item label="">
        <el-checkbox v-model="form.recurring" data-testid="entry-recurring">
          每月固定（下个月可以一键登记）
        </el-checkbox>
      </el-form-item>
    </el-form>
    <el-alert
      v-if="form.kind === 'expense'"
      type="info"
      :closable="false"
      title="采购原材料、商品的钱不要记在这里：商品的成本价已经算在销售成本里。"
    />
    <template #footer>
      <el-button @click="emit('update:modelValue', false)">取消</el-button>
      <el-button type="primary" :loading="saving" data-testid="entry-save" @click="save">保存</el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.wide {
  width: 100%;
}
</style>
