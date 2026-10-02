<script setup lang="ts">
import { errorMessage, type Schemas } from '@edp/api-client'
import { ElMessage } from 'element-plus'
import { computed, reactive, ref, watch } from 'vue'

import { api } from '../../api'
import { useAuthStore } from '../../stores/auth'
import { REMIND_OPTIONS, TASK_PRIORITY, tasksChanged, type Task, type TaskPriority } from '../../tasks'

/** 新建自己的事项，或交办给别人（task:assign）。 */
const props = defineProps<{ ownerId?: string | null }>()
const visible = defineModel<boolean>({ required: true })
const emit = defineEmits<{ created: [task: Task] }>()

const auth = useAuthStore()
const canAssign = computed(() => auth.can('task:assign'))
const staff = ref<Schemas['StaffOption'][]>([])
const saving = ref(false)
const form = reactive({
  title: '',
  note: '',
  priority: 'normal' as TaskPriority,
  dueAt: '' as string,
  remind: null as number | null,
  ownerId: '' as string,
})

async function loadStaff(): Promise<void> {
  if (!canAssign.value) return
  const { data } = await api.GET('/api/v1/tasks/staff')
  staff.value = data?.items ?? []
}

watch(visible, (open) => {
  if (!open) return
  Object.assign(form, {
    title: '',
    note: '',
    priority: 'normal',
    dueAt: '',
    remind: null,
    ownerId: props.ownerId ?? '',
  })
  void loadStaff()
})

async function save(): Promise<void> {
  const title = form.title.trim()
  if (!title) {
    ElMessage.warning('请填写事项')
    return
  }
  saving.value = true
  const { data, error } = await api.POST('/api/v1/tasks', {
    body: {
      title,
      note: form.note.trim(),
      priority: form.priority,
      due_at: form.dueAt || null,
      remind_before_minutes: form.dueAt ? form.remind : null,
      owner_id: form.ownerId || null,
      link: null,
    },
  })
  saving.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  ElMessage.success(data.owner_id === auth.me?.id ? '已记下' : `已交办给 ${data.owner_name ?? ''}`)
  tasksChanged()
  emit('created', data)
  visible.value = false
}
</script>

<template>
  <el-dialog v-model="visible" title="新建事项" width="520px" destroy-on-close data-testid="task-dialog">
    <el-form label-width="90px" @submit.prevent="save">
      <el-form-item label="事项" required>
        <el-input
          v-model="form.title"
          maxlength="100"
          placeholder="一句话，例如：周五前盘点仓库"
          data-testid="task-title"
          @keyup.enter="save"
        />
      </el-form-item>
      <el-form-item v-if="canAssign" label="交办给">
        <el-select v-model="form.ownerId" clearable placeholder="自己" data-testid="task-owner">
          <el-option v-for="s in staff" :key="s.id" :label="s.name" :value="s.id" />
        </el-select>
      </el-form-item>
      <el-form-item label="截止时间">
        <el-date-picker
          v-model="form.dueAt"
          type="datetime"
          value-format="YYYY-MM-DDTHH:mm:ssZ"
          placeholder="可以不填"
          data-testid="task-due"
        />
      </el-form-item>
      <el-form-item v-if="form.dueAt" label="提醒">
        <el-select v-model="form.remind" clearable placeholder="按企业设置" data-testid="task-remind">
          <el-option v-for="[minutes, label] in REMIND_OPTIONS" :key="minutes" :label="label" :value="minutes" />
        </el-select>
      </el-form-item>
      <el-form-item label="优先级">
        <el-radio-group v-model="form.priority" data-testid="task-priority">
          <el-radio-button v-for="[value, label] in TASK_PRIORITY" :key="value" :value="value">
            {{ label }}
          </el-radio-button>
        </el-radio-group>
      </el-form-item>
      <el-form-item label="说明">
        <el-input v-model="form.note" type="textarea" :rows="3" maxlength="4000" />
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="visible = false">取消</el-button>
      <el-button type="primary" :loading="saving" data-testid="task-save" @click="save">保存</el-button>
    </template>
  </el-dialog>
</template>
