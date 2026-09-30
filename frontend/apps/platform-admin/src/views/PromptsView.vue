<script setup lang="ts">
import { errorMessage, type Schemas } from '@edp/api-client'
import { ElMessage } from 'element-plus'
import { computed, onMounted, reactive, ref } from 'vue'

import { api } from '../api'

/**
 * 提示词版本（设计文档 §11.5）：每个场景的提示词可以保存多个版本、随时启用或改回内置模板；
 * 大模型调用记录和知识候选都记下所用的版本，便于对比效果。
 */
type Prompt = Schemas['PromptOut']

const prompts = ref<Prompt[]>([])
const loading = ref(false)
const editing = ref<Prompt | null>(null)
const saving = ref(false)
const form = reactive({ content: '', note: '', activate: true })

const dialogOpen = computed({
  get: () => editing.value !== null,
  set: (value) => {
    if (!value) editing.value = null
  },
})

function fmt(value: string): string {
  return new Date(value).toLocaleString('zh-CN', { hour12: false })
}

async function load(): Promise<void> {
  loading.value = true
  const { data, error } = await api.GET('/platform/v1/prompts')
  loading.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  prompts.value = data.items
}

function newVersion(prompt: Prompt): void {
  const active = prompt.versions.find((v) => v.active)
  form.content = active?.content ?? prompt.builtin
  form.note = ''
  form.activate = true
  editing.value = prompt
}

async function save(): Promise<void> {
  if (!editing.value) return
  saving.value = true
  const { data, error } = await api.POST('/platform/v1/prompts/{key}/versions', {
    params: { path: { key: editing.value.key } },
    body: { content: form.content, note: form.note.trim() || null, activate: form.activate },
  })
  saving.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  ElMessage.success(form.activate ? '已保存并启用，几秒内生效' : '已保存')
  editing.value = null
  await load()
}

async function activate(prompt: Prompt, version: number | null): Promise<void> {
  const { data, error } = await api.POST('/platform/v1/prompts/{key}/activate', {
    params: { path: { key: prompt.key } },
    body: { version },
  })
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  ElMessage.success(version === null ? '已改回内置模板' : `已启用 v${version}`)
  await load()
}

onMounted(load)
</script>

<template>
  <div v-loading="loading">
    <div class="page-header">
      <h2>提示词</h2>
    </div>
    <p class="sub">
      各场景的提示词模板。新版本保存后可以立即启用，也可以随时改回内置模板；大模型调用记录里记下所用的版本（如
      reply@3），便于对比效果。模板里可以使用的变量写成 {变量名}。
    </p>
    <el-card v-for="p in prompts" :key="p.key" shadow="never" class="prompt" data-testid="prompt-card">
      <template #header>
        <div class="card-head">
          <span>
            <strong>{{ p.name }}</strong>
            <code class="key">{{ p.key }}</code>
            <el-tag v-if="p.active_version" size="small" type="success" disable-transitions>
              v{{ p.active_version }}
            </el-tag>
            <el-tag v-else size="small" type="info" disable-transitions>内置模板</el-tag>
          </span>
          <span>
            <el-button
              v-if="p.active_version"
              link
              type="primary"
              @click="activate(p, null)"
            >
              改回内置
            </el-button>
            <el-button type="primary" size="small" data-testid="prompt-new-version" @click="newVersion(p)">
              新版本
            </el-button>
          </span>
        </div>
      </template>
      <div v-if="p.variables.length" class="sub">
        变量：<code v-for="v in p.variables" :key="v" class="var">{{ '{' + v + '}' }}</code>
      </div>
      <el-table v-if="p.versions.length" :data="p.versions" size="small" class="versions">
        <el-table-column label="版本" width="70">
          <template #default="{ row }">v{{ row.version }}</template>
        </el-table-column>
        <el-table-column label="说明" min-width="200">
          <template #default="{ row }">{{ row.note ?? '—' }}</template>
        </el-table-column>
        <el-table-column label="保存时间" width="170">
          <template #default="{ row }">{{ fmt(row.created_at) }}</template>
        </el-table-column>
        <el-table-column label="" width="120">
          <template #default="{ row }">
            <el-tag v-if="row.active" size="small" type="success" disable-transitions>使用中</el-tag>
            <el-button v-else link type="primary" size="small" @click="activate(p, row.version)">
              启用
            </el-button>
          </template>
        </el-table-column>
      </el-table>
      <p v-else class="sub">还没有自定义版本，正在使用内置模板。</p>
    </el-card>

    <el-dialog v-model="dialogOpen" :title="`新版本 · ${editing?.name ?? ''}`" width="720px">
      <el-form label-position="top">
        <el-form-item label="模板">
          <el-input
            v-model="form.content"
            type="textarea"
            :rows="12"
            maxlength="8000"
            show-word-limit
            data-testid="prompt-content"
          />
        </el-form-item>
        <el-form-item label="说明">
          <el-input v-model="form.note" maxlength="200" placeholder="这个版本改了什么" />
        </el-form-item>
        <el-checkbox v-model="form.activate">保存后立即启用</el-checkbox>
      </el-form>
      <template #footer>
        <el-button @click="editing = null">取消</el-button>
        <el-button type="primary" :loading="saving" data-testid="prompt-save" @click="save">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.page-header h2 {
  margin: 0 0 8px;
  font-size: 18px;
}

.sub {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  line-height: 1.6;
}

.prompt {
  margin-bottom: 12px;
}

.card-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.key {
  margin: 0 8px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.var {
  margin-right: 6px;
}

.versions {
  margin-top: 8px;
}
</style>
