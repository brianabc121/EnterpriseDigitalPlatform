<script setup lang="ts">
import { type Schemas } from '@edp/api-client'
import { onMounted, ref } from 'vue'

import { api, formatDateTime } from '../../api'
import { percent } from '../../reports'

/** 一条知识的使用与满意度：引用、员工和访客的评价、引用它的 AI 会话的满意度与转人工。 */
const props = defineProps<{ itemId: string }>()
const stats = ref<Schemas['KbItemStats'] | null>(null)

onMounted(async () => {
  const { data } = await api.GET('/api/v1/kb/items/{item_id}/stats', {
    params: { path: { item_id: props.itemId } },
  })
  stats.value = data ?? null
})
</script>

<template>
  <div v-if="stats" class="stats" data-testid="kb-item-stats">
    <el-alert
      v-if="stats.zombie"
      type="warning"
      :closable="false"
      show-icon
      title="长期未命中：发布超过 90 天，近 90 天没有被引用。考虑下线或补充问法。"
      class="zombie"
    />
    <el-descriptions :column="2" size="small" border>
      <el-descriptions-item label="被引用">
        {{ stats.hits }} 次
        <span v-if="stats.last_hit_at" class="muted">（最近 {{ formatDateTime(stats.last_hit_at) }}）</span>
      </el-descriptions-item>
      <el-descriptions-item label="员工评价">有用 {{ stats.likes }} · 没用 {{ stats.dislikes }}</el-descriptions-item>
      <el-descriptions-item label="访客评价">
        有用 {{ stats.visitor_likes }} · 没用 {{ stats.visitor_dislikes }}
        <span v-if="stats.visitor_satisfaction !== null" class="muted">
          （{{ percent(stats.visitor_satisfaction) }} 有用）
        </span>
      </el-descriptions-item>
      <el-descriptions-item label="AI 引用的会话">
        {{ stats.ai_sessions }} 个，其中转人工 {{ stats.handoff_sessions }} 个
      </el-descriptions-item>
      <el-descriptions-item label="这些会话的满意度" :span="2">
        <template v-if="stats.csat_avg !== null">
          {{ stats.csat_avg.toFixed(2) }} 分（{{ stats.csat_count }} 个评价）
        </template>
        <span v-else class="muted">还没有评价</span>
      </el-descriptions-item>
    </el-descriptions>
  </div>
</template>

<style scoped>
.stats {
  margin-top: 8px;
}

.zombie {
  margin-bottom: 8px;
}

.muted {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}
</style>
