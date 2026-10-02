<script setup lang="ts">
import { errorMessage } from '@edp/api-client'
import { ElMessage, ElMessageBox } from 'element-plus'
import { ref } from 'vue'

import { api, formatDateTime } from '../../api'
import {
  age,
  FINDING_STATUS,
  IGNORE_OPTIONS,
  SEVERITY_LABEL,
  SEVERITY_TAG,
  type Finding,
} from '../../wake'

/**
 * AI 巡检发现的问题（§33.4）：级别、分类、标题（点开对应的订单、单据、待办）、说明、负责人、已经
 * 持续多久；负责人和管理员可以忽略或标记已处理。首页"需要我处理的问题"和"AI 唤醒"页面共用。
 */
withDefaults(
  defineProps<{ items: Finding[]; showAssignees?: boolean; emptyText?: string }>(),
  { showAssignees: true, emptyText: '没有需要处理的问题' },
)
const emit = defineEmits<{ changed: [finding: Finding] }>()

const acting = ref<string | null>(null)

async function ignore(finding: Finding, days: number | null): Promise<void> {
  let note: string
  try {
    const result = await ElMessageBox.prompt(
      days === null
        ? '问题消除之前不再提醒。可以写一下原因（选填）。'
        : `${days} 天内不再提醒，到期后仍然存在会重新提醒。可以写一下原因（选填）。`,
      '忽略这个问题',
      { confirmButtonText: '忽略', cancelButtonText: '取消', inputPlaceholder: '原因' },
    )
    note = (result as { value: string }).value ?? ''
  } catch {
    return
  }
  acting.value = finding.id
  const { data, error } = await api.POST('/api/v1/wake/findings/{finding_id}/ignore', {
    params: { path: { finding_id: finding.id } },
    body: { days, note: note.trim() || null },
  })
  acting.value = null
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  ElMessage.success('已忽略')
  emit('changed', data)
}

async function resolve(finding: Finding): Promise<void> {
  acting.value = finding.id
  const { data, error } = await api.POST('/api/v1/wake/findings/{finding_id}/resolve', {
    params: { path: { finding_id: finding.id } },
    body: { note: null },
  })
  acting.value = null
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  ElMessage.success('已标记处理，下次检查仍然发现会重新提醒')
  emit('changed', data)
}

function names(finding: Finding): string {
  return finding.assignees.map((a) => a.name).join('、') || '管理员'
}
</script>

<template>
  <ul class="findings" data-testid="wake-findings">
    <li v-if="!items.length" class="empty">{{ emptyText }}</li>
    <li
      v-for="finding in items"
      :key="finding.id"
      class="finding"
      :class="finding.status"
      data-testid="wake-finding"
      :data-check="finding.check_code"
    >
      <div class="head">
        <el-tag size="small" :type="SEVERITY_TAG[finding.severity]" effect="dark">
          {{ SEVERITY_LABEL[finding.severity] }}
        </el-tag>
        <el-tag size="small" effect="plain" type="info">{{ finding.category_label }}</el-tag>
        <router-link
          v-if="finding.link"
          :to="finding.link"
          class="title"
          data-testid="wake-finding-title"
          >{{ finding.title }}</router-link
        >
        <span v-else class="title" data-testid="wake-finding-title">{{ finding.title }}</span>
        <el-tag v-if="finding.escalated_at" size="small" type="danger" effect="plain">已升级</el-tag>
        <el-tag v-if="finding.status !== 'open'" size="small" type="info" data-testid="wake-finding-status">
          {{ FINDING_STATUS[finding.status] }}
        </el-tag>
      </div>
      <div v-if="finding.detail" class="detail">{{ finding.detail }}</div>
      <div class="meta">
        <span>{{ age(finding) }}</span>
        <span v-if="showAssignees">负责：{{ names(finding) }}</span>
        <span>{{ finding.check_title }}</span>
        <span v-if="finding.status === 'ignored'">
          {{ finding.ignored_until ? `忽略到 ${formatDateTime(finding.ignored_until)}` : '一直忽略' }}
          {{ finding.ignore_note ? `（${finding.ignore_note}）` : '' }}
        </span>
        <span v-if="finding.status === 'resolved' && finding.resolve_note">
          {{ finding.resolved_by ? `${finding.resolved_by.name}：` : '' }}{{ finding.resolve_note }}
        </span>
        <span v-if="finding.can_handle && finding.status !== 'resolved'" class="actions">
          <el-button
            link
            type="primary"
            size="small"
            :loading="acting === finding.id"
            data-testid="wake-resolve"
            @click="resolve(finding)"
            >已处理</el-button
          >
          <el-dropdown
            v-if="finding.status === 'open'"
            trigger="click"
            @command="(days: number | null) => ignore(finding, days)"
          >
            <el-button link size="small" data-testid="wake-ignore">忽略</el-button>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item
                  v-for="[days, label] in IGNORE_OPTIONS"
                  :key="String(days)"
                  :command="days"
                  :data-testid="`wake-ignore-${days ?? 'forever'}`"
                  >{{ label }}</el-dropdown-item
                >
              </el-dropdown-menu>
            </template>
          </el-dropdown>
        </span>
      </div>
    </li>
  </ul>
</template>

<style scoped>
.findings {
  margin: 0;
  padding: 0;
  list-style: none;
  background: var(--el-bg-color);
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 8px;
}

.finding {
  padding: 10px 14px;
  border-bottom: 1px solid var(--el-border-color-lighter);
}

.finding:last-child {
  border-bottom: none;
}

.finding.resolved .title,
.finding.ignored .title {
  color: var(--el-text-color-secondary);
}

.empty {
  padding: 14px;
  text-align: center;
  color: var(--el-text-color-secondary);
  font-size: 13px;
}

.head {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
}

.title {
  flex: 1;
  min-width: 160px;
  font-weight: 500;
  color: var(--el-text-color-primary);
  text-decoration: none;
}

a.title:hover {
  color: var(--el-color-primary);
}

.detail {
  margin-top: 4px;
  font-size: 13px;
  color: var(--el-text-color-regular);
  word-break: break-word;
}

.meta {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 4px 14px;
  margin-top: 4px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.actions {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-left: auto;
}
</style>
