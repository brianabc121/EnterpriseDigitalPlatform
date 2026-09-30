<script setup lang="ts">
import { errorMessage } from '@edp/api-client'
import { ElMessage } from 'element-plus'
import { computed, ref, watch } from 'vue'

import { api, formatDateTime } from '../../api'
import {
  actionTag,
  actorColor,
  groupByDay,
  timeOf,
  TYPE_LABEL,
  type RecordHistory,
  type RecordType,
  type Version,
} from '../../history'
import VersionView from './VersionView.vue'

/**
 * 修改历史（设计文档 §25.14，参考在线表格的版本历史）：右边是按日期分组的版本列表（时间、操作人、
 * 操作、改动摘要），左边是选中版本的完整内容；"显示更改"时标出和对比版本的差异，对比的版本默认是
 * 上一个，也可以选最初的版本或任意一个。
 */
const open = defineModel<boolean>({ required: true })
const props = defineProps<{ recordType: RecordType; recordId: string | null; title?: string }>()

const history = ref<RecordHistory | null>(null)
const loading = ref(false)
const selected = ref<string | null>(null)
const highlight = ref(true)
// previous：上一个版本；first：最初的版本；其他是版本 id。
const compareWith = ref('previous')

const versions = computed<Version[]>(() => history.value?.versions ?? [])
const groups = computed(() => groupByDay(versions.value))
const current = computed(() => versions.value.find((v) => v.id === selected.value) ?? null)
const base = computed<Version | null>(() => {
  const list = versions.value
  const index = list.findIndex((v) => v.id === selected.value)
  if (index < 0) return null
  if (compareWith.value === 'previous') return list[index + 1] ?? null
  if (compareWith.value === 'first') {
    const first = list[list.length - 1]
    return first && first.id !== selected.value ? first : null
  }
  const other = list.find((v) => v.id === compareWith.value)
  return other && other.id !== selected.value ? other : null
})
const heading = computed(
  () => props.title ?? (history.value ? `${TYPE_LABEL[history.value.record_type]} ${history.value.label}` : ''),
)

watch(open, async (value) => {
  if (!value || !props.recordId) return
  history.value = null
  selected.value = null
  compareWith.value = 'previous'
  loading.value = true
  const { data, error } = await api.GET('/api/v1/history/{record_type}/{record_id}', {
    params: { path: { record_type: props.recordType, record_id: props.recordId } },
  })
  loading.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  history.value = data
  selected.value = data.versions[0]?.id ?? null
})

function label(v: Version): string {
  return `第 ${v.seq} 版 · ${v.actor_name} ${v.action_label} · ${formatDateTime(v.created_at)}`
}
</script>

<template>
  <el-drawer
    v-model="open"
    direction="rtl"
    size="min(1100px, 100%)"
    append-to-body
    :title="`修改历史 · ${heading}`"
    class="history-drawer"
    data-testid="history-drawer"
  >
    <div v-loading="loading" class="layout">
      <main class="content">
        <template v-if="current">
          <div class="toolbar">
            <el-switch v-model="highlight" active-text="显示更改" data-testid="history-highlight" />
            <span class="compare">
              对比
              <el-select v-model="compareWith" size="small" class="compare-select" data-testid="history-compare">
                <el-option label="上一个版本" value="previous" />
                <el-option label="最初的版本" value="first" />
                <el-option
                  v-for="v in versions.filter((x) => x.id !== current?.id)"
                  :key="v.id"
                  :label="label(v)"
                  :value="v.id"
                />
              </el-select>
            </span>
          </div>
          <div class="head" data-testid="history-selected">
            <span class="dot" :style="{ background: actorColor(current.actor_name) }"></span>
            <b>{{ current.actor_name }}</b>
            <el-tag size="small" :type="actionTag(current.action)">{{ current.action_label }}</el-tag>
            <span class="muted">{{ formatDateTime(current.created_at) }} · 第 {{ current.seq }} 版</span>
            <span v-if="current.actions.length > 1" class="muted">（{{ current.actions_label }}）</span>
          </div>
          <p v-if="current.reason || current.note" class="reason">
            <template v-if="current.reason">原因：{{ current.reason }}</template>
            <template v-if="current.reason && current.note">；</template>
            <template v-if="current.note">说明：{{ current.note }}</template>
          </p>
          <p v-if="highlight && !base" class="muted">这是{{ compareWith === 'previous' ? '最初的版本' : '对比的版本' }}，没有可以比较的内容。</p>
          <VersionView :doc="current.document" :base="base?.document ?? null" :highlight="highlight" />
        </template>
        <el-empty v-else-if="!loading" description="没有修改历史" />
      </main>
      <aside class="versions" data-testid="history-versions">
        <el-tag v-if="history?.deleted" type="danger" class="deleted" data-testid="history-deleted">已删除</el-tag>
        <div v-for="group in groups" :key="group.label" class="group">
          <div class="day">{{ group.label }}</div>
          <button
            v-for="v in group.items"
            :key="v.id"
            type="button"
            class="version"
            :class="{ active: v.id === selected }"
            data-testid="history-version"
            :data-action="v.action"
            @click="selected = v.id"
          >
            <span class="line1">
              <span class="time">{{ timeOf(v.created_at) }}</span>
              <span class="dot" :style="{ background: actorColor(v.actor_name) }"></span>
              <span class="actor">{{ v.actor_name }}</span>
              <el-tag size="small" :type="actionTag(v.action)">{{ v.action_label }}</el-tag>
              <span v-if="v.id === versions[0]?.id" class="latest">当前版本</span>
            </span>
            <span v-if="v.summary" class="summary" data-testid="history-summary">{{ v.summary }}</span>
            <span v-if="v.reason" class="summary">原因：{{ v.reason }}</span>
          </button>
        </div>
        <p v-if="history && !history.complete" class="muted legacy">更早的修改没有记录（启用修改历史之前）。</p>
      </aside>
    </div>
  </el-drawer>
</template>

<style scoped>
.layout {
  display: flex;
  gap: 16px;
  height: 100%;
  min-height: 300px;
}

.content {
  flex: 1;
  min-width: 0;
  overflow: auto;
}

.versions {
  flex-shrink: 0;
  width: 300px;
  overflow: auto;
  padding-left: 12px;
  border-left: 1px solid var(--el-border-color-lighter);
}

.toolbar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  margin-bottom: 12px;
}

.compare {
  display: flex;
  align-items: center;
  gap: 6px;
  color: var(--el-text-color-secondary);
  font-size: 13px;
}

.compare-select {
  width: 260px;
}

.head {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px 8px;
  margin-bottom: 8px;
}

.reason {
  margin: 0 0 12px;
  padding: 6px 10px;
  border-radius: 4px;
  background: var(--el-fill-color-lighter);
  font-size: 13px;
}

.dot {
  display: inline-block;
  flex-shrink: 0;
  width: 10px;
  height: 10px;
  border-radius: 50%;
}

.muted {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

.deleted {
  margin-bottom: 8px;
}

.day {
  margin: 12px 0 4px;
  color: var(--el-text-color-secondary);
  font-size: 12px;
  font-weight: 600;
}

.version {
  display: flex;
  flex-direction: column;
  gap: 4px;
  width: 100%;
  padding: 8px;
  border: 1px solid transparent;
  border-radius: 6px;
  background: none;
  text-align: left;
  cursor: pointer;
}

.version:hover {
  background: var(--el-fill-color-lighter);
}

.version.active {
  border-color: var(--el-color-primary-light-5);
  background: var(--el-color-primary-light-9);
}

.line1 {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
  font-size: 13px;
}

.time {
  color: var(--el-text-color-secondary);
  font-variant-numeric: tabular-nums;
}

.actor {
  font-weight: 500;
}

.latest {
  color: var(--el-color-success);
  font-size: 12px;
}

.summary {
  display: -webkit-box;
  overflow: hidden;
  color: var(--el-text-color-regular);
  font-size: 12px;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 2;
}

.legacy {
  margin-top: 12px;
}

@media (max-width: 760px) {
  .layout {
    flex-direction: column-reverse;
  }

  .versions {
    width: auto;
    max-height: 220px;
    padding-left: 0;
    border-left: none;
    border-bottom: 1px solid var(--el-border-color-lighter);
  }
}
</style>
