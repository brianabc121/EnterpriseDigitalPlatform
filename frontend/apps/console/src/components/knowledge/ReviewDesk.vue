<script setup lang="ts">
import { errorMessage, type Schemas } from '@edp/api-client'
import { ElMessage } from 'element-plus'
import { computed, onMounted, ref, watch } from 'vue'

import { api, formatDateTime } from '../../api'
import {
  CANDIDATE_KIND,
  CANDIDATE_KIND_TAG,
  CANDIDATE_SOURCE,
  CANDIDATE_STATUS,
} from '../../knowledge'
import CandidateDrawer from './CandidateDrawer.vue'

/**
 * 审核台（设计 §12.5）：从会话提炼的候选按影响排序；缺口单独筛选即为"知识缺口榜"。知识库整理
 * （§33.7）的建议来源是"制度对齐"：与制度冲突、制度里有知识库里没有、重复的知识。
 */
const props = defineProps<{ source?: Source }>()
const emit = defineEmits<{ reviewed: []; pending: [count: number] }>()

type Kind = '' | 'new' | 'similar' | 'conflict' | 'gap' | 'phrase' | 'duplicate'
type Status = 'pending' | 'approved' | 'merged' | 'rejected'
type Source = '' | 'session' | 'sidebar' | 'zone' | 'group' | 'policy'

const PAGE_SIZE = 20
const kind = ref<Kind>('')
const source = ref<Source>(props.source ?? '')
const status = ref<Status>('pending')
const page = ref(1)
const loading = ref(false)
const data = ref<Schemas['KbCandidatePage'] | null>(null)
const viewing = ref<string | null>(null)

const pendingTotal = computed(() =>
  Object.values(data.value?.pending ?? {}).reduce((sum, n) => sum + n, 0),
)

async function load(): Promise<void> {
  loading.value = true
  const { data: page_, error } = await api.GET('/api/v1/kb/candidates', {
    params: {
      query: {
        status: status.value,
        kind: kind.value || undefined,
        source: source.value || undefined,
        limit: PAGE_SIZE,
        offset: (page.value - 1) * PAGE_SIZE,
      },
    },
  })
  loading.value = false
  if (!page_) {
    ElMessage.error(errorMessage(error))
    return
  }
  data.value = page_
  emit('pending', pendingTotal.value)
}

function onReviewed(): void {
  viewing.value = null
  emit('reviewed')
  void load()
}

watch(
  () => props.source,
  (value) => {
    if (value !== undefined) source.value = value
  },
)
watch([kind, status, source], () => {
  page.value = 1
  void load()
})
onMounted(load)

defineExpose({ load })
</script>

<template>
  <div class="review-desk" data-testid="review-desk">
    <div class="filters">
      <el-radio-group v-model="kind" size="small" data-testid="candidate-kind-filter">
        <el-radio-button value="">全部 {{ pendingTotal }}</el-radio-button>
        <el-radio-button v-for="(label, value) in CANDIDATE_KIND" :key="value" :value="value">
          {{ label }} {{ data?.pending[value] ?? 0 }}
        </el-radio-button>
      </el-radio-group>
      <el-select v-model="source" size="small" class="source-filter" data-testid="candidate-source">
        <el-option label="全部来源" value="" />
        <el-option
          v-for="(label, value) in CANDIDATE_SOURCE"
          :key="value"
          :label="label"
          :value="value"
        />
      </el-select>
      <el-select v-model="status" size="small" class="status" data-testid="candidate-status">
        <el-option
          v-for="(label, value) in CANDIDATE_STATUS"
          :key="value"
          :label="label"
          :value="value"
        />
      </el-select>
    </div>
    <p class="hint">
      系统每小时从已结束的会话里提炼问答和没有解答的问题（先脱敏），相似的归为一类；
      出现次数多、最近还在出现的排在前面。来源是"制度对齐"的是 AI 对照现行规章制度整理知识库时
      提出的修改建议。
    </p>

    <el-table
      v-loading="loading"
      :data="data?.items ?? []"
      data-testid="candidates-table"
      :empty-text="status === 'pending' ? '没有待审核的候选' : '没有记录'"
      class="clickable"
      @row-click="(row: Schemas['KbCandidateOut']) => (viewing = row.id)"
    >
      <el-table-column label="类型" width="100">
        <template #default="{ row }">
          <el-tag size="small" :type="CANDIDATE_KIND_TAG[row.kind]">
            {{ CANDIDATE_KIND[row.kind] ?? row.kind }}
          </el-tag>
          <el-tag v-if="row.source !== 'session'" size="small" effect="plain" class="source">
            {{ CANDIDATE_SOURCE[row.source] ?? row.source }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="问题" min-width="220">
        <template #default="{ row }">
          <div>{{ row.question }}</div>
          <div v-if="row.variants.length > 1" class="muted">
            另有 {{ row.variants.length - 1 }} 种问法
          </div>
        </template>
      </el-table-column>
      <el-table-column label="答案" min-width="240">
        <template #default="{ row }">
          <div class="answer">{{ row.answer ?? '（客服没有给出答案）' }}</div>
          <div v-if="row.target_title" class="muted">原知识：{{ row.target_title }}</div>
        </template>
      </el-table-column>
      <el-table-column label="出现" width="110">
        <template #default="{ row }">
          <strong>{{ row.occurrences }}</strong> 次
          <div class="muted">近 7 天 {{ row.recent }} 次</div>
        </template>
      </el-table-column>
      <el-table-column v-if="status === 'pending'" label="最近出现" width="170">
        <template #default="{ row }">{{ formatDateTime(row.last_seen_at) }}</template>
      </el-table-column>
      <template v-else>
        <el-table-column label="处理" width="170">
          <template #default="{ row }">
            <div>{{ row.reviewed_by_name ?? '自动' }}</div>
            <div class="muted">{{ row.reviewed_at ? formatDateTime(row.reviewed_at) : '' }}</div>
          </template>
        </el-table-column>
        <el-table-column label="说明" min-width="140">
          <template #default="{ row }">{{ row.review_note ?? '' }}</template>
        </el-table-column>
      </template>
      <el-table-column label="" width="70">
        <template #default="{ row }">
          <el-button
            link
            type="primary"
            size="small"
            data-testid="review-candidate"
            @click.stop="viewing = row.id"
          >
            {{ status === 'pending' ? '审核' : '查看' }}
          </el-button>
        </template>
      </el-table-column>
    </el-table>
    <div class="page-footer">
      <el-pagination
        v-model:current-page="page"
        :page-size="PAGE_SIZE"
        :total="data?.total ?? 0"
        layout="total, prev, pager, next"
        @current-change="load"
      />
    </div>
    <CandidateDrawer :candidate-id="viewing" @close="viewing = null" @reviewed="onReviewed" />
  </div>
</template>

<style scoped>
.filters {
  display: flex;
  align-items: center;
  gap: 12px;
}

.status {
  width: 110px;
}

.source-filter {
  width: 120px;
}

.hint {
  margin: 8px 0 12px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.muted {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.answer {
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

.clickable :deep(.el-table__row) {
  cursor: pointer;
}
</style>
