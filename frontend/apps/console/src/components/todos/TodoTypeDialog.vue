<script setup lang="ts">
import { errorMessage, type Schemas } from '@edp/api-client'
import { ElMessage } from 'element-plus'
import { computed, reactive, ref, watch } from 'vue'

import { api } from '../../api'
import { ASSIGN_STEPS, FIELD_TYPES, PRIORITY, type AssignStep, type TodoType } from '../../todos'

/**
 * 编辑待办类型（设计文档 §24.2）：字段、给 AI 的说明、分派规则、时限、提醒与升级、AI 登记、话术。
 * 预置类型的编码不能改；系统类型（订单审核、催收）不能开启 AI 登记。
 */
const props = defineProps<{ type: TodoType | null }>()
const visible = defineModel<boolean>({ required: true })
const emit = defineEmits<{ saved: [] }>()

interface FieldRow {
  key: string
  label: string
  type: Schemas['TodoFieldSpec']['type']
  required: boolean
  sensitive: boolean
  options: string
}

const options = ref<Schemas['AssigneeOptions']>({ staff: [], groups: [] })
const saving = ref(false)
const example = ref('')
const form = reactive({
  code: '',
  name: '',
  aiHint: '',
  examples: [] as string[],
  fields: [] as FieldRow[],
  steps: ['owner'] as AssignStep[],
  skillGroupId: '',
  staffId: '',
  groupMode: 'pool' as 'pool' | 'least_loaded',
  priority: 'normal' as Schemas['Priority'],
  responseMinutes: null as number | null,
  resolveMode: 'days' as 'days' | 'minutes' | 'expected',
  resolveValue: 1,
  remindBefore: 120,
  escalateAfter: 240,
  aiEnabled: true,
  handoff: false,
  notifySupervisor: false,
  promise: '',
  doneTemplate: '',
  enabled: true,
  sort: 100,
})

const editing = computed(() => props.type !== null)
const system = computed(() => props.type?.system ?? false)

watch(visible, async (open) => {
  if (!open) return
  const { data } = await api.GET('/api/v1/todos/assignees')
  options.value = data ?? { staff: [], groups: [] }
  const t = props.type
  const rule = t?.assign_rule
  Object.assign(form, {
    code: t?.code ?? '',
    name: t?.name ?? '',
    aiHint: t?.ai_hint ?? '',
    examples: [...(t?.examples ?? [])],
    fields: (t?.fields ?? []).map((f) => ({
      key: f.key,
      label: f.label,
      type: f.type ?? 'text',
      required: f.required ?? false,
      sensitive: f.sensitive ?? false,
      options: (f.options ?? []).join('，'),
    })),
    steps: [...(rule?.steps ?? ['owner'])],
    skillGroupId: rule?.skill_group_id ?? '',
    staffId: rule?.staff_id ?? '',
    groupMode: rule?.group_mode ?? 'pool',
    priority: t?.priority ?? 'normal',
    responseMinutes: t?.sla_response_minutes ?? null,
    resolveMode: t?.sla_resolve_days ? 'days' : t?.sla_resolve_minutes ? 'minutes' : t ? 'expected' : 'days',
    resolveValue: t?.sla_resolve_days ?? t?.sla_resolve_minutes ?? 1,
    remindBefore: t?.remind_before_minutes ?? 120,
    escalateAfter: t?.escalate_after_minutes ?? 240,
    aiEnabled: t?.ai_enabled ?? true,
    handoff: t?.handoff ?? false,
    notifySupervisor: t?.notify_supervisor ?? false,
    promise: t?.promise_text ?? '',
    doneTemplate: t?.done_template ?? '',
    enabled: t?.enabled ?? true,
    sort: t?.sort ?? 100,
  })
})

function addExample(): void {
  const text = example.value.trim()
  if (text && !form.examples.includes(text) && form.examples.length < 5) form.examples.push(text)
  example.value = ''
}

function addField(): void {
  form.fields.push({
    key: `field_${form.fields.length + 1}`,
    label: '',
    type: 'text',
    required: false,
    sensitive: false,
    options: '',
  })
}

async function submit(): Promise<void> {
  if (!form.name.trim() || !/^[a-z][a-z0-9_]{0,31}$/.test(form.code)) {
    ElMessage.warning('请填写名称，编码只能用小写字母、数字和下划线，以字母开头')
    return
  }
  const body: Schemas['TodoTypeWrite'] = {
    code: form.code,
    name: form.name.trim(),
    ai_hint: form.aiHint.trim(),
    examples: form.examples,
    fields: form.fields.map((f) => ({
      key: f.key.trim(),
      label: f.label.trim(),
      type: f.type,
      required: f.required,
      sensitive: f.sensitive,
      options:
        f.type === 'option'
          ? f.options
              .split(/[,，\n]/)
              .map((o) => o.trim())
              .filter(Boolean)
          : null,
    })),
    assign_rule: {
      steps: form.steps,
      skill_group_id: form.steps.includes('skill_group') ? form.skillGroupId || null : null,
      staff_id: form.steps.includes('staff') ? form.staffId || null : null,
      group_mode: form.groupMode,
    },
    priority: form.priority,
    sla_response_minutes: form.responseMinutes || null,
    sla_resolve_days: form.resolveMode === 'days' ? form.resolveValue : null,
    sla_resolve_minutes: form.resolveMode === 'minutes' ? form.resolveValue : null,
    remind_before_minutes: form.remindBefore,
    escalate_after_minutes: form.escalateAfter,
    ai_enabled: form.aiEnabled,
    handoff: form.handoff,
    notify_supervisor: form.notifySupervisor,
    promise_text: form.promise.trim(),
    done_template: form.doneTemplate.trim(),
    enabled: form.enabled,
    sort: form.sort,
  }
  saving.value = true
  const result = props.type
    ? await api.PUT('/api/v1/admin/todo-types/{type_id}', {
        params: { path: { type_id: props.type.id } },
        body,
      })
    : await api.POST('/api/v1/admin/todo-types', { body })
  saving.value = false
  if (!result.data) {
    ElMessage.error(errorMessage(result.error))
    return
  }
  ElMessage.success('已保存')
  visible.value = false
  emit('saved')
}
</script>

<template>
  <el-dialog
    v-model="visible"
    :title="editing ? `编辑待办类型：${type?.name}` : '新建待办类型'"
    width="760px"
    data-testid="todo-type-dialog"
  >
    <el-form label-width="120px" class="form">
      <el-divider content-position="left">基本</el-divider>
      <el-form-item label="名称" required>
        <el-input v-model="form.name" maxlength="32" data-testid="type-name" />
      </el-form-item>
      <el-form-item label="编码" required>
        <el-input v-model="form.code" maxlength="32" :disabled="type?.preset" placeholder="如 contract_send" />
      </el-form-item>
      <el-form-item label="启用">
        <el-switch v-model="form.enabled" />
        <span class="hint">停用后不能新建这类待办，已有的不受影响</span>
      </el-form-item>

      <el-divider content-position="left">AI 登记</el-divider>
      <el-form-item label="AI 登记">
        <el-switch v-model="form.aiEnabled" :disabled="system" data-testid="type-ai-enabled" />
        <span class="hint">{{
          system ? '系统类型由系统规则生成' : 'AI 生成的这类待办先进入待确认页，人工确认后才进入待办列表'
        }}</span>
      </el-form-item>
      <el-form-item label="给 AI 的说明">
        <el-input
          v-model="form.aiHint"
          type="textarea"
          :rows="2"
          maxlength="500"
          placeholder="这类事是什么、需要哪些信息"
        />
      </el-form-item>
      <el-form-item label="客户常说">
        <div class="examples">
          <el-tag v-for="(e, i) in form.examples" :key="e" closable @close="form.examples.splice(i, 1)">{{
            e
          }}</el-tag>
          <el-input
            v-if="form.examples.length < 5"
            v-model="example"
            size="small"
            class="example-input"
            placeholder="回车添加"
            @keyup.enter="addExample"
            @blur="addExample"
          />
        </div>
      </el-form-item>
      <el-form-item label="同时转人工">
        <el-switch v-model="form.handoff" />
        <span class="hint">例如投诉处理：AI 登记后立即转人工</span>
      </el-form-item>

      <el-divider content-position="left">字段</el-divider>
      <el-table :data="form.fields" size="small" class="fields" empty-text="没有字段">
        <el-table-column label="编码" width="130">
          <template #default="{ row }"><el-input v-model="row.key" size="small" /></template>
        </el-table-column>
        <el-table-column label="名称" width="120">
          <template #default="{ row }"><el-input v-model="row.label" size="small" /></template>
        </el-table-column>
        <el-table-column label="类型" width="110">
          <template #default="{ row }">
            <el-select v-model="row.type" size="small">
              <el-option v-for="[value, label] in FIELD_TYPES" :key="value" :label="label" :value="value" />
            </el-select>
          </template>
        </el-table-column>
        <el-table-column label="选项" min-width="120">
          <template #default="{ row }">
            <el-input
              v-if="row.type === 'option'"
              v-model="row.options"
              size="small"
              placeholder="用逗号分隔"
            />
          </template>
        </el-table-column>
        <el-table-column label="必填" width="56">
          <template #default="{ row }"><el-checkbox v-model="row.required" /></template>
        </el-table-column>
        <el-table-column label="敏感" width="56">
          <template #default="{ row }"><el-checkbox v-model="row.sensitive" /></template>
        </el-table-column>
        <el-table-column width="56">
          <template #default="{ $index }">
            <el-button link type="danger" size="small" @click="form.fields.splice($index, 1)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
      <el-button v-if="form.fields.length < 20" size="small" class="add" @click="addField">添加字段</el-button>

      <el-divider content-position="left">分派</el-divider>
      <el-form-item label="由谁处理">
        <el-select v-model="form.steps" multiple class="wide" data-testid="type-steps">
          <el-option v-for="[value, label] in ASSIGN_STEPS" :key="value" :label="label" :value="value" />
        </el-select>
        <div class="hint">按选择的顺序取第一个可用的；都没有时进入公共待认领池。待确认的待办按同样的规则确定确认人。</div>
      </el-form-item>
      <el-form-item v-if="form.steps.includes('skill_group')" label="技能组">
        <el-select v-model="form.skillGroupId" data-testid="type-group">
          <el-option v-for="g in options.groups" :key="g.id" :label="g.name" :value="g.id" />
        </el-select>
      </el-form-item>
      <el-form-item v-if="form.steps.includes('skill_group') || form.steps.includes('channel_group')" label="组内分派">
        <el-radio-group v-model="form.groupMode" data-testid="type-group-mode">
          <el-radio value="pool">放进组内待认领</el-radio>
          <el-radio value="least_loaded">交给未完成待办最少的组员</el-radio>
        </el-radio-group>
      </el-form-item>
      <el-form-item v-if="form.steps.includes('staff')" label="指定员工">
        <el-select v-model="form.staffId" filterable>
          <el-option v-for="s in options.staff" :key="s.id" :label="s.name" :value="s.id" />
        </el-select>
      </el-form-item>

      <el-divider content-position="left">时限与提醒（按工作时间计算）</el-divider>
      <el-form-item label="默认优先级">
        <el-radio-group v-model="form.priority">
          <el-radio-button v-for="(label, key) in PRIORITY" :key="key" :value="key">{{ label }}</el-radio-button>
        </el-radio-group>
      </el-form-item>
      <el-form-item label="完成时限">
        <el-radio-group v-model="form.resolveMode">
          <el-radio value="days">工作日</el-radio>
          <el-radio value="minutes">工作分钟</el-radio>
          <el-radio value="expected">客户期望的时间</el-radio>
        </el-radio-group>
        <el-input-number
          v-if="form.resolveMode !== 'expected'"
          v-model="form.resolveValue"
          :min="1"
          :max="form.resolveMode === 'days' ? 90 : 129600"
          size="small"
          class="number"
        />
      </el-form-item>
      <el-form-item label="响应时限（分钟）">
        <el-input-number v-model="form.responseMinutes" :min="0" :max="43200" size="small" />
        <span class="hint">多久内开始处理，0 表示不设置</span>
      </el-form-item>
      <el-form-item label="截止前提醒（分钟）">
        <el-input-number v-model="form.remindBefore" :min="0" :max="10080" size="small" />
      </el-form-item>
      <el-form-item label="逾期升级（分钟）">
        <el-input-number v-model="form.escalateAfter" :min="0" :max="43200" size="small" />
        <span class="hint">逾期超过这么多工作分钟升级给主管，0 表示不升级</span>
      </el-form-item>
      <el-form-item label="立即提醒主管">
        <el-switch v-model="form.notifySupervisor" />
        <span class="hint">新的待确认同时提醒主管（投诉等）</span>
      </el-form-item>

      <el-divider content-position="left">话术</el-divider>
      <el-form-item label="AI 登记后答复">
        <el-input v-model="form.promise" maxlength="500" placeholder="已为您记录，客服确认后会尽快为您处理。" />
      </el-form-item>
      <el-form-item label="完成通知模板">
        <el-input
          v-model="form.doneTemplate"
          type="textarea"
          :rows="2"
          maxlength="1000"
          placeholder="{result} 会替换为处理结果"
        />
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="visible = false">取消</el-button>
      <el-button type="primary" :loading="saving" data-testid="type-save" @click="submit">保存</el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.form {
  max-height: 64vh;
  overflow-y: auto;
  padding-right: 8px;
}

.hint {
  margin-left: 8px;
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

.examples {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  align-items: center;
}

.example-input {
  width: 180px;
}

.fields {
  margin-bottom: 8px;
}

.add {
  margin-left: 120px;
}

.wide {
  width: 100%;
}

.number {
  margin-left: 12px;
}
</style>
