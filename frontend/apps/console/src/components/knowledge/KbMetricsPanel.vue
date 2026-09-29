<script setup lang="ts">
import { errorMessage, type Schemas } from '@edp/api-client'
import { ElMessage } from 'element-plus'
import { computed, onMounted, ref, watch } from 'vue'

import { api } from '../../api'
import { formatHours } from '../../knowledge'
import { HANDOFF_REASON } from '../../labels'
import { browserTimeZone, lastDays, percent } from '../../reports'
import BarList from '../charts/BarList.vue'
import StatTile from '../charts/StatTile.vue'

/** 知识运营数据（设计 §12.7）：AI 效果、知识缺口、沉淀质量、单条知识。 */
const days = ref(30)
const data = ref<Schemas['KbMetrics'] | null>(null)
const loading = ref(false)

const reasons = computed(() =>
  (data.value?.handoff_reasons ?? []).map((r) => ({
    label: HANDOFF_REASON[r.reason] ?? (r.reason || '其他'),
    value: r.count,
  })),
)

async function load(): Promise<void> {
  const [start, end] = lastDays(days.value)
  loading.value = true
  const { data: metrics, error } = await api.GET('/api/v1/kb/metrics', {
    params: { query: { start, end, tz: browserTimeZone() } },
  })
  loading.value = false
  if (!metrics) {
    ElMessage.error(errorMessage(error))
    return
  }
  data.value = metrics
}

watch(days, load)
onMounted(load)
</script>

<template>
  <div v-loading="loading" class="metrics" data-testid="kb-metrics">
    <el-radio-group v-model="days" size="small" class="range">
      <el-radio-button :value="7">近 7 天</el-radio-button>
      <el-radio-button :value="30">近 30 天</el-radio-button>
      <el-radio-button :value="90">近 90 天</el-radio-button>
    </el-radio-group>
    <template v-if="data">
      <div class="tiles">
        <StatTile
          label="知识命中率"
          :value="percent(data.knowledge_hit_rate)"
          :hint="`AI 回复 ${data.ai_replies} 轮`"
          testid="kb-hit-rate"
        />
        <StatTile
          label="AI 建议采纳率"
          :value="percent(data.adoption_rate)"
          :hint="`采纳 ${data.suggestions_adopted} / 请求 ${data.suggestions}`"
          testid="kb-adoption"
        />
        <StatTile
          label="候选通过率"
          :value="percent(data.pass_rate)"
          :hint="`新提炼 ${data.candidates_new}，已处理 ${data.candidates_reviewed}`"
          testid="kb-pass-rate"
        />
        <StatTile
          label="待处理缺口"
          :value="String(data.gaps_open)"
          :hint="`新增 ${data.gaps_new}，已处理 ${data.gaps_closed}`"
          testid="kb-gaps"
        />
        <StatTile label="缺口处理时长" :value="formatHours(data.gap_close_hours)" />
        <StatTile
          label="长期未命中"
          :value="String(data.stale_items)"
          hint="发布 90 天以上且近 90 天未被引用"
        />
      </div>
      <div class="grid">
        <el-card shadow="never">
          <template #header>AI 转人工原因</template>
          <BarList :items="reasons" label="AI 转人工原因" empty="这段时间 AI 没有转人工" />
        </el-card>
        <el-card shadow="never">
          <template #header>AI 引用最多的知识</template>
          <el-table :data="data.top_items" size="small" empty-text="暂无引用">
            <el-table-column prop="title" label="知识" min-width="200" />
            <el-table-column prop="count" label="引用次数" width="90" align="right" />
          </el-table>
        </el-card>
        <el-card shadow="never">
          <template #header>评价为"没用"的知识</template>
          <el-table :data="data.disliked_items" size="small" empty-text="没有差评">
            <el-table-column prop="title" label="知识" min-width="200" />
            <el-table-column prop="count" label="人数" width="70" align="right" />
          </el-table>
        </el-card>
      </div>
    </template>
  </div>
</template>

<style scoped>
.range {
  margin-bottom: 12px;
}

.tiles {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
  gap: 12px;
  margin-bottom: 16px;
}

.grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
  gap: 16px;
}
</style>
