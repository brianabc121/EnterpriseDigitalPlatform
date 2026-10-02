<script setup lang="ts">
import { errorMessage, type Schemas } from '@edp/api-client'
import { ElMessage } from 'element-plus'
import { onMounted, ref } from 'vue'

import { api, formatDateTime } from '../../api'
import HomeSection from './HomeSection.vue'

/** 知识管理员的首页（§25.15）：待审核的知识（从会话中提炼的候选）和 7 天内到期的知识。 */
const LIMIT = 5
const CANDIDATE_KIND: Record<string, string> = {
  new: '新问题',
  similar: '相似问法',
  conflict: '答案冲突',
  gap: '知识缺口',
  phrase: '优秀话术',
  duplicate: '重复',
}

const candidates = ref<Schemas['KbCandidateOut'][]>([])
const pending = ref(0)
const expiring = ref<Schemas['KbItemOut'][]>([])
const expiringTotal = ref(0)

async function load(): Promise<void> {
  const [review, soon] = await Promise.all([
    api.GET('/api/v1/kb/candidates', { params: { query: { status: 'pending', limit: LIMIT } } }),
    api.GET('/api/v1/kb/items', { params: { query: { expiring: true, limit: LIMIT } } }),
  ])
  if (!review.data || !soon.data) {
    ElMessage.error(errorMessage(review.error ?? soon.error))
    return
  }
  candidates.value = review.data.items
  pending.value = Object.values(review.data.pending).reduce((a, b) => a + b, 0)
  expiring.value = soon.data.items
  expiringTotal.value = soon.data.total
}

onMounted(load)
</script>

<template>
  <div data-testid="home-knowledge">
    <HomeSection :title="`待审核的知识${pending ? `（${pending}）` : ''}`" testid="home-kb-review">
      <template #extra>
        <router-link :to="{ path: '/knowledge', query: { tab: 'review' } }" data-testid="home-kb-review-link"
          >去审核台</router-link
        >
      </template>
      <ul class="list">
        <li v-if="!candidates.length" class="empty">没有待审核的知识</li>
        <li v-for="item in candidates" :key="item.id" data-testid="home-kb-candidate">
          <el-tag size="small" effect="plain">{{ CANDIDATE_KIND[item.kind] ?? item.kind }}</el-tag>
          <span class="title">{{ item.question }}</span>
          <span class="muted">出现 {{ item.occurrences }} 次 · {{ formatDateTime(item.last_seen_at) }}</span>
        </li>
      </ul>
    </HomeSection>

    <HomeSection :title="`快到期的知识${expiringTotal ? `（${expiringTotal}）` : ''}`" testid="home-kb-expiring">
      <template #extra><span class="muted">7 天内到期，到期后自动下线</span></template>
      <ul class="list">
        <li v-if="!expiring.length" class="empty">没有快到期的知识</li>
        <li v-for="item in expiring" :key="item.id" data-testid="home-kb-expiring-item">
          <router-link :to="{ path: '/knowledge', query: { item: item.id } }" class="title">{{ item.title }}</router-link>
          <span class="muted">{{ item.valid_to ? `${formatDateTime(item.valid_to)} 到期` : '' }}</span>
        </li>
      </ul>
    </HomeSection>
  </div>
</template>

<style scoped>
.list {
  margin: 0;
  padding: 0;
  list-style: none;
  background: var(--el-bg-color);
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 8px;
}

.list li {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 10px 14px;
  border-bottom: 1px solid var(--el-border-color-lighter);
}

.list li:last-child {
  border-bottom: none;
}

.empty {
  justify-content: center;
  color: var(--el-text-color-secondary);
  font-size: 13px;
}

.title {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.muted {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

a {
  color: var(--el-color-primary);
  text-decoration: none;
}

a.title {
  color: var(--el-text-color-primary);
}

a.title:hover {
  color: var(--el-color-primary);
}
</style>
