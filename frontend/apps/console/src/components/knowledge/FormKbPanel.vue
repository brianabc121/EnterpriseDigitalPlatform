<script setup lang="ts">
import { errorMessage, type Schemas } from '@edp/api-client'
import { ElMessage } from 'element-plus'
import { computed, onMounted, reactive, ref, watch } from 'vue'

import { api, formatDateTime } from '../../api'
import {
  ACTION_LABEL,
  FORM_LABEL,
  KIND_LABEL,
  SOURCE_LABEL,
  STATUS_LABEL,
  actionTag,
  evidenceText,
  eventText,
  kindText,
  recordSummary,
  statusTag,
  type FormKbAction,
  type FormKbEntry,
  type FormKbForm,
  type FormKbKind,
  type FormKbRecord,
  type FormKbStatus,
} from '../../formkb'
import { useAuthStore } from '../../stores/auth'
import FormKbEntryDialog from './FormKbEntryDialog.vue'
import FormKbEntryDrawer from './FormKbEntryDrawer.vue'

/**
 * 表单知识（设计文档 §25.18）：员工每次提交订单、领料单、入库单，系统判断要不要更新这里的知识
 * （叫法、用量、搭配）；生效的知识用在开单时的联想和一键领料。也可以手工填写。
 */
const PAGE_SIZE = 20
const auth = useAuthStore()
const canManage = computed(() => auth.can('form_kb:manage'))

const view = ref<'entries' | 'records'>('entries')
const summary = ref<Schemas['FormKbSummary'] | null>(null)
const settings = ref<Schemas['FormKbSettingsOut'] | null>(null)
const settingsOpen = ref(false)
const settingsForm = reactive({
  auto_activate: true,
  learn_aliases: true,
  learn_usage: true,
  learn_companions: true,
})
const savingSettings = ref(false)
const createOpen = ref(false)
const openId = ref<string | null>(null)

// 知识
const entries = ref<FormKbEntry[]>([])
const entryTotal = ref(0)
const entryPage = ref(1)
const loadingEntries = ref(false)
const filters = reactive({
  kind: '' as FormKbKind | '',
  status: '' as FormKbStatus | '',
  source: '' as 'learned' | 'manual' | '',
  review: false,
  q: '',
})

// 学习记录
const records = ref<FormKbRecord[]>([])
const recordTotal = ref(0)
const recordPage = ref(1)
const loadingRecords = ref(false)
const recordForm = ref<FormKbForm | ''>('')
const changedOnly = ref(false)

async function loadSummary(): Promise<void> {
  const { data } = await api.GET('/api/v1/form-kb/summary')
  if (data) summary.value = data
}

async function loadEntries(): Promise<void> {
  loadingEntries.value = true
  const { data, error } = await api.GET('/api/v1/form-kb/entries', {
    params: {
      query: {
        kind: filters.kind || undefined,
        status: filters.status || undefined,
        source: filters.source || undefined,
        review: filters.review || undefined,
        q: filters.q.trim() || undefined,
        limit: PAGE_SIZE,
        offset: (entryPage.value - 1) * PAGE_SIZE,
      },
    },
  })
  loadingEntries.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  entries.value = data.items
  entryTotal.value = data.total
}

async function loadRecords(): Promise<void> {
  loadingRecords.value = true
  const { data, error } = await api.GET('/api/v1/form-kb/submissions', {
    params: {
      query: {
        form: recordForm.value || undefined,
        changed: changedOnly.value,
        limit: PAGE_SIZE,
        offset: (recordPage.value - 1) * PAGE_SIZE,
      },
    },
  })
  loadingRecords.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  records.value = data.items
  recordTotal.value = data.total
}

function reloadEntries(): void {
  entryPage.value = 1
  void loadEntries()
}

function reloadRecords(): void {
  recordPage.value = 1
  void loadRecords()
}

/** 点数量：只看这一类（还没判断的在学习记录里）。 */
function pick(which: FormKbStatus | 'review' | 'pending'): void {
  if (which === 'pending') {
    recordForm.value = ''
    changedOnly.value = false
    recordPage.value = 1
    if (view.value === 'records') void loadRecords()
    else view.value = 'records'
    return
  }
  Object.assign(filters, {
    status: which === 'review' ? '' : which,
    review: which === 'review',
    kind: '',
    source: '',
    q: '',
  })
  entryPage.value = 1
  if (view.value === 'entries') void loadEntries()
  else view.value = 'entries'
}

function changed(): void {
  void loadSummary()
  if (view.value === 'entries') void loadEntries()
  else void loadRecords()
}

function created(entry: FormKbEntry): void {
  openId.value = entry.id
  changed()
}

async function openSettings(): Promise<void> {
  const { data, error } = await api.GET('/api/v1/form-kb/settings')
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  settings.value = data
  Object.assign(settingsForm, {
    auto_activate: data.auto_activate,
    learn_aliases: data.learn_aliases,
    learn_usage: data.learn_usage,
    learn_companions: data.learn_companions,
  })
  settingsOpen.value = true
}

async function saveSettings(): Promise<void> {
  savingSettings.value = true
  const { data, error } = await api.PUT('/api/v1/form-kb/settings', { body: { ...settingsForm } })
  savingSettings.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  settings.value = data
  settingsOpen.value = false
  ElMessage.success('已保存')
}

watch(view, (value) => {
  void loadSummary()
  if (value === 'records') void loadRecords()
  else void loadEntries()
})
onMounted(() => Promise.all([loadSummary(), loadEntries()]))
</script>

<template>
  <div data-testid="formkb-panel">
    <p class="intro">
      员工每次提交订单、领料单、入库单，系统都会判断要不要更新这里的知识：常用的叫法对应哪个商品、成品每件用多少材料、
      哪些商品常一起开。生效的知识用在开单时的联想（标“学到的”）和一键领料的预填。
    </p>
    <div class="bar">
      <div class="counts" data-testid="formkb-summary">
        <button type="button" class="count" data-testid="formkb-count-active" @click="pick('active')">
          生效 <b>{{ summary?.active ?? 0 }}</b>
        </button>
        <button type="button" class="count" data-testid="formkb-count-observing" @click="pick('observing')">
          观察中 <b>{{ summary?.observing ?? 0 }}</b>
        </button>
        <button
          type="button"
          class="count"
          :class="{ warn: summary?.review }"
          data-testid="formkb-count-review"
          @click="pick('review')"
        >
          待确认 <b>{{ summary?.review ?? 0 }}</b>
        </button>
        <button type="button" class="count" data-testid="formkb-count-disabled" @click="pick('disabled')">
          已停用 <b>{{ summary?.disabled ?? 0 }}</b>
        </button>
        <button type="button" class="count" data-testid="formkb-count-pending" @click="pick('pending')">
          还没判断 <b>{{ summary?.pending ?? 0 }}</b>
        </button>
      </div>
      <div class="buttons">
        <el-radio-group v-model="view" size="small" data-testid="formkb-view">
          <el-radio-button value="entries">知识</el-radio-button>
          <el-radio-button value="records">学习记录</el-radio-button>
        </el-radio-group>
        <template v-if="canManage">
          <el-button size="small" data-testid="formkb-settings" @click="openSettings">设置</el-button>
          <el-button size="small" type="primary" data-testid="formkb-create" @click="createOpen = true">
            新增知识
          </el-button>
        </template>
      </div>
    </div>

    <template v-if="view === 'entries'">
      <div class="filters">
        <el-select
          v-model="filters.kind"
          size="small"
          class="narrow"
          placeholder="全部类型"
          data-testid="formkb-kind-filter"
          @change="reloadEntries"
        >
          <el-option label="全部类型" value="" />
          <el-option v-for="(text, value) in KIND_LABEL" :key="value" :label="text" :value="value" />
        </el-select>
        <el-radio-group
          v-model="filters.status"
          size="small"
          data-testid="formkb-status-filter"
          @change="reloadEntries"
        >
          <el-radio-button value="">全部</el-radio-button>
          <el-radio-button v-for="(text, value) in STATUS_LABEL" :key="value" :value="value">
            {{ text }}
          </el-radio-button>
        </el-radio-group>
        <el-select
          v-model="filters.source"
          size="small"
          class="narrow"
          placeholder="全部来源"
          @change="reloadEntries"
        >
          <el-option label="全部来源" value="" />
          <el-option v-for="(text, value) in SOURCE_LABEL" :key="value" :label="text" :value="value" />
        </el-select>
        <el-checkbox v-model="filters.review" data-testid="formkb-review-filter" @change="reloadEntries">
          待确认
        </el-checkbox>
        <el-input
          v-model="filters.q"
          size="small"
          class="keyword"
          placeholder="搜索商品名称、代码或叫法"
          clearable
          maxlength="64"
          data-testid="formkb-keyword"
          @keyup.enter="reloadEntries"
          @clear="reloadEntries"
        />
      </div>
      <el-table
        v-loading="loadingEntries"
        :data="entries"
        class="clickable"
        empty-text="还没有表单知识：员工提交订单、领料单后会自动学习，也可以点“新增知识”手工填写"
        data-testid="formkb-table"
        @row-click="(row: FormKbEntry) => (openId = row.id)"
      >
        <el-table-column label="知识" min-width="320">
          <template #default="{ row }">
            <el-tag size="small" type="info" class="kind">{{ kindText(row) }}</el-tag>
            <span>{{ row.sentence }}</span>
            <el-tag v-if="row.review" size="small" type="warning" class="flag">待确认</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="130">
          <template #default="{ row }">
            <el-tag size="small" :type="statusTag(row.status)">{{ STATUS_LABEL[row.status as FormKbStatus] }}</el-tag>
            <el-tag v-if="row.locked" size="small" type="warning" effect="plain" class="flag">固定</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="依据" width="150">
          <template #default="{ row }">{{ evidenceText(row) }}</template>
        </el-table-column>
        <el-table-column label="开单时用到" width="100" align="right">
          <template #default="{ row }">{{ row.hits }} 次</template>
        </el-table-column>
        <el-table-column label="更新时间" width="160">
          <template #default="{ row }">{{ formatDateTime(row.updated_at) }}</template>
        </el-table-column>
      </el-table>
      <div class="page-footer">
        <el-pagination
          v-model:current-page="entryPage"
          :page-size="PAGE_SIZE"
          :total="entryTotal"
          layout="total, prev, pager, next"
          @current-change="loadEntries"
        />
      </div>
    </template>

    <template v-else>
      <div class="filters">
        <el-select
          v-model="recordForm"
          size="small"
          class="narrow"
          placeholder="全部表单"
          data-testid="formkb-record-form"
          @change="reloadRecords"
        >
          <el-option label="全部表单" value="" />
          <el-option v-for="(text, value) in FORM_LABEL" :key="value" :label="text" :value="value" />
        </el-select>
        <el-switch
          v-model="changedOnly"
          active-text="只看有更新的"
          data-testid="formkb-changed-only"
          @change="reloadRecords"
        />
        <el-button size="small" link type="primary" @click="loadRecords">刷新</el-button>
      </div>
      <el-table
        v-loading="loadingRecords"
        :data="records"
        empty-text="还没有学习记录：员工提交订单、领料单、入库单后出现在这里"
        data-testid="formkb-records"
      >
        <el-table-column label="时间" width="160">
          <template #default="{ row }">{{ formatDateTime(row.created_at) }}</template>
        </el-table-column>
        <el-table-column label="表单" width="190">
          <template #default="{ row }">
            {{ FORM_LABEL[row.form as FormKbForm] }} {{ row.record_no }}
            <div class="muted">{{ eventText(row) }} · {{ row.actor_name ?? '—' }}</div>
          </template>
        </el-table-column>
        <el-table-column label="判断结果" min-width="320">
          <template #default="{ row }">
            <div :class="['summary', row.status]">{{ recordSummary(row) }}</div>
            <div v-for="(r, index) in row.result" :key="index" class="result">
              <el-tag size="small" :type="actionTag(r.action)">{{ ACTION_LABEL[r.action as FormKbAction] }}</el-tag>
              <el-button
                v-if="r.entry_id"
                link
                type="primary"
                size="small"
                class="result-text"
                data-testid="formkb-record-entry"
                @click="openId = r.entry_id"
              >
                {{ r.text }}
              </el-button>
              <span v-else class="result-text">{{ r.text }}</span>
            </div>
          </template>
        </el-table-column>
      </el-table>
      <div class="page-footer">
        <el-pagination
          v-model:current-page="recordPage"
          :page-size="PAGE_SIZE"
          :total="recordTotal"
          layout="total, prev, pager, next"
          @current-change="loadRecords"
        />
      </div>
    </template>

    <FormKbEntryDrawer
      :entry-id="openId"
      :can-manage="canManage"
      @close="openId = null"
      @changed="changed"
    />
    <FormKbEntryDialog v-model="createOpen" :entry="null" @saved="created" />

    <el-dialog v-model="settingsOpen" title="表单知识设置" width="520px" append-to-body>
      <el-form label-width="0" data-testid="formkb-settings-form">
        <el-form-item>
          <el-switch v-model="settingsForm.auto_activate" data-testid="formkb-auto-activate" />
          <span class="setting">学到的知识达到条件后自动生效</span>
          <div class="setting-hint">关闭后，达到条件的知识标“待确认”，确认后才用在开单时。</div>
        </el-form-item>
        <el-form-item>
          <el-switch v-model="settingsForm.learn_aliases" />
          <span class="setting">学习叫法</span>
          <div class="setting-hint">员工输入的文字（例如“大窗”）最后选了哪个商品；同一叫法多次选同一商品后，联想里排在最前面。</div>
        </el-form-item>
        <el-form-item>
          <el-switch v-model="settingsForm.learn_usage" />
          <span class="setting">学习用量</span>
          <div class="setting-hint">仓管确认的领料单：成品每件实际用多少材料；没有配方时用于一键领料预填，和配方不一致时提示更新配方。</div>
        </el-form-item>
        <el-form-item>
          <el-switch v-model="settingsForm.learn_companions" />
          <span class="setting">学习搭配</span>
          <div class="setting-hint">同一张单据里常一起开的商品；录入行还没输入时，联想里先列常一起开的。</div>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="settingsOpen = false">取消</el-button>
        <el-button
          type="primary"
          :loading="savingSettings"
          :disabled="!settings?.can_edit"
          data-testid="formkb-settings-save"
          @click="saveSettings"
        >
          保存
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.intro {
  margin: 0 0 12px;
  font-size: 13px;
  line-height: 1.6;
  color: var(--el-text-color-secondary);
}

.bar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 12px;
}

.counts,
.buttons {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
}

.count {
  padding: 4px 10px;
  font-size: 13px;
  color: var(--el-text-color-regular);
  cursor: pointer;
  background: var(--el-fill-color-light);
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 14px;
}

.count:hover {
  border-color: var(--el-color-primary-light-5);
}

.count b {
  margin-left: 2px;
  color: var(--el-text-color-primary);
}

.count.warn b {
  color: var(--el-color-warning);
}

.filters {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 12px;
  margin-bottom: 12px;
}

.narrow {
  width: 120px;
}

.keyword {
  width: 220px;
}

.kind {
  margin-right: 6px;
}

.flag {
  margin-left: 6px;
}

.muted {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.summary {
  font-size: 13px;
}

.summary.pending,
.summary.done {
  color: var(--el-text-color-secondary);
}

.summary.failed {
  color: var(--el-color-danger);
}

.result {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-top: 4px;
}

.result-text {
  font-size: 13px;
  white-space: normal;
  text-align: left;
}

.setting {
  margin-left: 8px;
}

.setting-hint {
  width: 100%;
  font-size: 12px;
  line-height: 1.5;
  color: var(--el-text-color-secondary);
}

.clickable :deep(.el-table__row) {
  cursor: pointer;
}
</style>
