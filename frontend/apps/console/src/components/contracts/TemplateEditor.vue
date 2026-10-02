<script setup lang="ts">
import { errorMessage } from '@edp/api-client'
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed, reactive, ref, watch } from 'vue'

import { api, formatDateTime } from '../../api'
import {
  categoryOptions,
  categoryPath,
  categoryTree,
  defaultValues,
  placeholders,
  STARTER_BODY,
  type Category,
  type Template,
  type TemplateField,
} from '../../contracts'
import HistoryDrawer from '../history/HistoryDrawer.vue'
import ContractPreview from './ContractPreview.vue'

/**
 * 合同模板的编辑页（§34.2、§34.5）：和合同的编辑页一样左边正文、右边预览；多一个填写项列表——正文里的
 * {{名称}}，可以写说明（AI 起草时也会参考）和默认值；内置填写项由系统填写。templateId 为 "new" 时新建。
 * 可以停用、启用，没有用过的模板可以删除。
 */
const props = defineProps<{ templateId: string | null; categories: Category[]; builtin: TemplateField[] }>()
const emit = defineEmits<{
  close: []
  changed: []
  created: [id: string]
  use: [id: string, ai: boolean]
}>()

const CUSTOM_FIELD = '__custom__'
const FIELD_HELP = '正文里的 {{名称}}：说明 AI 起草时也会参考，默认值在新建合同时直接填上。'
const SYNTAX_HELP =
  '# 合同名称，## 条款标题，### 小标题；每行一段；"- " 开头是列表；"|" 开头的几行是表格；' +
  '**加粗**；{{名称}} 是填写项。'

const template = ref<Template | null>(null)
const loading = ref(false)
const saving = ref(false)
const historyOpen = ref(false)
const bodyInput = ref<{ textarea?: HTMLTextAreaElement } | null>(null)
const form = reactive({
  name: '',
  category: [] as string[],
  description: '',
  body: '',
  fields: {} as Record<string, { hint: string; default: string }>,
})
const saved = ref('')

const creating = computed(() => props.templateId === 'new')
const open = computed({
  get: () => props.templateId !== null,
  set: (value) => {
    if (!value) emit('close')
  },
})
const editable = computed(() => creating.value || template.value?.can_edit === true)
const options = computed(() => categoryOptions(categoryTree(props.categories)))
const builtinByName = computed(() => new Map(props.builtin.map((f) => [f.name, f])))

/** 正文里的填写项（按出现的先后）。 */
const fieldRows = computed(() =>
  placeholders(form.body).map((name) => {
    const builtin = builtinByName.value.get(name)
    return {
      name,
      builtin: builtin !== undefined,
      hint: builtin?.hint ?? '',
    }
  }),
)
const fieldList = computed<TemplateField[]>(() =>
  fieldRows.value.map((row) => ({
    name: row.name,
    builtin: row.builtin,
    hint: row.builtin ? '' : (form.fields[row.name]?.hint ?? '').trim(),
    default: row.builtin ? '' : (form.fields[row.name]?.default ?? '').trim(),
  })),
)
const previewValues = computed(() => defaultValues(fieldList.value))

function state(): string {
  return JSON.stringify({
    name: form.name,
    category: form.category,
    description: form.description,
    body: form.body,
    fields: fieldList.value,
  })
}

const dirty = computed(() => (creating.value || template.value !== null) && state() !== saved.value)

/** 正文里每个自定义填写项都有说明、默认值（空的），方便 v-model。 */
function ensureFields(): void {
  for (const row of fieldRows.value) {
    if (!row.builtin && !form.fields[row.name]) form.fields[row.name] = { hint: '', default: '' }
  }
}

function fieldOf(name: string): { hint: string; default: string } {
  return form.fields[name] ?? { hint: '', default: '' }
}

function fill(t: Template | null): void {
  template.value = t
  form.name = t?.name ?? ''
  form.category = categoryPath(props.categories, t?.category_id ?? null)
  form.description = t?.description ?? ''
  form.body = t?.body ?? STARTER_BODY
  form.fields = Object.fromEntries(
    (t?.fields ?? []).filter((f) => !f.builtin).map((f) => [f.name, { hint: f.hint, default: f.default }]),
  )
  ensureFields()
  saved.value = state()
}

watch(fieldRows, ensureFields)

async function load(): Promise<void> {
  const id = props.templateId
  if (!id) return
  if (id === 'new') {
    fill(null)
    return
  }
  loading.value = true
  const { data, error } = await api.GET('/api/v1/contracts/templates/{contract_template_id}', {
    params: { path: { contract_template_id: id } },
  })
  loading.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    emit('close')
    return
  }
  fill(data)
}

watch(
  () => props.templateId,
  () => {
    template.value = null
    void load()
  },
  { immediate: true },
)

async function beforeClose(done: () => void): Promise<void> {
  if (dirty.value && editable.value) {
    try {
      await ElMessageBox.confirm('有没有保存的修改，关闭后会丢失。', '关闭', {
        confirmButtonText: '不保存，关闭',
        cancelButtonText: '继续编辑',
        type: 'warning',
      })
    } catch {
      return
    }
  }
  done()
}

async function save(): Promise<void> {
  const name = form.name.trim()
  if (!name) {
    ElMessage.warning('请填写模板名称')
    return
  }
  const body = {
    name,
    category_id: form.category.at(-1) ?? null,
    description: form.description.trim() || null,
    body: form.body,
    fields: fieldList.value,
  }
  saving.value = true
  if (creating.value) {
    const { data, error } = await api.POST('/api/v1/contracts/templates', { body })
    saving.value = false
    if (!data) {
      ElMessage.error(errorMessage(error))
      return
    }
    fill(data)
    ElMessage.success('已新建模板')
    emit('changed')
    emit('created', data.id)
    return
  }
  const id = template.value?.id
  if (!id) return
  const { data, error } = await api.PATCH('/api/v1/contracts/templates/{contract_template_id}', {
    params: { path: { contract_template_id: id } },
    body,
  })
  saving.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  fill(data)
  ElMessage.success('已保存')
  emit('changed')
}

async function toggle(): Promise<void> {
  const t = template.value
  if (!t) return
  const status = t.status === 'active' ? 'disabled' : 'active'
  saving.value = true
  const { data, error } = await api.PATCH('/api/v1/contracts/templates/{contract_template_id}', {
    params: { path: { contract_template_id: t.id } },
    body: { status },
  })
  saving.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  template.value = data
  ElMessage.success(status === 'active' ? '已启用' : '已停用，不能再用来新建合同')
  emit('changed')
}

async function remove(): Promise<void> {
  const t = template.value
  if (!t) return
  try {
    await ElMessageBox.confirm(`删除模板「${t.name}」？上传的原件一起删除。`, '删除', {
      confirmButtonText: '删除',
      cancelButtonText: '取消',
      type: 'warning',
    })
  } catch {
    return
  }
  const { error } = await api.DELETE('/api/v1/contracts/templates/{contract_template_id}', {
    params: { path: { contract_template_id: t.id } },
  })
  if (error) {
    ElMessage.error(errorMessage(error))
    return
  }
  ElMessage.success('已删除')
  saved.value = state()
  emit('changed')
  emit('close')
}

async function download(): Promise<void> {
  const t = template.value
  if (!t) return
  const { data, error } = await api.GET('/api/v1/contracts/templates/{contract_template_id}/file', {
    params: { path: { contract_template_id: t.id } },
  })
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  window.open(data.url, '_blank', 'noopener')
}

function use(ai: boolean): void {
  const t = template.value
  if (!t) return
  if (dirty.value) {
    ElMessage.warning('先保存模板')
    return
  }
  emit('use', t.id, ai)
}

function insertField(name: string): void {
  const textarea = bodyInput.value?.textarea
  const token = `{{${name}}}`
  if (!textarea) {
    form.body += token
    return
  }
  const start = textarea.selectionStart ?? form.body.length
  const end = textarea.selectionEnd ?? start
  form.body = form.body.slice(0, start) + token + form.body.slice(end)
  requestAnimationFrame(() => {
    textarea.focus()
    textarea.setSelectionRange(start + token.length, start + token.length)
  })
}

async function onInsert(command: string): Promise<void> {
  if (command !== CUSTOM_FIELD) {
    insertField(command)
    return
  }
  try {
    const result = await ElMessageBox.prompt('填写项的名称，例如"交货地点"。', '插入填写项', {
      confirmButtonText: '插入',
      cancelButtonText: '取消',
      inputPattern: /^[^{}\n]{1,40}$/,
      inputErrorMessage: '1–40 个字，不能有花括号',
    })
    insertField((result as { value: string }).value.trim())
  } catch {
    // 取消
  }
}
</script>

<template>
  <el-drawer
    v-model="open"
    direction="rtl"
    size="100%"
    :before-close="beforeClose"
    class="template-editor"
    data-testid="template-editor"
  >
    <template #header>
      <div class="head">
        <el-input
          v-if="editable"
          v-model="form.name"
          maxlength="128"
          placeholder="模板名称，例如：定制加工合同"
          class="name-input"
          data-testid="template-name"
        />
        <h3 v-else class="name">{{ template?.name }}</h3>
        <template v-if="template">
          <el-tag :type="template.status === 'active' ? 'success' : 'info'" data-testid="template-status">{{
            template.status === 'active' ? '启用' : '停用'
          }}</el-tag>
          <span class="muted">用过 {{ template.used_count }} 次 · {{ template.created_by_name ?? '—' }} 创建 ·
            {{ formatDateTime(template.updated_at) }} 更新</span>
        </template>
        <span v-if="dirty && editable && !creating" class="unsaved">有修改未保存</span>
      </div>
    </template>

    <div v-loading="loading" class="editor">
      <template v-if="creating || template">
        <section class="info">
          <el-form label-width="60px" size="small" class="info-form">
            <el-form-item label="分类">
              <el-cascader
                v-model="form.category"
                :options="options"
                :props="{ checkStrictly: true }"
                :disabled="!editable"
                clearable
                placeholder="未分类"
                class="wide"
                data-testid="template-category"
              />
            </el-form-item>
            <el-form-item label="说明">
              <el-input
                v-model="form.description"
                maxlength="1000"
                :disabled="!editable"
                placeholder="适用的场景，例如：工服、制服的定制加工"
              />
            </el-form-item>
            <el-form-item v-if="template?.file_name" label="原件">
              <el-button link type="primary" size="small" data-testid="template-file" @click="download">{{
                template.file_name
              }}</el-button>
            </el-form-item>
          </el-form>
        </section>

        <section class="fields" data-testid="template-fields">
          <div class="block-head">
            <h4>填写项</h4>
            <span class="muted">{{ FIELD_HELP }}</span>
          </div>
          <el-table :data="fieldRows" size="small" empty-text="正文里还没有填写项" class="field-table">
            <el-table-column label="名称" width="160">
              <template #default="{ row }">
                <span :data-testid="`template-field-${row.name}`">{{ row.name }}</span>
                <el-tag v-if="row.builtin" size="small" type="info" effect="plain" class="builtin">系统填写</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="说明" min-width="220">
              <template #default="{ row }">
                <span v-if="row.builtin" class="muted">{{ row.hint }}</span>
                <el-input
                  v-else
                  v-model="fieldOf(row.name).hint"
                  size="small"
                  maxlength="200"
                  :disabled="!editable"
                  placeholder="例如：写明天数，例如 30 天"
                />
              </template>
            </el-table-column>
            <el-table-column label="默认值" min-width="180">
              <template #default="{ row }">
                <span v-if="row.builtin" class="muted">—</span>
                <el-input
                  v-else
                  v-model="fieldOf(row.name).default"
                  size="small"
                  maxlength="500"
                  :disabled="!editable"
                  :data-testid="`template-default-${row.name}`"
                />
              </template>
            </el-table-column>
          </el-table>
        </section>

        <section class="body" :class="{ split: editable }">
          <div v-if="editable" class="source">
            <div class="pane-head">
              <h4>正文</h4>
              <el-dropdown trigger="click" @command="onInsert">
                <el-button size="small" data-testid="template-insert-field">插入填写项</el-button>
                <template #dropdown>
                  <el-dropdown-menu>
                    <el-dropdown-item v-for="f in builtin" :key="f.name" :command="f.name">
                      {{ f.name }} <span class="muted">· {{ f.hint }}</span>
                    </el-dropdown-item>
                    <el-dropdown-item divided :command="CUSTOM_FIELD">其他填写项…</el-dropdown-item>
                  </el-dropdown-menu>
                </template>
              </el-dropdown>
            </div>
            <el-input
              ref="bodyInput"
              v-model="form.body"
              type="textarea"
              :autosize="{ minRows: 24 }"
              class="mono"
              data-testid="template-body"
            />
            <p class="hint">{{ SYNTAX_HELP }}</p>
          </div>
          <div class="preview">
            <div class="pane-head">
              <h4>预览</h4>
              <span class="muted">填了默认值的显示默认值，其他的标红</span>
            </div>
            <ContractPreview :body="form.body" :values="previewValues" />
          </div>
        </section>
      </template>
    </div>

    <template #footer>
      <div class="actions">
        <span class="left">
          <template v-if="template">
            <el-button data-testid="template-history" @click="historyOpen = true">修改历史</el-button>
            <el-button
              v-if="template.status === 'active'"
              data-testid="template-use-ai"
              @click="use(true)"
              >AI 按模板起草</el-button
            >
            <el-button v-if="template.status === 'active'" data-testid="template-use" @click="use(false)"
              >按模板新建合同</el-button
            >
          </template>
        </span>
        <span>
          <template v-if="template && template.can_edit">
            <el-button
              v-if="template.used_count === 0"
              type="danger"
              plain
              :disabled="saving"
              data-testid="template-delete"
              @click="remove"
              >删除</el-button
            >
            <el-button :disabled="saving" data-testid="template-toggle" @click="toggle">{{
              template.status === 'active' ? '停用' : '启用'
            }}</el-button>
          </template>
          <el-button
            v-if="editable"
            type="primary"
            :loading="saving"
            :disabled="!creating && !dirty"
            data-testid="template-save"
            @click="save"
            >{{ creating ? '新建' : '保存' }}</el-button
          >
        </span>
      </div>
    </template>

    <HistoryDrawer
      v-if="template"
      v-model="historyOpen"
      record-type="contract_tpl"
      :record-id="template.id"
      :title="`合同模板 ${template.name}`"
    />
  </el-drawer>
</template>

<style scoped>
.head {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
}

.name {
  margin: 0;
  font-size: 18px;
}

.name-input {
  width: min(420px, 100%);
}

.unsaved {
  font-size: 12px;
  color: var(--el-color-warning);
}

.editor {
  min-height: 200px;
}

.info-form {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
  column-gap: 16px;
}

.info-form :deep(.el-form-item) {
  margin-bottom: 10px;
}

.wide {
  width: 100%;
}

.fields {
  margin-bottom: 12px;
  padding: 12px 16px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 8px;
}

.block-head,
.pane-head {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 8px;
}

.block-head h4,
.pane-head h4 {
  margin: 0;
  font-size: 14px;
}

.builtin {
  margin-left: 6px;
}

.body {
  display: grid;
  grid-template-columns: minmax(0, 1fr);
  gap: 16px;
}

.body.split {
  grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
}

.mono :deep(textarea) {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 13px;
  line-height: 1.7;
}

.hint {
  margin: 4px 0 0;
  font-size: 12px;
  line-height: 1.5;
  color: var(--el-text-color-secondary);
}

.muted {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.actions {
  display: flex;
  flex-wrap: wrap;
  justify-content: space-between;
  gap: 8px;
}

.actions .left {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.actions .left .el-button + .el-button {
  margin-left: 0;
}

@media (max-width: 900px) {
  .body.split {
    grid-template-columns: minmax(0, 1fr);
  }
}
</style>
