<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { api } from '../../api'
import type { Finding } from '../../wake'
import FindingList from '../wake/FindingList.vue'
import HomeSection from './HomeSection.vue'

/**
 * 首页"需要我处理的问题"（§33.5）：AI 巡检发现的、自己负责的问题（严重的在前），可以直接忽略或
 * 标记已处理。没有问题时不显示。
 */
const LIMIT = 5

const props = defineProps<{ canOpenWake: boolean }>()
const items = ref<Finding[]>([])
const total = ref(0)

async function load(): Promise<void> {
  const { data } = await api.GET('/api/v1/wake/findings', {
    params: { query: { view: 'mine', status: 'open', limit: LIMIT } },
  })
  items.value = data?.items ?? []
  total.value = data?.total ?? 0
}

onMounted(load)
defineExpose({ load })
</script>

<template>
  <HomeSection
    v-if="total"
    :title="`需要我处理的问题（${total}）`"
    testid="home-wake"
  >
    <template #extra>
      <span class="muted">AI 巡检企业数据时发现的</span>
      <router-link v-if="props.canOpenWake" to="/wake" data-testid="home-wake-link">全部问题</router-link>
    </template>
    <FindingList :items="items" :show-assignees="false" @changed="load" />
  </HomeSection>
</template>

<style scoped>
.muted {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

a {
  color: var(--el-color-primary);
  text-decoration: none;
}
</style>
