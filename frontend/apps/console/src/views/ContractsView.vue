<script setup lang="ts">
import { errorMessage } from '@edp/api-client'
import { ElMessage } from 'element-plus'
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { api } from '../api'
import ContractCategoryTree from '../components/contracts/ContractCategoryTree.vue'
import ContractEditor from '../components/contracts/ContractEditor.vue'
import ContractList from '../components/contracts/ContractList.vue'
import ContractSettingsDialog from '../components/contracts/ContractSettingsDialog.vue'
import GenerateDialog from '../components/contracts/GenerateDialog.vue'
import TemplateEditor from '../components/contracts/TemplateEditor.vue'
import TemplateList from '../components/contracts/TemplateList.vue'
import TemplateUploadDialog from '../components/contracts/TemplateUploadDialog.vue'
import type { Category, ContractSettings, TemplateField } from '../contracts'
import { useAuthStore } from '../stores/auth'

/**
 * 合同（设计文档 §34.5）：左边是多层级分类，右边两个页签——合同（按状态筛选；AI 生成合同、新建合同）和
 * 模板（上传模板、新建模板）。链接可以带 id（打开这份合同，例如 AI 唤醒的提醒）、order（带上这个订单
 * 打开 AI 生成合同，来自订单详情）、tab=templates。
 */
type Tab = 'contracts' | 'templates'
interface GenerateInitial {
  order_id?: string
  customer_id?: string
  category_id?: string | null
  template_id?: string
}

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()

const tab = ref<Tab>(route.query.tab === 'templates' ? 'templates' : 'contracts')
const categories = ref<Category[]>([])
const maxDepth = ref(5)
const selected = ref<string | null>(null)
const settings = ref<ContractSettings | null>(null)
const builtin = ref<TemplateField[]>([])
const refreshKey = ref(0)
const openId = ref<string | null>(null)
const templateId = ref<string | null>(null)
const generate = ref<{ open: boolean; manual: boolean; initial: GenerateInitial | null }>({
  open: false,
  manual: false,
  initial: null,
})
const uploadOpen = ref(false)
const settingsOpen = ref(false)
const canManage = computed(() => auth.can('contract:manage'))

async function loadCategories(): Promise<void> {
  const { data, error } = await api.GET('/api/v1/contracts/categories')
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  categories.value = data.items
  maxDepth.value = data.max_depth
  if (selected.value && !data.items.some((c) => c.id === selected.value)) selected.value = null
}

async function loadSettings(): Promise<void> {
  const { data } = await api.GET('/api/v1/contracts/settings')
  if (!data) return
  settings.value = data.settings
  builtin.value = data.builtin
}

function refresh(): void {
  refreshKey.value += 1
  void loadCategories()
}

function startGenerate(manual: boolean, initial: GenerateInitial = {}): void {
  generate.value = { open: true, manual, initial: { category_id: selected.value, ...initial } }
}

function onCreated(id: string): void {
  tab.value = 'contracts'
  refresh()
  openId.value = id
}

function onUploaded(id: string): void {
  refresh()
  templateId.value = id
}

/** 模板的"AI 按模板起草""按模板新建合同"。 */
function useTemplate(id: string, ai: boolean): void {
  templateId.value = null
  startGenerate(!ai, { template_id: id })
}

/** 链接带的参数：打开合同，或者带上订单打开 AI 生成合同；用过就从地址里去掉。 */
function fromQuery(): void {
  const { id, order } = route.query
  if (typeof id === 'string' && id) {
    tab.value = 'contracts'
    openId.value = id
  }
  if (typeof order === 'string' && order) {
    tab.value = 'contracts'
    startGenerate(false, { order_id: order })
  }
  if (id || order) void router.replace({ query: { ...route.query, id: undefined, order: undefined } })
}

watch(() => [route.query.id, route.query.order], fromQuery)

onMounted(async () => {
  await Promise.all([loadCategories(), loadSettings()])
  fromQuery()
})
</script>

<template>
  <div>
    <div class="page-header">
      <h2>合同</h2>
      <span>
        <el-button v-if="canManage" data-testid="contract-settings-open" @click="settingsOpen = true"
          >合同设置</el-button
        >
        <template v-if="tab === 'contracts'">
          <el-button data-testid="contract-new" @click="startGenerate(true)">新建合同</el-button>
          <el-button type="primary" data-testid="contract-ai-generate" @click="startGenerate(false)"
            >AI 生成合同</el-button
          >
        </template>
        <template v-else>
          <el-button data-testid="template-new" @click="templateId = 'new'">新建模板</el-button>
          <el-button type="primary" data-testid="template-upload" @click="uploadOpen = true">上传模板</el-button>
        </template>
      </span>
    </div>

    <div class="layout">
      <aside class="side">
        <ContractCategoryTree
          :categories="categories"
          :max-depth="maxDepth"
          :selected="selected"
          :can-manage="canManage"
          :count="tab"
          @select="selected = $event"
          @changed="loadCategories"
        />
      </aside>
      <section class="main">
        <el-tabs v-model="tab" data-testid="contract-tabs">
          <el-tab-pane label="合同" name="contracts" />
          <el-tab-pane label="模板" name="templates" />
        </el-tabs>
        <ContractList
          v-if="tab === 'contracts'"
          :category-id="selected"
          :refresh-key="refreshKey"
          @open="openId = $event"
        />
        <TemplateList v-else :category-id="selected" :refresh-key="refreshKey" @open="templateId = $event" />
      </section>
    </div>

    <GenerateDialog
      v-model="generate.open"
      :categories="categories"
      :manual="generate.manual"
      :initial="generate.initial"
      @created="onCreated"
    />
    <ContractEditor
      :contract-id="openId"
      :categories="categories"
      :builtin="builtin"
      @close="openId = null"
      @changed="refresh"
    />
    <TemplateEditor
      :template-id="templateId"
      :categories="categories"
      :builtin="builtin"
      @close="templateId = null"
      @changed="refresh"
      @created="templateId = $event"
      @use="useTemplate"
    />
    <TemplateUploadDialog
      v-model="uploadOpen"
      :categories="categories"
      :category-id="selected"
      @uploaded="onUploaded"
    />
    <ContractSettingsDialog v-model="settingsOpen" :settings="settings" @saved="settings = $event" />
  </div>
</template>

<style scoped>
.layout {
  display: grid;
  grid-template-columns: 240px minmax(0, 1fr);
  gap: 16px;
  align-items: start;
}

.side {
  position: sticky;
  top: 0;
}

@media (max-width: 900px) {
  .layout {
    grid-template-columns: minmax(0, 1fr);
  }

  .side {
    position: static;
  }
}
</style>
