<script setup lang="ts">
import { errorMessage, type Schemas } from '@edp/api-client'
import { ElMessage } from 'element-plus'
import { computed, reactive, ref, watch } from 'vue'

import { api } from '../../api'
import type { Opportunity } from '../../opportunities'
import { useAuthStore } from '../../stores/auth'

/**
 * 安排下一步（设计文档 §40.7）：建一条关联这条商机的待办，类型默认"回电 / 回访"（可选报价等），处理人默认是
 * 负责人，到期提醒走待办自己的机制；可以同时改商机的下次跟进日期。
 */
const open = defineModel<boolean>({ required: true })
const props = defineProps<{ opportunity: Opportunity | null }>()
const emit = defineEmits<{ done: [opportunity: Opportunity] }>()

const auth = useAuthStore()
const types = ref<Schemas['TodoTypeOut'][]>([])
const staff = ref<Schemas['StaffOut'][]>([])
const form = reactive({
  typeCode: 'callback',
  title: '',
  detail: '',
  dueAt: '',
  assigneeId: '',
  nextFollowAt: '',
})
const saving = ref(false)

const canPickAssignee = computed(() => auth.can('staff:read'))
const typeName = computed(() => types.value.find((t) => t.code === form.typeCode)?.name ?? '回电 / 回访')
const titlePlaceholder = computed(() =>
  props.opportunity ? `${typeName.value}：${props.opportunity.name}` : '',
)

async function load(): Promise<void> {
  Object.assign(form, {
    typeCode: 'callback',
    title: '',
    detail: '',
    dueAt: '',
    assigneeId: props.opportunity?.owner_id ?? '',
    nextFollowAt: '',
  })
  const [typeList, staffList] = await Promise.all([
    api.GET('/api/v1/todo-types'),
    canPickAssignee.value && staff.value.length === 0
      ? api.GET('/api/v1/staff')
      : Promise.resolve(null),
  ])
  types.value = (typeList.data?.items ?? []).filter((t) => !t.system)
  if (types.value.length && !types.value.some((t) => t.code === form.typeCode)) {
    form.typeCode = types.value[0]?.code ?? 'callback'
  }
  if (staffList?.data) staff.value = staffList.data.items.filter((s) => s.status === 'active')
}

async function save(): Promise<void> {
  if (!props.opportunity) return
  saving.value = true
  const { data, error } = await api.POST('/api/v1/opportunities/{opportunity_id}/todos', {
    params: { path: { opportunity_id: props.opportunity.id } },
    body: {
      type_code: form.typeCode,
      title: form.title.trim() || null,
      detail: form.detail.trim(),
      due_at: form.dueAt || null,
      assignee_id: form.assigneeId || null,
      next_follow_at: form.nextFollowAt || null,
    },
  })
  saving.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  ElMessage.success('已安排下一步')
  open.value = false
  emit('done', data)
}

watch(open, (value) => {
  if (value) void load()
})
</script>

<template>
  <el-dialog v-model="open" title="安排下一步" width="520px" append-to-body data-testid="opp-next-step">
    <el-form label-width="84px" @submit.prevent="save">
      <el-form-item label="类型">
        <el-select v-model="form.typeCode" data-testid="opp-step-type">
          <el-option v-for="t in types" :key="t.code" :label="t.name" :value="t.code" />
          <el-option v-if="types.length === 0" label="回电 / 回访" value="callback" />
        </el-select>
      </el-form-item>
      <el-form-item label="标题">
        <el-input
          v-model="form.title"
          maxlength="100"
          :placeholder="titlePlaceholder"
          data-testid="opp-step-title"
        />
      </el-form-item>
      <el-form-item label="说明">
        <el-input
          v-model="form.detail"
          type="textarea"
          :rows="2"
          maxlength="4000"
          placeholder="要做什么、客户的要求"
          data-testid="opp-step-detail"
        />
      </el-form-item>
      <el-form-item label="截止时间">
        <el-date-picker
          v-model="form.dueAt"
          type="datetime"
          value-format="YYYY-MM-DDTHH:mm:ssZ"
          placeholder="不填按类型的时限"
          data-testid="opp-step-due"
        />
      </el-form-item>
      <el-form-item label="处理人">
        <el-select
          v-if="canPickAssignee"
          v-model="form.assigneeId"
          clearable
          placeholder="默认负责人"
          data-testid="opp-step-assignee"
        >
          <el-option v-for="s in staff" :key="s.id" :label="s.display_name" :value="s.id" />
        </el-select>
        <span v-else>{{ opportunity?.owner_name ?? '负责人' }}</span>
      </el-form-item>
      <el-form-item label="下次跟进">
        <el-date-picker
          v-model="form.nextFollowAt"
          type="date"
          value-format="YYYY-MM-DD"
          placeholder="同时改商机的下次跟进日期"
          data-testid="opp-step-next"
        />
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="open = false">取消</el-button>
      <el-button type="primary" :loading="saving" data-testid="opp-step-save" @click="save">
        建待办
      </el-button>
    </template>
  </el-dialog>
</template>
