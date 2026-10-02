<script setup lang="ts">
import { errorMessage, type Schemas } from '@edp/api-client'
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { KB_KIND, KB_STATUS, KB_STATUS_TAG, KB_VISIBILITY } from '../ai'
import { api, formatDateTime } from '../api'
import FormKbPanel from '../components/knowledge/FormKbPanel.vue'
import KbAlignmentPanel from '../components/knowledge/KbAlignmentPanel.vue'
import KbDigestPanel from '../components/knowledge/KbDigestPanel.vue'
import KbImportDialog from '../components/knowledge/KbImportDialog.vue'
import KbItemEditor from '../components/knowledge/KbItemEditor.vue'
import KbMetricsPanel from '../components/knowledge/KbMetricsPanel.vue'
import KbSearchPanel from '../components/knowledge/KbSearchPanel.vue'
import KbSpaceTree, { type Selection } from '../components/knowledge/KbSpaceTree.vue'
import ReviewDesk from '../components/knowledge/ReviewDesk.vue'
import { placementLabel, placementPath } from '../knowledge'
import { useAuthStore } from '../stores/auth'
import { useKbSpacesStore } from '../stores/kbSpaces'

type Item = Schemas['KbItemOut']
type Status = 'draft' | 'published' | 'archived'

const PAGE_SIZE = 20
const auth = useAuthStore()
const spaces = useKbSpacesStore()
const route = useRoute()
const router = useRouter()
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
const mine = ref(false)
// 规章制度（§33.7.1）和知识库整理的整理清单（§33.7.2）的筛选。
const policyOnly = ref(false)
const expiring = ref(false)
const noOwner = ref(false)
const disliked = ref(false)
type Housekeeping = 'stale' | 'expiring' | 'no_owner' | 'disliked' | 'policy'
const reviewSource = ref<'' | 'policy'>('')
const node = ref('all')
const selection = ref<Selection>({})
/** 新建知识时默认放到左侧选中的空间或分类。 */
const newPlacement = computed(() => {
  const space = selection.value.space_id
  if (space) return [space]
  const category = selection.value.category_id
  const owner = spaces.spaces.find((s) => s.categories.some((c) => c.id === category))
  return owner ? placementPath(spaces.spaces, owner.id, category) : []
})

function select(key: string, value: Selection): void {
  node.value = key
  selection.value = value
  reload()
}

function placement(item: Item): string {
  return placementLabel(spaces.spaces, item.space_id, item.category_id) || item.category
}

async function load(): Promise<void> {
  loading.value = true
  const { data, error } = await api.GET('/api/v1/kb/items', {
    params: {
      query: {
        status: status.value || undefined,
        kind: kind.value || undefined,
        q: keyword.value.trim() || undefined,
        stale: stale.value || undefined,
        mine: mine.value || undefined,
        policy: policyOnly.value || undefined,
        expiring: expiring.value || undefined,
        no_owner: noOwner.value || undefined,
        disliked: disliked.value || undefined,
        ...selection.value,
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

function onSaved(): void {
  void load()
  void spaces.load()
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

/** 从站内信等处打开 /knowledge?item=<id>：直接打开这条知识。 */
async function openFromQuery(): Promise<void> {
  const id = typeof route.query.item === 'string' ? route.query.item : ''
  if (!id) return
  const { data } = await api.GET('/api/v1/kb/items/{item_id}', { params: { path: { item_id: id } } })
  if (data) edit(data)
  await router.replace({ query: {} })
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

/** 制度对齐页签里点了整理清单的数字：回到知识条目，只看这一类。 */
function showOnly(name: Housekeeping): void {
  stale.value = name === 'stale'
  expiring.value = name === 'expiring'
  noOwner.value = name === 'no_owner'
  disliked.value = name === 'disliked'
  policyOnly.value = name === 'policy'
  status.value = ''
  tab.value = 'items'
}

function reviewPolicy(): void {
  reviewSource.value = 'policy'
  tab.value = 'review'
}

watch([status, kind, stale, mine, policyOnly, expiring, noOwner, disliked], reload)
watch(() => route.query.item, openFromQuery)
onMounted(async () => {
  // 首页的"去审核台"（/knowledge?tab=review）；表单知识（/knowledge?tab=form）。
  if (route.query.tab === 'review' && canManage.value) tab.value = 'review'
  if (route.query.tab === 'form') tab.value = 'form'
  // 知识库整理的通知（/knowledge?tab=review）和"制度对齐"（/knowledge?tab=alignment）。
  if (route.query.tab === 'alignment' && canManage.value) tab.value = 'alignment'
  await Promise.all([load(), spaces.load(), openFromQuery()])
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
        <div class="items-layout">
        <KbSpaceTree
          :can-manage="canManage"
          :selected="node"
          @select="select"
          @changed="load"
        />
        <div class="items-main">
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
          <el-checkbox v-if="canManage" v-model="mine" data-testid="kb-mine-filter">
            我负责的
          </el-checkbox>
          <el-checkbox v-model="policyOnly" data-testid="kb-policy-filter">规章制度</el-checkbox>
          <el-checkbox v-if="canManage && expiring" v-model="expiring" data-testid="kb-expiring-filter">
            快到期
          </el-checkbox>
          <el-checkbox v-if="canManage && noOwner" v-model="noOwner" data-testid="kb-no-owner-filter">
            没有负责人
          </el-checkbox>
          <el-checkbox v-if="canManage && disliked" v-model="disliked" data-testid="kb-disliked-filter">
            评价差
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
              <el-tag v-if="row.policy" size="small" type="warning" class="kind-tag" data-testid="kb-policy-tag"
                >制度</el-tag
              >
              <span>{{ row.title }}</span>
              <span v-if="row.questions.length" class="muted"
                >+{{ row.questions.length }} 个问法</span
              >
            </template>
          </el-table-column>
          <el-table-column label="空间 / 分类" min-width="140">
            <template #default="{ row }">
              <span class="placement">{{ placement(row) }}</span>
            </template>
          </el-table-column>
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
        </div>
        </div>
      </el-tab-pane>
      <el-tab-pane v-if="canManage" name="review" lazy>
        <template #label>
          审核台
          <el-badge v-if="pendingCandidates" :value="pendingCandidates" class="badge" />
        </template>
        <ReviewDesk
          :source="reviewSource"
          @reviewed="load"
          @pending="(n: number) => (pendingCandidates = n)"
        />
      </el-tab-pane>
      <el-tab-pane v-if="canManage" label="制度对齐" name="alignment" lazy>
        <KbAlignmentPanel @review="reviewPolicy" @filter="showOnly" />
      </el-tab-pane>
      <el-tab-pane label="表单知识" name="form" lazy>
        <FormKbPanel />
      </el-tab-pane>
      <el-tab-pane v-if="canManage" label="运营数据" name="metrics" lazy>
        <KbMetricsPanel />
      </el-tab-pane>
      <el-tab-pane label="周报" name="digest" lazy>
        <KbDigestPanel />
      </el-tab-pane>
    </el-tabs>

    <KbItemEditor
      v-model="editorOpen"
      :item="editing"
      :kind="newKind"
      :placement="newPlacement"
      @saved="onSaved"
    />
    <KbImportDialog v-model="importOpen" :placement="newPlacement" @imported="onSaved" />
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

.items-layout {
  display: flex;
  gap: 16px;
  align-items: flex-start;
}

.items-main {
  flex: 1;
  min-width: 0;
}

.placement {
  font-size: 12px;
  color: var(--el-text-color-secondary);
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
