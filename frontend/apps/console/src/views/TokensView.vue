<script setup lang="ts">
import { errorMessage, type Schemas } from '@edp/api-client'
import { ElMessage } from 'element-plus'
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { api } from '../api'
import StatTile from '../components/charts/StatTile.vue'
import TrendChart from '../components/charts/TrendChart.vue'
import TokenCallsDrawer from '../components/tokens/TokenCallsDrawer.vue'
import {
  changeText,
  dailyAverage,
  dailyPoints,
  feeText,
  monthOptions,
  shareText,
  tokenText,
} from '../tokens'

/**
 * 企业 token 计费（设计文档 §37）：这个月（或以前的月份）企业的 AI 用掉多少 tokens、多少钱，每天的用量，按场景、
 * 模型、员工的构成；"查看近 7 天明细"列出每一次调用。链接带 details=1 时直接打开明细。
 */
type Group = 'scene' | 'model' | 'staff'
type Row = Schemas['TokenGroup']

const route = useRoute()
const router = useRouter()

const month = ref('')
const summary = ref<Schemas['TokenSummary'] | null>(null)
const loading = ref(false)
const group = ref<Group>('scene')
const detailsOpen = ref(false)

const months = computed(() => (summary.value ? monthOptions(summary.value.today) : []))
const totals = computed(() => summary.value?.totals)
const rows = computed<Row[]>(() => {
  const s = summary.value
  if (!s) return []
  return group.value === 'scene' ? s.by_scene : group.value === 'model' ? s.by_model : s.by_staff
})
const points = computed(() => dailyPoints(summary.value?.days ?? []))

async function load(): Promise<void> {
  loading.value = true
  const { data, error } = await api.GET('/api/v1/tokens/summary', {
    params: { query: { month: month.value || undefined } },
  })
  loading.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  summary.value = data
  month.value = data.month
}

function comparison(current: number, previous: number): string {
  const change = changeText(current, previous)
  return change ? `比上月同期 ${change}` : '上月同期没有用量'
}

function openDetails(): void {
  detailsOpen.value = true
}

watch(month, (value, old) => {
  if (old && value !== old) void load()
})

watch(detailsOpen, (open) => {
  if (!open && route.query.details) void router.replace({ query: { ...route.query, details: undefined } })
})

onMounted(async () => {
  await load()
  if (route.query.details) detailsOpen.value = true
})
</script>

<template>
  <div v-loading="loading">
    <div class="page-header">
      <h2>Token 计费</h2>
      <span>
        <el-select v-model="month" class="month" data-testid="token-month">
          <el-option v-for="[value, label] in months" :key="value" :label="label" :value="value" />
        </el-select>
        <el-button type="primary" data-testid="token-details-open" @click="openDetails">查看近 7 天明细</el-button>
      </span>
    </div>

    <template v-if="summary && totals">
      <el-alert
        v-if="summary.own_key"
        type="info"
        :closable="false"
        show-icon
        class="notice"
        title="你们在 AI 设置里用的是自己的大模型接口密钥：tokens 照常记录，费用由模型厂商直接收取，这里记为 0。"
        data-testid="token-own-key"
      />
      <p class="hint">
        费用按平台为每个模型设置的价格（输入、输出分别按每千 tokens 计价）在每次调用时算好；失败的调用不收费。
        统计按企业时区（{{ summary.timezone }}），实时更新。
      </p>

      <div class="tiles">
        <StatTile
          label="tokens 合计"
          :value="tokenText(totals.tokens)"
          :hint="`输入 ${tokenText(totals.prompt_tokens)} · 输出 ${tokenText(totals.completion_tokens)}`"
          testid="token-total"
        />
        <StatTile
          label="费用"
          :value="feeText(totals.cost)"
          :hint="comparison(totals.cost, summary.previous.cost)"
          testid="token-cost"
        />
        <StatTile
          label="调用次数"
          :value="totals.calls.toLocaleString('zh-CN')"
          :hint="totals.failed ? `失败 ${totals.failed} 次（不收费）` : '全部成功'"
          testid="token-calls"
        />
        <StatTile
          label="日均 tokens"
          :value="tokenText(dailyAverage(totals.tokens, summary.days.length))"
          :hint="comparison(totals.tokens, summary.previous.tokens)"
          testid="token-daily"
        />
      </div>

      <section class="block">
        <h4>每天的 tokens</h4>
        <TrendChart
          v-if="points.length"
          :points="points"
          kind="column"
          :format="tokenText"
          label="每天的 tokens"
          data-testid="token-trend"
        />
      </section>

      <section class="block">
        <div class="block-head">
          <h4>用在哪里</h4>
          <el-radio-group v-model="group" size="small" data-testid="token-group">
            <el-radio-button value="scene">按场景</el-radio-button>
            <el-radio-button value="model">按模型</el-radio-button>
            <el-radio-button value="staff">按员工</el-radio-button>
          </el-radio-group>
        </div>
        <el-table :data="rows" size="small" empty-text="这个月还没有用量" data-testid="token-groups">
          <el-table-column prop="label" :label="group === 'scene' ? '场景' : group === 'model' ? '模型' : '员工'" min-width="180" />
          <el-table-column label="调用次数" width="110" align="right">
            <template #default="{ row }: { row: Row }">{{ row.calls.toLocaleString('zh-CN') }}</template>
          </el-table-column>
          <el-table-column label="tokens" width="130" align="right">
            <template #default="{ row }: { row: Row }">{{ tokenText(row.tokens) }}</template>
          </el-table-column>
          <el-table-column label="占比" width="90" align="right">
            <template #default="{ row }: { row: Row }">{{ shareText(row.tokens, totals.tokens) }}</template>
          </el-table-column>
          <el-table-column label="费用" width="120" align="right">
            <template #default="{ row }: { row: Row }">{{ feeText(row.cost) }}</template>
          </el-table-column>
        </el-table>
      </section>
    </template>

    <TokenCallsDrawer v-model="detailsOpen" />
  </div>
</template>

<style scoped>
.month {
  width: 150px;
}

.notice {
  margin-bottom: 12px;
}

.hint {
  margin: 0 0 12px;
  font-size: 13px;
  color: var(--el-text-color-secondary);
}

.tiles {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 12px;
}

.block {
  margin-top: 16px;
  padding: 14px 16px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 8px;
  background: var(--el-bg-color);
}

.block h4 {
  margin: 0 0 10px;
  font-size: 14px;
}

.block-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 10px;
}

.block-head h4 {
  margin: 0;
}

@media (max-width: 900px) {
  .tiles {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}
</style>
