<script setup lang="ts">
import { errorMessage, type Schemas } from '@edp/api-client'
import { ElMessage } from 'element-plus'
import { computed, reactive, ref, watch } from 'vue'

import { api } from '../../api'
import { useAuthStore } from '../../stores/auth'
import { changedFields, missingFields, PRIORITY, todosChanged, type TodoType } from '../../todos'
import TodoFieldsForm from './TodoFieldsForm.vue'

/**
 * 新建待办（员工新建的、由 AI 预填后核对保存的，都直接进入待办列表）。
 * 可以粘贴客户的话让 AI 预填；在工作台或侧边栏打开时带着当前客户和会话。
 */
const props = defineProps<{
  customerId?: string | null
  customerName?: string | null
  sessionId?: string | null
  source?: 'staff' | 'copilot' | 'sidebar'
  prefill?: Schemas['TodoSuggestion'] | null
}>()
const visible = defineModel<boolean>({ required: true })
const emit = defineEmits<{ created: [todo: Schemas['TodoOut']] }>()

const auth = useAuthStore()
const canAssign = computed(() => auth.can('todo:assign'))
const types = ref<TodoType[]>([])
const options = ref<Schemas['AssigneeOptions']>({ staff: [], groups: [] })
const customers = ref<{ id: string; name: string }[]>([])
const searching = ref(false)
const saving = ref(false)
const extracting = ref(false)
const pasted = ref('')
const evidence = ref<string[]>([])
const form = reactive({
  typeId: '',
  title: '',
  detail: '',
  customerId: '',
  expectedAt: '' as string,
  dueAt: '' as string,
  priority: '' as Schemas['Priority'] | '',
  assignMode: 'rule' as 'rule' | 'me' | 'staff' | 'group',
  assigneeId: '',
  groupId: '',
})
const fields = ref<Record<string, string>>({})

const creatable = computed(() => types.value.filter((t) => t.enabled && !t.system))
const current = computed(() => types.value.find((t) => t.id === form.typeId) ?? null)

async function loadOptions(): Promise<void> {
  const [t, a] = await Promise.all([
    api.GET('/api/v1/todo-types'),
    api.GET('/api/v1/todos/assignees'),
  ])
  types.value = t.data?.items ?? []
  options.value = a.data ?? { staff: [], groups: [] }
}

function reset(): void {
  Object.assign(form, {
    typeId: '',
    title: '',
    detail: '',
    customerId: props.customerId ?? '',
    expectedAt: '',
    dueAt: '',
    priority: '',
    assignMode: 'rule',
    assigneeId: '',
    groupId: '',
  })
  fields.value = {}
  pasted.value = ''
  evidence.value = []
  customers.value = props.customerId
    ? [{ id: props.customerId, name: props.customerName ?? '当前客户' }]
    : []
}

function apply(suggestion: Schemas['TodoSuggestion']): void {
  form.typeId = suggestion.type_id
  form.title = suggestion.title
  form.detail = suggestion.detail
  form.expectedAt = suggestion.expected_at ?? ''
  fields.value = { ...suggestion.fields }
  evidence.value = [...suggestion.evidence_message_ids]
}

watch(visible, async (open) => {
  if (!open) return
  reset()
  // AI 预填的内容先填上，不等类型和处理人的选项加载完（否则先输入的内容会被覆盖）。
  if (props.prefill) apply(props.prefill)
  await loadOptions()
})

watch(
  () => form.typeId,
  (id, previous) => {
    if (previous && id !== previous) fields.value = {}
  },
)

async function searchCustomers(q: string): Promise<void> {
  searching.value = true
  const { data } = await api.GET('/api/v1/customers', {
    params: { query: { q: q.trim() || undefined, limit: 20 } },
  })
  searching.value = false
  customers.value = (data?.items ?? []).map((c) => ({ id: c.id, name: c.display_name }))
}

async function extract(): Promise<void> {
  if (!pasted.value.trim()) {
    ElMessage.warning('请粘贴客户的话')
    return
  }
  extracting.value = true
  const { data, error } = await api.POST('/api/v1/todos/extract', {
    body: { text: pasted.value.trim(), message_ids: [] },
  })
  extracting.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  const [first] = data.items
  if (!first) {
    ElMessage.info('没有识别出需要跟进的事，请手工填写')
    return
  }
  apply(first)
  ElMessage.success(`AI 已预填「${first.type_name}」，请核对后保存`)
}

async function submit(): Promise<void> {
  const type = current.value
  if (!type) {
    ElMessage.warning('请选择类型')
    return
  }
  if (!form.title.trim()) {
    ElMessage.warning('请填写标题')
    return
  }
  const missing = missingFields(type.fields ?? [], fields.value)
  if (missing.length) {
    ElMessage.warning(`请填写：${missing.join('、')}`)
    return
  }
  let assignee: string | null = null
  let group: string | null = null
  if (form.assignMode === 'me') assignee = auth.me?.id ?? null
  if (form.assignMode === 'staff') assignee = form.assigneeId || null
  if (form.assignMode === 'group') group = form.groupId || null
  saving.value = true
  const { data, error } = await api.POST('/api/v1/todos', {
    body: {
      type_id: type.id,
      title: form.title.trim(),
      detail: form.detail.trim(),
      fields: changedFields(fields.value),
      customer_id: form.customerId || null,
      session_id: props.sessionId ?? null,
      evidence_message_ids: evidence.value,
      expected_at: form.expectedAt || null,
      due_at: form.dueAt || null,
      priority: form.priority || null,
      assignee_id: assignee,
      skill_group_id: group,
      source: props.source ?? 'staff',
    },
  })
  saving.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  ElMessage.success(`已新建待办 ${data.no}`)
  todosChanged()
  visible.value = false
  emit('created', data)
}
</script>

<template>
  <el-dialog v-model="visible" title="新建待办" width="620px" data-testid="todo-form">
    <div class="prefill">
      <el-input
        v-model="pasted"
        type="textarea"
        :rows="2"
        maxlength="4000"
        placeholder="粘贴客户的话，让 AI 预填类型、标题和字段"
        data-testid="todo-paste"
      />
      <el-button :loading="extracting" data-testid="todo-prefill" @click="extract">AI 预填</el-button>
    </div>
    <el-form label-width="96px" class="form">
      <el-form-item label="类型" required>
        <el-select v-model="form.typeId" placeholder="选择类型" data-testid="todo-type">
          <el-option v-for="t in creatable" :key="t.id" :label="t.name" :value="t.id" />
        </el-select>
      </el-form-item>
      <el-form-item label="标题" required>
        <el-input v-model="form.title" maxlength="100" data-testid="todo-title" />
      </el-form-item>
      <el-form-item label="需求描述">
        <el-input
          v-model="form.detail"
          type="textarea"
          :rows="3"
          maxlength="4000"
          data-testid="todo-detail"
        />
      </el-form-item>
      <TodoFieldsForm v-if="current" v-model="fields" :specs="current.fields ?? []" />
      <el-form-item label="客户">
        <el-select
          v-model="form.customerId"
          filterable
          remote
          clearable
          :remote-method="searchCustomers"
          :loading="searching"
          :disabled="!!customerId"
          placeholder="搜索客户（可以不选）"
          data-testid="todo-customer"
        >
          <el-option v-for="c in customers" :key="c.id" :label="c.name" :value="c.id" />
        </el-select>
      </el-form-item>
      <el-form-item label="客户期望时间">
        <el-date-picker v-model="form.expectedAt" type="datetime" value-format="YYYY-MM-DDTHH:mm:ssZ" />
      </el-form-item>
      <el-form-item label="截止时间">
        <el-date-picker
          v-model="form.dueAt"
          type="datetime"
          value-format="YYYY-MM-DDTHH:mm:ssZ"
          placeholder="不填按类型的时限计算"
        />
      </el-form-item>
      <el-form-item label="优先级">
        <el-radio-group v-model="form.priority">
          <el-radio-button value="">按类型</el-radio-button>
          <el-radio-button v-for="(label, key) in PRIORITY" :key="key" :value="key">{{
            label
          }}</el-radio-button>
        </el-radio-group>
      </el-form-item>
      <el-form-item label="处理人">
        <el-radio-group v-model="form.assignMode" data-testid="todo-assign-mode">
          <el-radio value="rule">按类型的规则分派</el-radio>
          <el-radio value="me">自己处理</el-radio>
          <el-radio v-if="canAssign" value="staff">指定员工</el-radio>
          <el-radio v-if="canAssign" value="group">放进技能组待认领</el-radio>
        </el-radio-group>
      </el-form-item>
      <el-form-item v-if="form.assignMode === 'staff'" label="员工">
        <el-select v-model="form.assigneeId" filterable data-testid="todo-assignee">
          <el-option v-for="s in options.staff" :key="s.id" :label="s.name" :value="s.id" />
        </el-select>
      </el-form-item>
      <el-form-item v-if="form.assignMode === 'group'" label="技能组">
        <el-select v-model="form.groupId" filterable>
          <el-option v-for="g in options.groups" :key="g.id" :label="g.name" :value="g.id" />
        </el-select>
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="visible = false">取消</el-button>
      <el-button type="primary" :loading="saving" data-testid="todo-save" @click="submit">
        保存
      </el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.prefill {
  display: flex;
  gap: 8px;
  align-items: flex-start;
  margin-bottom: 16px;
}

.form :deep(.el-select),
.form :deep(.el-date-editor) {
  width: 100%;
}
</style>
