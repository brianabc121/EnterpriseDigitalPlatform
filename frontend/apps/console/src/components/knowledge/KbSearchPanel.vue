<script setup lang="ts">
import { errorMessage, type Schemas } from '@edp/api-client'
import { ElMessage } from 'element-plus'
import { ref } from 'vue'

import { KB_KIND } from '../../ai'
import { api } from '../../api'
import { percent } from '../../reports'

/** 知识检索：知识库页的"检索测试"，以及工作台里坐席查知识、把答案插入回复框。 */
defineProps<{ insertable?: boolean }>()
const emit = defineEmits<{ insert: [text: string] }>()

const query = ref('')
const hits = ref<Schemas['KbSearchHit'][] | null>(null)
const loading = ref(false)
/** 我对每条知识的评价（1 有用、-1 没用）。 */
const votes = ref<Record<string, number>>({})

async function rate(hit: Schemas['KbSearchHit'], value: 1 | -1): Promise<void> {
  const next = votes.value[hit.item_id] === value ? 0 : value
  const { data, error } = await api.POST('/api/v1/kb/items/{item_id}/feedback', {
    params: { path: { item_id: hit.item_id } },
    body: { value: next },
  })
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  votes.value = { ...votes.value, [hit.item_id]: data.mine }
  if (next) ElMessage.success('谢谢反馈')
}

async function search(): Promise<void> {
  const q = query.value.trim()
  if (!q) return
  loading.value = true
  const { data, error } = await api.GET('/api/v1/kb/search', { params: { query: { q, limit: 5 } } })
  loading.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  hits.value = data.items
}
</script>

<template>
  <div class="kb-search" data-testid="kb-search">
    <el-input
      v-model="query"
      size="small"
      placeholder="输入客户的问题，查找相关知识"
      clearable
      maxlength="500"
      data-testid="kb-search-input"
      @keyup.enter="search"
    >
      <template #append>
        <el-button :loading="loading" data-testid="kb-search-button" @click="search">
          检索
        </el-button>
      </template>
    </el-input>
    <div v-if="hits" class="hits">
      <div v-for="hit in hits" :key="hit.item_id" class="hit" data-testid="kb-hit">
        <div class="hit-head">
          <el-tag size="small" type="info">{{ KB_KIND[hit.kind] ?? hit.kind }}</el-tag>
          <span class="title">{{ hit.title }}</span>
          <span
            class="score"
            :title="`语义 ${percent(hit.dense)} · 关键词 ${percent(hit.lexical)}`"
          >
            {{ percent(hit.score) }}
          </span>
        </div>
        <p class="text">{{ hit.text }}</p>
        <div class="hit-actions">
          <el-button
            v-if="insertable"
            link
            type="primary"
            size="small"
            data-testid="kb-insert"
            @click="emit('insert', hit.text)"
          >
            插入回复框
          </el-button>
          <span class="rate">
            <el-button
              link
              size="small"
              :type="votes[hit.item_id] === 1 ? 'primary' : undefined"
              data-testid="kb-like"
              @click="rate(hit, 1)"
            >
              有用
            </el-button>
            <el-button
              link
              size="small"
              :type="votes[hit.item_id] === -1 ? 'danger' : undefined"
              data-testid="kb-dislike"
              @click="rate(hit, -1)"
            >
              没用
            </el-button>
          </span>
        </div>
      </div>
      <el-empty v-if="hits.length === 0" :image-size="48" description="没有找到相关知识" />
    </div>
  </div>
</template>

<style scoped>
.hits {
  margin-top: 8px;
}

.hit {
  padding: 8px 0;
  border-bottom: 1px solid var(--el-border-color-lighter);
}

.hit:last-child {
  border-bottom: none;
}

.hit-head {
  display: flex;
  align-items: center;
  gap: 6px;
}

.title {
  flex: 1;
  min-width: 0;
  font-weight: 500;
  font-size: 13px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.score {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  font-variant-numeric: tabular-nums;
}

.hit-actions {
  display: flex;
  align-items: center;
}

.rate {
  margin-left: auto;
}

.text {
  margin: 4px 0 0;
  font-size: 12px;
  line-height: 1.6;
  color: var(--el-text-color-regular);
  white-space: pre-wrap;
  word-break: break-word;
  display: -webkit-box;
  -webkit-line-clamp: 6;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
</style>
