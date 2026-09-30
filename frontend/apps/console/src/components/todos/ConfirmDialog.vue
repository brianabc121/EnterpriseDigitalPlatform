<script setup lang="ts">
import { errorMessage, type Schemas } from '@edp/api-client'
import { ElMessage } from 'element-plus'
import { computed, reactive, ref, watch } from 'vue'

import { api, formatDateTime } from '../../api'
import { useAuthStore } from '../../stores/auth'
import {
  changedFields,
  fieldValues,
  missingFields,
  PRIORITY,
  todosChanged,
  type TodoDetail,
  type TodoType,
} from '../../todos'
import TodoFieldsForm from './TodoFieldsForm.vue'

/** 确认 AI 生成的待办：可以先修改类型、标题、字段、处理人和截止时间，确认后进入待办列表。 */
const props = defineProps<{ todo: TodoDetail }>()
const visible = defineModel<boolean>({ required: true })
const emit = defineEmits<{ confirmed: [] }>()

const auth = useAuthStore()
const types = ref<TodoType[]>([])
const options = ref<Schemas['AssigneeOptions']>({ staff: [], groups: [] })
const saving = ref(false)
const fields = ref<Record<string, string>>({})
const form = reactive({
  typeId: '',
  title: '',
  detail: '',
  priority: 'normal' as Schemas['Priority'],
  handler: 'keep' as 'keep' | 'me' | 'staff' | 'group',
  assigneeId: '',
  groupId: '',
  dueAt: '',
})

const current = computed(() => types.value.find((t) => t.id === form.typeId) ?? null)
const saved = computed(
  () => new Set(props.todo.fields.filter((f) => f.sensitive).map((f) => f.key)),
)
const handlerName = computed(() =>
  props.todo.assignee_name
    ? `${props.todo.assignee_name}（按规则）`
    : props.todo.skill_group_name
      ? `${props.todo.skill_group_name}（待认领）`
      : '公共待认领池',
)

watch(visible, async (open) => {
  if (!open) return
  const [t, a] = await Promise.all([
    api.GET('/api/v1/todo-types'),
    api.GET('/api/v1/todos/assignees'),
  ])
  types.value = (t.data?.items ?? []).filter((x) => x.enabled && !x.system)
  options.value = a.data ?? { staff: [], groups: [] }
  Object.assign(form, {
    typeId: props.todo.type_id,
    title: props.todo.title,
    detail: props.todo.detail,
    priority: props.todo.priority,
    handler: 'keep',
    assigneeId: '',
    groupId: '',
    dueAt: '',
  })
  const type = types.value.find((x) => x.id === props.todo.type_id)
  fields.value = fieldValues(type?.fields ?? [], props.todo.fields)
})

watch(
  () => form.typeId,
  (id, previous) => {
    if (!previous || id === previous) return
    const type = types.value.find((x) => x.id === id)
    fields.value = fieldValues(type?.fields ?? [], props.todo.fields)
  },
)

async function submit(): Promise<void> {
  const type = current.value
  if (!type) return
  const keep = form.typeId === props.todo.type_id ? saved.value : new Set<string>()
  const missing = missingFields(type.fields ?? [], fields.value, keep)
  if (missing.length) {
    ElMessage.warning(`请填写：${missing.join('、')}`)
    return
  }
  let assignee: string | null = null
  let group: string | null = null
  if (form.handler === 'me') assignee = auth.me?.id ?? null
  if (form.handler === 'staff') assignee = form.assigneeId || null
  if (form.handler === 'group') group = form.groupId || null
  saving.value = true
  const { data, error } = await api.POST('/api/v1/todos/{todo_id}/confirm', {
    params: { path: { todo_id: props.todo.id } },
    body: {
      type_id: form.typeId !== props.todo.type_id ? form.typeId : null,
      title: form.title.trim() !== props.todo.title ? form.title.trim() : null,
      detail: form.detail.trim() !== props.todo.detail ? form.detail.trim() : null,
      fields: changedFields(fields.value),
      priority: form.priority !== props.todo.priority ? form.priority : null,
      assignee_id: assignee,
      skill_group_id: group,
      due_at: form.dueAt || null,
    },
  })
  saving.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  ElMessage.success('已确认，进入待办列表')
  todosChanged()
  visible.value = false
  emit('confirmed')
}
</script>

<template>
  <el-dialog v-model="visible" title="确认待办" width="600px" append-to-body data-testid="confirm-dialog">
    <el-form label-width="96px" class="form">
      <el-form-item label="类型">
        <el-select v-model="form.typeId" data-testid="confirm-type">
          <el-option v-for="t in types" :key="t.id" :label="t.name" :value="t.id" />
        </el-select>
      </el-form-item>
      <el-form-item label="标题">
        <el-input v-model="form.title" maxlength="100" />
      </el-form-item>
      <el-form-item label="需求描述">
        <el-input v-model="form.detail" type="textarea" :rows="3" maxlength="4000" />
      </el-form-item>
      <TodoFieldsForm v-if="current" v-model="fields" :specs="current.fields ?? []" :saved="saved" />
      <el-form-item label="优先级">
        <el-radio-group v-model="form.priority">
          <el-radio-button v-for="(label, key) in PRIORITY" :key="key" :value="key">{{
            label
          }}</el-radio-button>
        </el-radio-group>
      </el-form-item>
      <el-form-item label="处理人">
        <el-radio-group v-model="form.handler" data-testid="confirm-handler">
          <el-radio value="keep">{{ handlerName }}</el-radio>
          <el-radio value="me">自己处理</el-radio>
          <el-radio value="staff">指定员工</el-radio>
          <el-radio value="group">技能组待认领</el-radio>
        </el-radio-group>
      </el-form-item>
      <el-form-item v-if="form.handler === 'staff'" label="员工">
        <el-select v-model="form.assigneeId" filterable data-testid="confirm-assignee">
          <el-option v-for="s in options.staff" :key="s.id" :label="s.name" :value="s.id" />
        </el-select>
      </el-form-item>
      <el-form-item v-if="form.handler === 'group'" label="技能组">
        <el-select v-model="form.groupId" filterable>
          <el-option v-for="g in options.groups" :key="g.id" :label="g.name" :value="g.id" />
        </el-select>
      </el-form-item>
      <el-form-item label="截止时间">
        <el-date-picker
          v-model="form.dueAt"
          type="datetime"
          value-format="YYYY-MM-DDTHH:mm:ssZ"
          :placeholder="
            todo.expected_at
              ? `客户期望 ${formatDateTime(todo.expected_at)}`
              : '不填从确认时按时限计算'
          "
        />
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="visible = false">取消</el-button>
      <el-button type="primary" :loading="saving" data-testid="confirm-submit" @click="submit">
        确认
      </el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.form :deep(.el-select),
.form :deep(.el-date-editor) {
  width: 100%;
}
</style>
