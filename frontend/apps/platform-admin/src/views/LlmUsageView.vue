<script setup lang="ts">
import { errorMessage, type Schemas } from '@edp/api-client'
import { ElMessage } from 'element-plus'
import { onMounted, ref, watch } from 'vue'

import { api } from '../api'

/** 大模型用量与费用（设计文档 §11.5）：按模型、租户、场景统计调用次数、tokens 和估算费用。 */
const days = ref(30)
const usage = ref<Schemas['LlmUsage'] | null>(null)
const loading = ref(false)

function yuan(fen: number): string {
  return `¥${(fen / 100).toFixed(2)}`
}

async function load(): Promise<void> {
  loading.value = true
  const { data, error } = await api.GET('/platform/v1/llm-usage', {
    params: { query: { days: days.value } },
  })
  loading.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  usage.value = data
}

watch(days, load)
onMounted(load)
</script>

<template>
  <div v-loading="loading">
    <div class="page-header">
      <h2>大模型用量</h2>
      <el-radio-group v-model="days" size="small">
        <el-radio-button :value="7">近 7 天</el-radio-button>
        <el-radio-button :value="30">近 30 天</el-radio-button>
        <el-radio-button :value="90">近 90 天</el-radio-button>
      </el-radio-group>
    </div>
    <p class="sub">费用按供应商配置的单价估算（每千 tokens 多少分），租户自带密钥的调用不计入平台费用。</p>
    <template v-if="usage">
      <div class="tiles" data-testid="llm-usage-totals">
        <div class="tile">
          <div class="label">调用次数</div>
          <div class="value">{{ usage.total_calls.toLocaleString('zh-CN') }}</div>
        </div>
        <div class="tile">
          <div class="label">tokens</div>
          <div class="value">{{ usage.total_tokens.toLocaleString('zh-CN') }}</div>
        </div>
        <div class="tile">
          <div class="label">估算费用</div>
          <div class="value">{{ yuan(usage.total_cost) }}</div>
        </div>
      </div>
      <div class="tables">
        <section v-for="[title, rows] in ([['按模型', usage.by_model], ['按租户', usage.by_tenant], ['按场景', usage.by_scene]] as const)" :key="title">
          <h3>{{ title }}</h3>
          <el-table :data="rows" size="small" empty-text="暂无调用">
            <el-table-column prop="label" label="名称" min-width="140" />
            <el-table-column label="调用" width="80" align="right">
              <template #default="{ row }">{{ row.calls.toLocaleString('zh-CN') }}</template>
            </el-table-column>
            <el-table-column label="失败" width="64" align="right">
              <template #default="{ row }">{{ row.errors }}</template>
            </el-table-column>
            <el-table-column label="tokens" width="100" align="right">
              <template #default="{ row }">{{ row.tokens.toLocaleString('zh-CN') }}</template>
            </el-table-column>
            <el-table-column label="费用" width="90" align="right">
              <template #default="{ row }">{{ yuan(row.cost) }}</template>
            </el-table-column>
          </el-table>
        </section>
      </div>
    </template>
  </div>
</template>

<style scoped>
.page-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 8px;
}

.page-header h2 {
  margin: 0;
  font-size: 18px;
}

.sub {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.tiles {
  display: flex;
  gap: 12px;
  margin: 12px 0;
}

.tile {
  flex: 1;
  padding: 12px 16px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 6px;
}

.label {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.value {
  margin-top: 4px;
  font-size: 20px;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
}

.tables {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(360px, 1fr));
  gap: 16px;
}

h3 {
  margin: 8px 0;
  font-size: 14px;
}
</style>
