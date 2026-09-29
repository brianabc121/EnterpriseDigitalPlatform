<script setup lang="ts">
import { errorMessage, type Schemas } from '@edp/api-client'
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed, onMounted, ref, watch } from 'vue'

import { KB_KIND, KB_STATUS, KB_STATUS_TAG, KB_VISIBILITY } from '../ai'
import { api, formatDateTime } from '../api'
import KbDigestPanel from '../components/knowledge/KbDigestPanel.vue'
import KbImportDialog from '../components/knowledge/KbImportDialog.vue'
import KbItemEditor from '../components/knowledge/KbItemEditor.vue'
import KbMetricsPanel from '../components/knowledge/KbMetricsPanel.vue'
import KbSearchPanel from '../components/knowledge/KbSearchPanel.vue'
import ReviewDesk from '../components/knowledge/ReviewDesk.vue'
import { useAuthStore } from '../stores/auth'

type Item = Schemas['KbItemOut']
type Status = 'draft' | 'published' | 'archived'

const PAGE_SIZE = 20
const auth = useAuthStore()
const canManage = computed(() => auth.can('kb:manage'))
const canPublish = computed(() => auth.can('kb:publish'))

const items = ref<Item[]>([])
const total = ref(0)
const page = ref(1)
const status = ref<Status | ''>('')
const kind = ref<'faq' | 'doc' | ''>('')
const keyword = ref('')
const loading = ref(false)
const editing = ref<Item | null>(null)
const editorOpen = ref(false)
const newKind = ref<'faq' | 'doc'>('faq')
const importOpen = ref(false)
const searchOpen = ref(false)
const tab = ref('items')
const pendingCandidates = ref(0)
const stale = ref(false)

async function load(): Promise<void> {
  loading.value = true
  const { data, error } = await api.GET('/api/v1/kb/items', {
    params: {
      query: {
        status: status.value || undefined,
        kind: kind.value || undefined,
        q: keyword.value.trim() || undefined,
        stale: stale.value || undefined,
        limit: PAGE_SIZE,
        offset: (page.value - 1) * PAGE_SIZE,
      },
    },
  })
  loading.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  items.value = data.items
  total.value = data.total
}

function reload(): void {
  page.value = 1
  void load()
}

function create(value: 'faq' | 'doc'): void {
  newKind.value = value
  editing.value = null
  editorOpen.value = true
}

function edit(item: Item): void {
  editing.value = item
  editorOpen.value = true
}

async function act(item: Item, action: 'publish' | 'archive'): Promise<void> {
  const path = `/api/v1/kb/items/{item_id}/${action}` as const
  const { data, error } = await api.POST(path, { params: { path: { item_id: item.id } } })
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  ElMessage.success(action === 'publish' ? '已发布' : '已下架')
  await load()
}

async function remove(item: Item): Promise<void> {
  try {
    await ElMessageBox.confirm(`删除「${item.title}」？删除后不能恢复。`, '删除知识', {
      confirmButtonText: '删除',
      cancelButtonText: '取消',
      type: 'warning',
    })
  } catch {
    return
  }
  const { error } = await api.DELETE('/api/v1/kb/items/{item_id}', {
    params: { path: { item_id: item.id } },
  })
  if (error) {
    ElMessage.error(errorMessage(error))
    return
  }
  ElMessage.success('已删除')
  await load()
}

watch([status, kind, stale], reload)
onMounted(async () => {
  await load()
  // 待审核候选数显示在"审核台"页签上。
  if (canManage.value) {
    const { data } = await api.GET('/api/v1/kb/candidates', { params: { query: { limit: 1 } } })
    if (data) pendingCandidates.value = Object.values(data.pending).reduce((a, b) => a + b, 0)
  }
})
</script>

<template>
  <div>
    <div class="page-header">
      <h2>知识库</h2>
      <div class="actions">
        <el-button data-testid="kb-open-search" @click="searchOpen = true">检索测试</el-button>
        <template v-if="canManage">
          <el-button data-testid="kb-open-import" @click="importOpen = true">批量导入</el-button>
          <el-dropdown @command="create">
            <el-button type="primary" data-testid="kb-create">新建</el-button>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item command="faq" data-testid="kb-create-faq">问答</el-dropdown-item>
                <el-dropdown-item command="doc" data-testid="kb-create-doc">文档</el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
        </template>
      </div>
    </div>

    <el-tabs v-model="tab" data-testid="kb-tabs">
      <el-tab-pane label="知识条目" name="items">
        <div class="filters">
          <el-radio-group
            v-if="canManage"
            v-model="status"
            size="small"
            data-testid="kb-status-filter"
          >
            <el-radio-button value="">全部</el-radio-button>
            <el-radio-button v-for="(label, value) in KB_STATUS" :key="value" :value="value">
              {{ label }}
            </el-radio-button>
          </el-radio-group>
          <el-select v-model="kind" size="small" class="kind" placeholder="类型">
            <el-option label="全部类型" value="" />
            <el-option
              v-for="(label, value) in KB_KIND"
              :key="value"
              :label="label"
              :value="value"
            />
          </el-select>
          <el-input
            v-model="keyword"
            size="small"
            class="keyword"
            placeholder="搜索标题或内容"
            clearable
            maxlength="100"
            data-testid="kb-keyword"
            @keyup.enter="reload"
            @clear="reload"
          />
          <el-checkbox v-if="canManage" v-model="stale" data-testid="kb-stale-filter">
            长期未命中
          </el-checkbox>
        </div>

        <el-table
          v-loading="loading"
          :data="items"
          data-testid="kb-table"
          empty-text="还没有知识，点击右上角新建或批量导入"
          class="clickable"
          @row-click="edit"
        >
          <el-table-column label="标题" min-width="260">
            <template #default="{ row }">
              <el-tag size="small" type="info" class="kind-tag">{{
                KB_KIND[row.kind] ?? row.kind
              }}</el-tag>
              <span>{{ row.title }}</span>
              <span v-if="row.questions.length" class="muted"
                >+{{ row.questions.length }} 个问法</span
              >
            </template>
          </el-table-column>
          <el-table-column prop="category" label="分类" width="110" />
          <el-table-column label="状态" width="90">
            <template #default="{ row }">
              <el-tag size="small" :type="KB_STATUS_TAG[row.status]">
                {{ KB_STATUS[row.status] ?? row.status }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="可见范围" width="140">
            <template #default="{ row }">{{
              KB_VISIBILITY[row.visibility] ?? row.visibility
            }}</template>
          </el-table-column>
          <el-table-column label="版本" width="64">
            <template #default="{ row }">v{{ row.version }}</template>
          </el-table-column>
          <el-table-column label="引用" width="70" align="right">
            <template #default="{ row }">{{ row.hits }}</template>
          </el-table-column>
          <el-table-column label="更新时间" width="170">
            <template #default="{ row }">{{ formatDateTime(row.updated_at) }}</template>
          </el-table-column>
          <el-table-column v-if="canManage || canPublish" label="" width="150">
            <template #default="{ row }">
              <span @click.stop>
                <el-button
                  v-if="canPublish && row.status !== 'published'"
                  link
                  type="primary"
                  size="small"
                  data-testid="kb-publish"
                  @click="act(row, 'publish')"
                >
                  发布
                </el-button>
                <el-button
                  v-if="canPublish && row.status === 'published'"
                  link
                  size="small"
                  data-testid="kb-archive"
                  @click="act(row, 'archive')"
                >
                  下架
                </el-button>
                <el-button
                  v-if="canManage && row.status !== 'published'"
                  link
                  type="danger"
                  size="small"
                  @click="remove(row)"
                >
                  删除
                </el-button>
              </span>
            </template>
          </el-table-column>
        </el-table>
        <div class="page-footer">
          <el-pagination
            v-model:current-page="page"
            :page-size="PAGE_SIZE"
            :total="total"
            layout="total, prev, pager, next"
            @current-change="load"
          />
        </div>
      </el-tab-pane>
      <el-tab-pane v-if="canManage" name="review" lazy>
        <template #label>
          审核台
          <el-badge v-if="pendingCandidates" :value="pendingCandidates" class="badge" />
        </template>
        <ReviewDesk @reviewed="load" @pending="(n: number) => (pendingCandidates = n)" />
      </el-tab-pane>
      <el-tab-pane v-if="canManage" label="运营数据" name="metrics" lazy>
        <KbMetricsPanel />
      </el-tab-pane>
      <el-tab-pane label="周报" name="digest" lazy>
        <KbDigestPanel />
      </el-tab-pane>
    </el-tabs>

    <KbItemEditor v-model="editorOpen" :item="editing" :kind="newKind" @saved="load" />
    <KbImportDialog v-model="importOpen" @imported="reload" />
    <el-drawer v-model="searchOpen" title="检索测试" size="480px">
      <p class="muted search-hint">
        按客户的问法检索已发布的知识，查看 AI 和坐席会引用哪些条目、相关度多少。
      </p>
      <KbSearchPanel />
    </el-drawer>
  </div>
</template>

<style scoped>
.actions {
  display: flex;
  gap: 8px;
}

.filters {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 12px;
}

.kind {
  width: 120px;
}

.keyword {
  width: 220px;
}

.kind-tag {
  margin-right: 6px;
}

.badge {
  margin-left: 4px;
}

.muted {
  margin-left: 6px;
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

.search-hint {
  margin: 0 0 12px;
}

.clickable :deep(.el-table__row) {
  cursor: pointer;
}
</style>
