<script setup lang="ts">
import { errorMessage } from '@edp/api-client'
import { ElMessage } from 'element-plus'
import { onMounted, ref, watch } from 'vue'

import { api, formatDateTime } from '../../api'
import { canResend, JOB_STATUS_TAG, type PrintJob } from '../../printing'

/**
 * 打印记录（设计文档 §29.6）：时间、小票和单号、打印机、第几次、打印人、来源、状态和错误；失败、放弃的
 * 可以重新发送；点"内容"用等宽字体预览当时打的小票。设置页看全部（可按状态、单号筛选），加工页和
 * 单据详情只看一张单据的。
 */
const props = withDefaults(
  defineProps<{
    kind?: 'order' | 'requisition' | 'test'
    refId?: string
    /** 单据里的简版：不显示筛选和分页。 */
    compact?: boolean
  }>(),
  { kind: undefined, refId: undefined, compact: false },
)

const PAGE_SIZE = 20
const items = ref<PrintJob[]>([])
const total = ref(0)
const page = ref(1)
const status = ref('')
const q = ref('')
const loading = ref(false)
const busy = ref<string | null>(null)
const viewing = ref<PrintJob | null>(null)

async function load(): Promise<void> {
  loading.value = true
  const { data, error } = await api.GET('/api/v1/print/jobs', {
    params: {
      query: {
        kind: props.kind,
        ref_id: props.refId,
        status: (status.value || undefined) as PrintJob['status'] | undefined,
        q: q.value.trim() || undefined,
        limit: props.compact ? 50 : PAGE_SIZE,
        offset: props.compact ? 0 : (page.value - 1) * PAGE_SIZE,
      },
    },
  })
  loading.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  items.value = data.items
  total.value = data.total
}

async function resend(job: PrintJob): Promise<void> {
  busy.value = job.id
  const { data, error } = await api.POST('/api/v1/print/jobs/{print_job_id}/resend', {
    params: { path: { print_job_id: job.id } },
  })
  busy.value = null
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  ElMessage.success('已重新发送')
  await load()
}

function search(): void {
  page.value = 1
  void load()
}

watch(() => [props.kind, props.refId], () => void load())
onMounted(load)
defineExpose({ reload: load })
</script>

<template>
  <div>
    <div v-if="!compact" class="filters">
      <el-select
        v-model="status"
        clearable
        placeholder="全部状态"
        class="status"
        data-testid="print-jobs-status"
        @change="search"
      >
        <el-option label="排队" value="queued" />
        <el-option label="已发送" value="sent" />
        <el-option label="已打印" value="printed" />
        <el-option label="失败重试中" value="retrying" />
        <el-option label="已放弃" value="dead" />
      </el-select>
      <el-input
        v-model="q"
        clearable
        placeholder="单号或打印人"
        class="search"
        data-testid="print-jobs-search"
        @keyup.enter="search"
        @clear="search"
      />
      <el-button data-testid="print-jobs-reload" @click="load">刷新</el-button>
    </div>
    <el-table
      v-loading="loading"
      :data="items"
      :size="compact ? 'small' : undefined"
      data-testid="print-jobs"
      empty-text="还没有打印记录"
    >
      <el-table-column label="时间" width="150">
        <template #default="{ row }">{{ formatDateTime(row.created_at) }}</template>
      </el-table-column>
      <el-table-column v-if="!compact" label="小票" min-width="180">
        <template #default="{ row }">
          <span data-testid="print-job-ticket">{{ row.kind_label }} {{ row.ref_no }}</span>
        </template>
      </el-table-column>
      <el-table-column label="打印机" min-width="110" prop="printer_name" />
      <el-table-column label="第几次" width="80">
        <template #default="{ row }">
          <span data-testid="print-job-seq">第 {{ row.seq }} 次</span>
        </template>
      </el-table-column>
      <el-table-column label="打印人" min-width="90" prop="requested_by_name" />
      <el-table-column label="来源" width="90" prop="source_label" />
      <el-table-column label="状态" min-width="150">
        <template #default="{ row }">
          <el-tag
            :type="JOB_STATUS_TAG[row.status as PrintJob['status']]"
            size="small"
            data-testid="print-job-status"
          >
            {{ row.status_label }}
          </el-tag>
          <div v-if="row.last_error" class="error" data-testid="print-job-error">
            {{ row.last_error }}
          </div>
        </template>
      </el-table-column>
      <el-table-column label="操作" width="150" fixed="right">
        <template #default="{ row }">
          <el-button
            link
            type="primary"
            :data-testid="`print-job-view-${row.seq}`"
            @click="viewing = row"
          >
            内容
          </el-button>
          <el-button
            v-if="canResend(row)"
            link
            type="primary"
            :loading="busy === row.id"
            :data-testid="`print-job-resend-${row.seq}`"
            @click="resend(row)"
          >
            重新发送
          </el-button>
        </template>
      </el-table-column>
    </el-table>
    <el-pagination
      v-if="!compact && total > PAGE_SIZE"
      v-model:current-page="page"
      :page-size="PAGE_SIZE"
      :total="total"
      layout="prev, pager, next"
      class="pager"
      @current-change="load"
    />
    <el-dialog
      :model-value="viewing !== null"
      :title="viewing ? `${viewing.kind_label} ${viewing.ref_no}（第 ${viewing.seq} 次）` : ''"
      width="520px"
      data-testid="print-job-content"
      @update:model-value="viewing = null"
    >
      <pre class="ticket">{{ viewing?.content }}</pre>
    </el-dialog>
  </div>
</template>

<style scoped>
.filters {
  display: flex;
  gap: 8px;
  margin-bottom: 12px;
  flex-wrap: wrap;
}

.status {
  width: 140px;
}

.search {
  width: 200px;
}

.error {
  margin-top: 4px;
  font-size: 12px;
  line-height: 1.4;
  color: var(--el-color-danger);
}

.pager {
  margin-top: 12px;
  justify-content: flex-end;
}

.ticket {
  margin: 0;
  padding: 12px;
  font-family: 'SFMono-Regular', Consolas, 'Liberation Mono', Menlo, monospace;
  font-size: 12px;
  line-height: 1.5;
  white-space: pre;
  overflow-x: auto;
  background: var(--el-fill-color-lighter);
  border-radius: 4px;
}
</style>
