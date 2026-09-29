<script setup lang="ts">
import type { Schemas } from '@edp/api-client'
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'

import { api } from '../api'
import StatTile from '../components/charts/StatTile.vue'
import { formatDuration } from '../labels'
import { visibleMenus } from '../menu'
import { useAuthStore } from '../stores/auth'

const REFRESH_MS = 15_000

const auth = useAuthStore()
const modules = computed(() => visibleMenus(auth.permissions).filter((m) => m.name !== 'dashboard'))
const live = ref<Schemas['Realtime'] | null>(null)
const serves = computed(() => auth.can('workbench:use'))
const oversees = computed(
  () => auth.can('session:read_all') || auth.can('session:read_team') || auth.can('routing:manage'),
)
let timer: ReturnType<typeof setInterval> | null = null

async function refresh(): Promise<void> {
  const { data } = await api.GET('/api/v1/reports/realtime')
  if (data) live.value = data
}

onMounted(() => {
  void refresh()
  timer = setInterval(() => void refresh(), REFRESH_MS)
})
onBeforeUnmount(() => {
  if (timer) clearInterval(timer)
})
</script>

<template>
  <div>
    <div class="page-header">
      <h2>你好，{{ auth.me?.display_name }}</h2>
      <span class="muted">{{ auth.me?.tenant.name }}</span>
    </div>

    <div v-if="live && (serves || oversees)" class="tiles" data-testid="realtime-tiles">
      <StatTile
        label="排队中"
        :value="String(live.queued)"
        :hint="
          live.longest_wait_seconds !== null
            ? `最长已等 ${formatDuration(live.longest_wait_seconds)}`
            : '没有客户在等待'
        "
        testid="rt-queued"
      />
      <template v-if="serves">
        <StatTile label="我正在接待" :value="String(live.my_serving)" testid="rt-my-serving" />
        <StatTile label="我今天的会话" :value="String(live.my_today_sessions)" />
      </template>
      <template v-if="oversees">
        <StatTile label="接待中" :value="String(live.serving)" testid="rt-serving" />
        <StatTile
          label="在线坐席"
          :value="String(live.agents_online + live.agents_busy)"
          :hint="`忙碌 ${live.agents_busy}，小休 ${live.agents_away}`"
          testid="rt-agents"
        />
        <StatTile
          label="今日会话"
          :value="String(live.today_sessions)"
          :hint="`已结束 ${live.today_closed}`"
          testid="rt-today"
        />
        <StatTile
          label="今日满意度"
          :value="
            live.today_csat_avg === null || live.today_csat_avg === undefined
              ? '—'
              : `${live.today_csat_avg.toFixed(1)} 分`
          "
          :hint="`${live.today_csat_count} 个评价`"
        />
      </template>
    </div>
    <p v-if="live" class="muted refresh">每 15 秒自动刷新</p>

    <h3 class="section">常用功能</h3>
    <el-space wrap>
      <router-link v-for="item in modules" :key="item.name" :to="item.path">
        <el-card shadow="hover" class="module">{{ item.title }}</el-card>
      </router-link>
    </el-space>
  </div>
</template>

<style scoped>
.tiles {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(170px, 1fr));
  gap: 12px;
}

.muted {
  color: var(--el-text-color-secondary);
  font-size: 13px;
}

.refresh {
  margin: 8px 0 0;
  font-size: 12px;
}

.section {
  margin: 24px 0 12px;
  font-size: 15px;
}

.module {
  width: 160px;
  text-align: center;
}

a {
  text-decoration: none;
}
</style>
