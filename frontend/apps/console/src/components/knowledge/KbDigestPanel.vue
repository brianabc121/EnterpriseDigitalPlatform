<script setup lang="ts">
import { errorMessage, type Schemas } from '@edp/api-client'
import { ElMessage } from 'element-plus'
import { computed, onMounted, ref } from 'vue'

import { api, formatDateTime } from '../../api'
import { VERSION_CHANGE } from '../../knowledge'
import { percent } from '../../reports'
import { useAuthStore } from '../../stores/auth'

/** 知识周报（设计 §12.6）：每周一自动生成上一周的，管理员也可以随时生成本周的。 */
interface DigestItem {
  item_id?: string
  candidate_id?: string
  title?: string
  question?: string
  version?: number
  change?: string
  count?: number
  occurrences?: number
}
interface DigestData {
  week_start: string
  week_end: string
  new_items: DigestItem[]
  updated_items: DigestItem[]
  expired_items: DigestItem[]
  hot_items: DigestItem[]
  top_gaps: DigestItem[]
  candidates: { pending: number; reviewed: number; accepted: number }
  ai: { sessions: number; resolved: number; resolution_rate: number | null }
}

const auth = useAuthStore()
const digests = ref<Schemas['KbDigestOut'][]>([])
const week = ref<string>('')
const generating = ref(false)

const current = computed(() => {
  const found = digests.value.find((d) => d.week_start === week.value) ?? digests.value[0]
  return found ? (found.data as unknown as DigestData) : null
})
const createdAt = computed(
  () => digests.value.find((d) => d.week_start === week.value)?.created_at ?? null,
)

async function load(): Promise<void> {
  const { data, error } = await api.GET('/api/v1/kb/digests', { params: { query: { limit: 12 } } })
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  digests.value = data.items
  if (!data.items.some((d) => d.week_start === week.value))
    week.value = data.items[0]?.week_start ?? ''
}

async function generate(): Promise<void> {
  generating.value = true
  const { data, error } = await api.POST('/api/v1/kb/digests', { body: {} })
  generating.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  week.value = data.week_start
  await load()
  ElMessage.success('已生成本周周报')
}

onMounted(load)
</script>

<template>
  <div class="digest" data-testid="kb-digest">
    <div class="bar">
      <el-select v-if="digests.length" v-model="week" size="small" class="week">
        <el-option
          v-for="d in digests"
          :key="d.week_start"
          :label="`${d.week_start} 起的一周`"
          :value="d.week_start"
        />
      </el-select>
      <el-button
        v-if="auth.can('kb:manage')"
        size="small"
        :loading="generating"
        data-testid="generate-digest"
        @click="generate"
      >
        生成本周周报
      </el-button>
      <span v-if="createdAt" class="muted">生成于 {{ formatDateTime(createdAt) }}</span>
    </div>
    <el-empty v-if="!current" description="还没有周报：每周一自动生成上一周的周报" />
    <template v-else>
      <h3>{{ current.week_start }} 至 {{ current.week_end }}</h3>
      <div class="summary">
        <span
          >新增知识 <b>{{ current.new_items.length }}</b></span
        >
        <span
          >更新 <b>{{ current.updated_items.length }}</b></span
        >
        <span
          >到期下线 <b>{{ current.expired_items.length }}</b></span
        >
        <span>
          AI 接待 <b>{{ current.ai.sessions }}</b> 个会话，独立解决率
          <b>{{ percent(current.ai.resolution_rate) }}</b>
        </span>
        <span>
          候选：待审核 <b>{{ current.candidates.pending }}</b
          >，本周处理 <b>{{ current.candidates.reviewed }}</b
          >（通过 {{ current.candidates.accepted }}）
        </span>
      </div>
      <div class="sections">
        <section>
          <h4>新增知识</h4>
          <ul data-testid="digest-new">
            <li v-for="i in current.new_items" :key="i.item_id">{{ i.title }}</li>
            <li v-if="!current.new_items.length" class="muted">没有</li>
          </ul>
        </section>
        <section>
          <h4>更新的知识</h4>
          <ul>
            <li v-for="i in current.updated_items" :key="i.item_id">
              {{ i.title }}
              <span class="muted">v{{ i.version }} {{ VERSION_CHANGE[i.change ?? ''] ?? '' }}</span>
            </li>
            <li v-if="!current.updated_items.length" class="muted">没有</li>
          </ul>
        </section>
        <section>
          <h4>AI 引用最多</h4>
          <ol>
            <li v-for="i in current.hot_items" :key="i.item_id">
              {{ i.title }} <span class="muted">{{ i.count }} 次</span>
            </li>
            <li v-if="!current.hot_items.length" class="muted">没有</li>
          </ol>
        </section>
        <section>
          <h4>知识缺口 Top 10</h4>
          <ol data-testid="digest-gaps">
            <li v-for="g in current.top_gaps" :key="g.candidate_id">
              {{ g.question }} <span class="muted">{{ g.occurrences }} 次</span>
            </li>
            <li v-if="!current.top_gaps.length" class="muted">没有待处理的缺口</li>
          </ol>
        </section>
        <section v-if="current.expired_items.length">
          <h4>到期下线</h4>
          <ul>
            <li v-for="i in current.expired_items" :key="i.item_id">{{ i.title }}</li>
          </ul>
        </section>
      </div>
    </template>
  </div>
</template>

<style scoped>
.bar {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 12px;
}

.week {
  width: 180px;
}

.muted {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

h3 {
  margin: 8px 0;
  font-size: 16px;
}

.summary {
  display: flex;
  flex-wrap: wrap;
  gap: 8px 24px;
  font-size: 13px;
  margin-bottom: 16px;
}

.sections {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
  gap: 16px;
}

section {
  padding: 12px 16px;
  background: var(--el-bg-color);
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 8px;
}

h4 {
  margin: 0 0 8px;
  font-size: 14px;
}

ul,
ol {
  margin: 0;
  padding-left: 18px;
  font-size: 13px;
  line-height: 1.8;
}
</style>
