<script setup lang="ts">
import type { Schemas } from '@edp/api-client'
import { onMounted, ref } from 'vue'

import { api, formatDateTime } from '../../api'
import { percent } from '../../reports'

/** 必读知识的确认情况（当前版本）：有接待权限的在职员工中谁已确认。 */
const props = defineProps<{ itemId: string }>()
const stats = ref<Schemas['KbReadStats'] | null>(null)

onMounted(async () => {
  const { data } = await api.GET('/api/v1/kb/items/{item_id}/reads', {
    params: { path: { item_id: props.itemId } },
  })
  stats.value = data ?? null
})
</script>

<template>
  <div v-if="stats" class="reads" data-testid="kb-read-stats">
    <div class="summary">
      v{{ stats.version }} 已确认 <b>{{ stats.confirmed }}</b> / {{ stats.total }} 人（{{
        percent(stats.rate)
      }}）
    </div>
    <div class="people">
      <el-tag
        v-for="r in stats.readers"
        :key="r.staff_id"
        size="small"
        :type="r.read_at ? 'success' : 'info'"
        :title="r.read_at ? `确认于 ${formatDateTime(r.read_at)}` : '未确认'"
      >
        {{ r.display_name }}{{ r.read_at ? ' ✓' : '' }}
      </el-tag>
    </div>
  </div>
</template>

<style scoped>
.summary {
  font-size: 13px;
  margin-bottom: 6px;
}

.people {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
}
</style>
