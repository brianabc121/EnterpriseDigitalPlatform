<script setup lang="ts">
import { errorMessage } from '@edp/api-client'
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed, ref, watch } from 'vue'

import { api, formatDateTime } from '../../api'
import {
  ACTION_LABEL,
  BASIS_LABEL,
  FORM_LABEL,
  KIND_LABEL,
  REVIEW_LABEL,
  SOURCE_LABEL,
  STATUS_LABEL,
  actionTag,
  evidenceText,
  kindText,
  reviewDecisions,
  statusTag,
  type FormKbDetail,
  type FormKbForm,
} from '../../formkb'
import FormKbEntryDialog from './FormKbEntryDialog.vue'

/**
 * 一条表单知识（设计文档 §25.18）：写成一句话的知识、依据的单据、变化记录；可以处理待确认、修改、
 * 停用、固定，用量还可以写进配方。
 */
const props = defineProps<{ entryId: string | null; canManage: boolean }>()
const emit = defineEmits<{ close: []; changed: [] }>()

const entry = ref<FormKbDetail | null>(null)
const loading = ref(false)
const busy = ref(false)
const editOpen = ref(false)

const open = computed({
  get: () => props.entryId !== null,
  set: (value) => {
    if (!value) emit('close')
  },
})
const decisions = computed(() => reviewDecisions(entry.value?.review ?? null))
const editable = computed(() => entry.value !== null && entry.value.kind !== 'companion')

async function load(id: string): Promise<void> {
  loading.value = true
  const { data, error } = await api.GET('/api/v1/form-kb/entries/{entry_id}', {
    params: { path: { entry_id: id } },
  })
  loading.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    emit('close')
    return
  }
  entry.value = data
}

watch(
  () => props.entryId,
  (id) => {
    entry.value = null
    if (id) void load(id)
  },
  { immediate: true },
)

function updated(data: FormKbDetail, message: string): void {
  entry.value = data
  ElMessage.success(message)
  emit('changed')
}

async function act(action: 'enable' | 'disable' | 'lock' | 'unlock'): Promise<void> {
  if (!entry.value) return
  busy.value = true
  const path = `/api/v1/form-kb/entries/{entry_id}/${action}` as const
  const { data, error } = await api.POST(path, { params: { path: { entry_id: entry.value.id } } })
  busy.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  const done = { enable: '已启用', disable: '已停用', lock: '已固定', unlock: '已取消固定' }
  updated(data, done[action])
}

async function decide(decision: 'activate' | 'adopt' | 'keep', label: string): Promise<void> {
  if (!entry.value) return
  busy.value = true
  const { data, error } = await api.POST('/api/v1/form-kb/entries/{entry_id}/confirm', {
    params: { path: { entry_id: entry.value.id } },
    body: { decision },
  })
  busy.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  updated(data, `已处理：${label}`)
}

async function applyRecipe(): Promise<void> {
  const e = entry.value
  if (!e) return
  try {
    await ElMessageBox.confirm(
      `${e.product.name} 的配方：${e.related?.name ?? '这种材料'}改为每${e.product.unit || '件'} ${e.value}${e.related?.unit ?? ''}？以后一键领料按配方预填。`,
      '写进配方',
      { confirmButtonText: '写进配方', cancelButtonText: '取消', type: 'warning' },
    )
  } catch {
    return
  }
  busy.value = true
  const { data, error } = await api.POST('/api/v1/form-kb/entries/{entry_id}/apply-recipe', {
    params: { path: { entry_id: e.id } },
  })
  busy.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  updated(data, '已写进配方')
}

async function remove(): Promise<void> {
  const e = entry.value
  if (!e) return
  try {
    await ElMessageBox.confirm(`删除这条知识？\n${e.sentence}`, '删除表单知识', {
      confirmButtonText: '删除',
      cancelButtonText: '取消',
      type: 'warning',
    })
  } catch {
    return
  }
  const { error } = await api.DELETE('/api/v1/form-kb/entries/{entry_id}', {
    params: { path: { entry_id: e.id } },
  })
  if (error) {
    ElMessage.error(errorMessage(error))
    return
  }
  ElMessage.success('已删除')
  emit('changed')
  emit('close')
}

function onSaved(data: FormKbDetail): void {
  entry.value = data
  emit('changed')
}
</script>

<template>
  <el-drawer
    v-model="open"
    :title="entry ? `${KIND_LABEL[entry.kind]} · 表单知识` : '表单知识'"
    size="620px"
    append-to-body
  >
    <div v-loading="loading" data-testid="formkb-drawer">
      <template v-if="entry">
        <p class="sentence" data-testid="formkb-drawer-sentence">{{ entry.sentence }}</p>
        <div class="tags">
          <el-tag size="small" type="info">{{ kindText(entry) }}</el-tag>
          <el-tag size="small" :type="statusTag(entry.status)" data-testid="formkb-drawer-status">
            {{ STATUS_LABEL[entry.status] }}
          </el-tag>
          <el-tag size="small" type="info" effect="plain">{{ SOURCE_LABEL[entry.source] }}</el-tag>
          <el-tag v-if="entry.locked" size="small" type="warning" effect="plain">固定</el-tag>
        </div>

        <el-alert
          v-if="entry.review"
          :title="REVIEW_LABEL[entry.review]"
          type="warning"
          :closable="false"
          show-icon
          class="review"
          data-testid="formkb-drawer-review"
        >
          <div v-if="entry.review_note">{{ entry.review_note }}</div>
          <div v-if="canManage" class="review-actions">
            <el-button
              v-if="entry.review === 'recipe' && entry.can_apply_recipe"
              type="primary"
              size="small"
              :loading="busy"
              data-testid="formkb-drawer-recipe"
              @click="applyRecipe"
            >
              更新配方
            </el-button>
            <el-button
              v-for="d in decisions"
              :key="d.decision"
              :type="d.primary ? 'primary' : undefined"
              size="small"
              :loading="busy"
              :data-testid="`formkb-drawer-${d.decision}`"
              @click="decide(d.decision, d.label)"
            >
              {{ d.label }}
            </el-button>
          </div>
        </el-alert>

        <el-descriptions :column="2" size="small" border class="facts">
          <el-descriptions-item label="依据">{{ evidenceText(entry) }}</el-descriptions-item>
          <el-descriptions-item label="开单时用到">
            {{ entry.hits }} 次
            <span v-if="entry.last_hit_at" class="muted">
              （最近 {{ formatDateTime(entry.last_hit_at) }}）
            </span>
          </el-descriptions-item>
          <template v-if="entry.kind === 'usage'">
            <el-descriptions-item label="每件用量">
              {{ entry.value ?? '—' }} {{ entry.related?.unit ?? '' }}
            </el-descriptions-item>
            <el-descriptions-item label="配方里">
              {{ entry.recipe === null ? '没有这种材料' : `${entry.recipe} ${entry.related?.unit ?? ''}` }}
            </el-descriptions-item>
            <el-descriptions-item v-if="entry.basis" label="和配方比" :span="2">
              {{ BASIS_LABEL[entry.basis] }}
            </el-descriptions-item>
          </template>
          <el-descriptions-item label="学到时间">
            {{ entry.learned_at ? formatDateTime(entry.learned_at) : '—' }}
          </el-descriptions-item>
          <el-descriptions-item label="更新时间">
            {{ formatDateTime(entry.updated_at) }}
          </el-descriptions-item>
        </el-descriptions>

        <div v-if="canManage" class="actions">
          <el-button
            v-if="editable"
            size="small"
            data-testid="formkb-drawer-edit"
            @click="editOpen = true"
          >
            修改
          </el-button>
          <el-button
            v-if="entry.status !== 'active' && !entry.review"
            size="small"
            :loading="busy"
            data-testid="formkb-drawer-enable"
            @click="act('enable')"
          >
            启用
          </el-button>
          <el-button
            v-if="entry.status !== 'disabled'"
            size="small"
            :loading="busy"
            data-testid="formkb-drawer-disable"
            @click="act('disable')"
          >
            停用
          </el-button>
          <el-button
            size="small"
            :loading="busy"
            data-testid="formkb-drawer-lock"
            @click="act(entry.locked ? 'unlock' : 'lock')"
          >
            {{ entry.locked ? '取消固定' : '固定' }}
          </el-button>
          <el-button
            v-if="entry.can_apply_recipe && entry.review !== 'recipe' && entry.basis !== 'recipe'"
            size="small"
            :loading="busy"
            data-testid="formkb-drawer-apply"
            @click="applyRecipe"
          >
            写进配方
          </el-button>
          <el-button
            v-if="entry.source === 'manual'"
            size="small"
            type="danger"
            plain
            data-testid="formkb-drawer-delete"
            @click="remove"
          >
            删除
          </el-button>
        </div>

        <h4>依据的单据</h4>
        <el-table
          :data="entry.evidence_items"
          size="small"
          empty-text="手工填写，没有依据的单据"
          data-testid="formkb-drawer-evidence"
        >
          <el-table-column label="时间" width="150">
            <template #default="{ row }">{{ formatDateTime(row.at) }}</template>
          </el-table-column>
          <el-table-column label="单据" width="210">
            <template #default="{ row }">
              {{ FORM_LABEL[row.form as FormKbForm] }} {{ row.record_no }}
            </template>
          </el-table-column>
          <el-table-column label="内容" min-width="180">
            <template #default="{ row }">
              {{ row.detail }}
              <span v-if="row.actor_name" class="muted">· {{ row.actor_name }}</span>
            </template>
          </el-table-column>
        </el-table>

        <h4>变化记录</h4>
        <el-timeline data-testid="formkb-drawer-log">
          <el-timeline-item
            v-for="(item, index) in entry.log"
            :key="index"
            :timestamp="formatDateTime(item.at)"
            :type="actionTag(item.action)"
          >
            <el-tag size="small" :type="actionTag(item.action)">
              {{ ACTION_LABEL[item.action] }}
            </el-tag>
            <span class="note">{{ item.note }}</span>
            <div class="muted">
              {{ item.actor_name ?? '系统学习' }}
              <template v-if="item.record_no">· {{ item.record_no }}</template>
            </div>
          </el-timeline-item>
        </el-timeline>
      </template>
    </div>
    <FormKbEntryDialog v-model="editOpen" :entry="entry" @saved="onSaved" />
  </el-drawer>
</template>

<style scoped>
.sentence {
  margin: 0 0 8px;
  font-size: 15px;
  font-weight: 600;
  line-height: 1.6;
}

.tags {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-bottom: 12px;
}

.review {
  margin-bottom: 12px;
}

.review-actions {
  display: flex;
  gap: 8px;
  margin-top: 8px;
}

.facts {
  margin-bottom: 12px;
}

.actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 4px;
}

.actions .el-button + .el-button {
  margin-left: 0;
}

h4 {
  margin: 16px 0 8px;
}

.note {
  margin-left: 6px;
}

.muted {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
</style>
