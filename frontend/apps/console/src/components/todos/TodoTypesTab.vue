<script setup lang="ts">
import { errorMessage, type Schemas } from '@edp/api-client'
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed, onMounted, reactive, ref } from 'vue'

import { api } from '../../api'
import { ruleText, slaText, type TodoType } from '../../todos'
import TodoTypeDialog from './TodoTypeDialog.vue'

/** 设置 → 待办：待办类型和待办设置（需要 todo:config）。 */
const types = ref<TodoType[]>([])
const options = ref<Schemas['AssigneeOptions']>({ staff: [], groups: [] })
const loading = ref(false)
const editing = ref<TodoType | null>(null)
const dialogOpen = ref(false)
const savingSettings = ref(false)
const settings = reactive<Schemas['TodoSettings']>({
  ai_hourly_limit: 3,
  extract_enabled: true,
  extract_min_confidence: 0.6,
  pending_remind_minutes: 120,
  digest_enabled: true,
  reopen_days: 7,
  visitor_progress: false,
})

const groups = computed(() => new Map(options.value.groups.map((g) => [g.id, g.name])))
const staff = computed(() => new Map(options.value.staff.map((s) => [s.id, s.name])))

async function load(): Promise<void> {
  loading.value = true
  const [t, o, s] = await Promise.all([
    api.GET('/api/v1/todo-types'),
    api.GET('/api/v1/todos/assignees'),
    api.GET('/api/v1/admin/todo-settings'),
  ])
  loading.value = false
  types.value = t.data?.items ?? []
  options.value = o.data ?? { staff: [], groups: [] }
  if (s.data) Object.assign(settings, s.data)
}

function edit(type: TodoType | null): void {
  editing.value = type
  dialogOpen.value = true
}

async function toggle(type: TodoType, key: 'enabled' | 'ai_enabled', value: boolean): Promise<void> {
  const { id, preset, system, created_at, updated_at, ...body } = type
  void preset
  void system
  void created_at
  void updated_at
  const { data, error } = await api.PUT('/api/v1/admin/todo-types/{type_id}', {
    params: { path: { type_id: id } },
    body: { ...body, [key]: value },
  })
  if (!data) {
    ElMessage.error(errorMessage(error))
  }
  await load()
}

async function remove(type: TodoType): Promise<void> {
  try {
    await ElMessageBox.confirm(`删除类型「${type.name}」？`, '删除', { type: 'warning' })
  } catch {
    return
  }
  const { error } = await api.DELETE('/api/v1/admin/todo-types/{type_id}', {
    params: { path: { type_id: type.id } },
  })
  if (error) {
    ElMessage.error(errorMessage(error))
    return
  }
  ElMessage.success('已删除')
  await load()
}

async function saveSettings(): Promise<void> {
  savingSettings.value = true
  const { data, error } = await api.PUT('/api/v1/admin/todo-settings', { body: { ...settings } })
  savingSettings.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  ElMessage.success('待办设置已保存')
}

onMounted(load)
</script>

<template>
  <div v-loading="loading" data-testid="todo-types">
    <div class="head">
      <span class="muted">预置类型可以修改或停用；AI 登记的待办都要人工确认后才进入待办列表。</span>
      <el-button type="primary" size="small" data-testid="new-todo-type" @click="edit(null)">新建类型</el-button>
    </div>
    <el-table :data="types" size="small" data-testid="todo-types-table">
      <el-table-column label="类型" min-width="140">
        <template #default="{ row }">
          <div>{{ row.name }}</div>
          <div class="muted">{{ row.code }}</div>
        </template>
      </el-table-column>
      <el-table-column label="由谁处理" min-width="220">
        <template #default="{ row }">
          <span class="rule">{{ ruleText(row.assign_rule, groups, staff) }}</span>
        </template>
      </el-table-column>
      <el-table-column label="完成时限" width="130">
        <template #default="{ row }">{{ slaText(row) }}</template>
      </el-table-column>
      <el-table-column label="AI 登记" width="90">
        <template #default="{ row }">
          <el-switch
            :model-value="row.ai_enabled"
            :disabled="row.system"
            size="small"
            :data-testid="`ai-switch-${row.code}`"
            @change="(v: string | number | boolean) => toggle(row, 'ai_enabled', Boolean(v))"
          />
        </template>
      </el-table-column>
      <el-table-column label="启用" width="80">
        <template #default="{ row }">
          <el-switch
            :model-value="row.enabled"
            size="small"
            @change="(v: string | number | boolean) => toggle(row, 'enabled', Boolean(v))"
          />
        </template>
      </el-table-column>
      <el-table-column width="120">
        <template #default="{ row }">
          <el-button link type="primary" size="small" :data-testid="`edit-type-${row.code}`" @click="edit(row)"
            >编辑</el-button
          >
          <el-button v-if="!row.preset" link type="danger" size="small" @click="remove(row)">删除</el-button>
        </template>
      </el-table-column>
    </el-table>

    <h3 class="settings-title">待办设置</h3>
    <el-form label-width="220px" class="settings" data-testid="todo-settings">
      <el-form-item label="每个会话每小时 AI 最多登记">
        <el-input-number v-model="settings.ai_hourly_limit" :min="1" :max="20" size="small" />
        <span class="hint">条（防止被诱导批量登记）</span>
      </el-form-item>
      <el-form-item label="会话结束后由 AI 解析待办">
        <el-switch v-model="settings.extract_enabled" />
      </el-form-item>
      <el-form-item label="解析的最低置信度">
        <el-input-number
          v-model="settings.extract_min_confidence"
          :min="0"
          :max="1"
          :step="0.05"
          :precision="2"
          size="small"
        />
      </el-form-item>
      <el-form-item label="待确认多久没处理再提醒">
        <el-input-number v-model="settings.pending_remind_minutes" :min="10" :max="1440" size="small" />
        <span class="hint">工作分钟</span>
      </el-form-item>
      <el-form-item label="每个工作日发送今日待办汇总">
        <el-switch v-model="settings.digest_enabled" />
      </el-form-item>
      <el-form-item label="完成后可以重新打开的天数">
        <el-input-number v-model="settings.reopen_days" :min="1" :max="90" size="small" />
      </el-form-item>
      <el-form-item label="访客在 Widget 查看服务进度">
        <el-switch v-model="settings.visitor_progress" data-testid="visitor-progress-switch" />
      </el-form-item>
      <el-form-item>
        <el-button type="primary" :loading="savingSettings" data-testid="save-todo-settings" @click="saveSettings"
          >保存设置</el-button
        >
      </el-form-item>
    </el-form>

    <TodoTypeDialog v-model="dialogOpen" :type="editing" @saved="load" />
  </div>
</template>

<style scoped>
.head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 12px;
}

.muted {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

.rule {
  font-size: 12px;
}

.settings-title {
  margin: 24px 0 12px;
  font-size: 15px;
}

.hint {
  margin-left: 8px;
  color: var(--el-text-color-secondary);
  font-size: 12px;
}
</style>
