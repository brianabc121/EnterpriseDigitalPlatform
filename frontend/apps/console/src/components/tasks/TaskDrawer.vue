<script setup lang="ts">
import { errorMessage } from '@edp/api-client'
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed, reactive, ref, watch } from 'vue'

import { api, formatDateTime } from '../../api'
import {
  dueState,
  PRIORITY_LABEL,
  PRIORITY_TAG,
  REMIND_OPTIONS,
  TASK_PRIORITY,
  TASK_SOURCE,
  TASK_STATUS,
  TASK_STATUS_TAG,
  tasksChanged,
  type Task,
  type TaskPriority,
} from '../../tasks'

/** 事项详情：修改、完成（可以写备注）、重新打开、取消。 */
const props = withDefaults(defineProps<{ taskId: string | null; size?: string }>(), { size: '520px' })
const emit = defineEmits<{ close: []; changed: [] }>()

const task = ref<Task | null>(null)
const loading = ref(false)
const acting = ref(false)
const editing = ref(false)
const form = reactive({
  title: '',
  note: '',
  priority: 'normal' as TaskPriority,
  dueAt: '' as string,
  remind: null as number | null,
})
const done = reactive({ open: false, note: '' })

const open = computed({
  get: () => props.taskId !== null,
  set: (value: boolean) => {
    if (!value) emit('close')
  },
})
const due = computed(() => (task.value ? dueState(task.value) : null))

async function load(): Promise<void> {
  if (!props.taskId) {
    task.value = null
    return
  }
  loading.value = true
  const { data, error } = await api.GET('/api/v1/tasks/{task_id}', {
    params: { path: { task_id: props.taskId } },
  })
  loading.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    emit('close')
    return
  }
  task.value = data
  editing.value = false
}

watch(() => props.taskId, load, { immediate: true })

function changed(updated: Task): void {
  task.value = updated
  tasksChanged()
  emit('changed')
}

function startEdit(): void {
  if (!task.value) return
  Object.assign(form, {
    title: task.value.title,
    note: task.value.note,
    priority: task.value.priority,
    dueAt: task.value.due_at ?? '',
    remind: task.value.remind_before_minutes,
  })
  editing.value = true
}

async function saveEdit(): Promise<void> {
  if (!task.value || acting.value) return
  const title = form.title.trim()
  if (!title) {
    ElMessage.warning('请填写事项')
    return
  }
  acting.value = true
  const { data, error } = await api.PATCH('/api/v1/tasks/{task_id}', {
    params: { path: { task_id: task.value.id } },
    body: {
      title,
      note: form.note.trim(),
      priority: form.priority,
      due_at: form.dueAt || null,
      remind_before_minutes: form.dueAt ? form.remind : null,
    },
  })
  acting.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  editing.value = false
  changed(data)
}

async function act(action: 'done' | 'reopen' | 'cancel', body?: { note: string | null }): Promise<void> {
  if (!task.value || acting.value) return
  acting.value = true
  const path = {
    done: '/api/v1/tasks/{task_id}/done' as const,
    reopen: '/api/v1/tasks/{task_id}/reopen' as const,
    cancel: '/api/v1/tasks/{task_id}/cancel' as const,
  }[action]
  const { data, error } = await api.POST(path, {
    params: { path: { task_id: task.value.id } },
    ...(body ? { body } : {}),
  })
  acting.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  changed(data)
}

async function finish(): Promise<void> {
  await act('done', { note: done.note.trim() || null })
  done.open = false
  done.note = ''
}

async function cancel(): Promise<void> {
  try {
    await ElMessageBox.confirm('取消后这件事不再提醒，可以重新打开。', '取消事项', {
      confirmButtonText: '取消事项',
      cancelButtonText: '先不',
      type: 'warning',
    })
  } catch {
    return
  }
  await act('cancel')
}
</script>

<template>
  <el-drawer v-model="open" :size="size" :title="task ? task.no : '事项'" data-testid="task-drawer">
    <div v-loading="loading" class="body">
      <template v-if="task">
        <div class="head">
          <el-tag :type="TASK_STATUS_TAG[task.status]" data-testid="task-status">
            {{ TASK_STATUS[task.status] }}
          </el-tag>
          <el-tag :type="PRIORITY_TAG[task.priority]" effect="plain">
            优先级 {{ PRIORITY_LABEL[task.priority] }}
          </el-tag>
          <el-tag v-if="due === 'overdue'" type="danger">已逾期</el-tag>
          <el-tag v-if="task.source !== 'self'" effect="plain">{{ TASK_SOURCE[task.source] }}</el-tag>
        </div>
        <template v-if="editing">
          <el-form label-width="80px">
            <el-form-item label="事项" required>
              <el-input v-model="form.title" maxlength="100" data-testid="task-edit-title" />
            </el-form-item>
            <el-form-item label="截止时间">
              <el-date-picker v-model="form.dueAt" type="datetime" value-format="YYYY-MM-DDTHH:mm:ssZ" />
            </el-form-item>
            <el-form-item v-if="form.dueAt" label="提醒">
              <el-select v-model="form.remind" clearable placeholder="按企业设置">
                <el-option v-for="[m, label] in REMIND_OPTIONS" :key="m" :label="label" :value="m" />
              </el-select>
            </el-form-item>
            <el-form-item label="优先级">
              <el-radio-group v-model="form.priority">
                <el-radio-button v-for="[value, label] in TASK_PRIORITY" :key="value" :value="value">
                  {{ label }}
                </el-radio-button>
              </el-radio-group>
            </el-form-item>
            <el-form-item label="说明">
              <el-input v-model="form.note" type="textarea" :rows="3" maxlength="4000" />
            </el-form-item>
          </el-form>
          <div class="actions">
            <el-button type="primary" :loading="acting" data-testid="task-edit-save" @click="saveEdit">
              保存
            </el-button>
            <el-button @click="editing = false">取消</el-button>
          </div>
        </template>
        <template v-else>
          <h3 class="title" data-testid="task-title-text">{{ task.title }}</h3>
          <p v-if="task.note" class="note">{{ task.note }}</p>
          <el-descriptions :column="1" size="small" class="facts">
            <el-descriptions-item label="负责人">{{ task.owner_name ?? '—' }}</el-descriptions-item>
            <el-descriptions-item label="截止时间">
              {{ task.due_at ? formatDateTime(task.due_at) : '无' }}
            </el-descriptions-item>
            <el-descriptions-item v-if="task.created_by_name && task.source === 'assigned'" label="交办人">
              {{ task.created_by_name }}
            </el-descriptions-item>
            <el-descriptions-item label="创建时间">{{ formatDateTime(task.created_at) }}</el-descriptions-item>
            <el-descriptions-item v-if="task.done_at" label="完成时间">
              {{ formatDateTime(task.done_at) }}
            </el-descriptions-item>
            <el-descriptions-item v-if="task.done_note" label="完成备注">{{ task.done_note }}</el-descriptions-item>
          </el-descriptions>
          <router-link v-if="task.link" :to="task.link" class="link">查看关联的页面</router-link>
          <div v-if="task.allowed.edit" class="actions">
            <template v-if="task.status === 'open'">
              <el-button type="primary" :loading="acting" data-testid="task-done" @click="done.open = true">
                完成
              </el-button>
              <el-button data-testid="task-edit" @click="startEdit">修改</el-button>
              <el-button data-testid="task-cancel" @click="cancel">取消事项</el-button>
            </template>
            <el-button v-else data-testid="task-reopen" :loading="acting" @click="act('reopen')">
              重新打开
            </el-button>
          </div>
        </template>
      </template>
    </div>
    <el-dialog v-model="done.open" title="完成事项" width="420px" append-to-body>
      <el-input v-model="done.note" type="textarea" :rows="3" maxlength="2000" placeholder="完成备注（可以不填）" />
      <template #footer>
        <el-button @click="done.open = false">取消</el-button>
        <el-button type="primary" :loading="acting" data-testid="task-done-confirm" @click="finish">
          完成
        </el-button>
      </template>
    </el-dialog>
  </el-drawer>
</template>

<style scoped>
.head {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-bottom: 12px;
}

.title {
  margin: 0 0 8px;
  font-size: 16px;
}

.note {
  white-space: pre-wrap;
  color: var(--el-text-color-regular);
}

.facts {
  margin: 12px 0;
}

.link {
  font-size: 13px;
}

.actions {
  display: flex;
  gap: 8px;
  margin-top: 16px;
}
</style>
