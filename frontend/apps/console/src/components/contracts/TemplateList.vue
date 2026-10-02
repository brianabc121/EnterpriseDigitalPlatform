<script setup lang="ts">
import { errorMessage } from '@edp/api-client'
import { ElMessage } from 'element-plus'
import { onMounted, ref, watch } from 'vue'

import { api, formatDateTime } from '../../api'
import type { TemplateSummary } from '../../contracts'

/**
 * 合同模板（§34.2、§34.5）：名称、分类、填写项数、使用次数、上传的原件、更新时间；按左侧的分类（包含
 * 下级分类）、状态筛选，按名称搜索。点一行打开模板。
 */
const props = defineProps<{ categoryId: string | null; refreshKey: number }>()
const emit = defineEmits<{ open: [id: string] }>()

const PAGE_SIZE = 20
const status = ref<'' | 'active' | 'disabled'>('')
const q = ref('')
const page = ref(1)
const items = ref<TemplateSummary[]>([])
const total = ref(0)
const loading = ref(false)

async function load(): Promise<void> {
  loading.value = true
  const { data, error } = await api.GET('/api/v1/contracts/templates', {
    params: {
      query: {
        category_id: props.categoryId ?? undefined,
        status: status.value || undefined,
        q: q.value.trim() || undefined,
        limit: PAGE_SIZE,
        offset: (page.value - 1) * PAGE_SIZE,
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

function search(): void {
  if (page.value === 1) void load()
  else page.value = 1
}

/** 上传的原件：5 分钟有效的下载地址。 */
async function download(row: TemplateSummary): Promise<void> {
  const { data, error } = await api.GET('/api/v1/contracts/templates/{contract_template_id}/file', {
    params: { path: { contract_template_id: row.id } },
  })
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  window.open(data.url, '_blank', 'noopener')
}

watch([status, () => props.categoryId], search)
watch(page, load)
watch(() => props.refreshKey, load)
onMounted(load)
</script>

<template>
  <div>
    <div class="filters">
      <el-select v-model="status" clearable placeholder="状态" size="small" class="filter">
        <el-option label="启用" value="active" />
        <el-option label="停用" value="disabled" />
      </el-select>
      <el-input
        v-model="q"
        clearable
        placeholder="模板名称"
        size="small"
        class="search"
        data-testid="template-search"
        @keyup.enter="search"
        @clear="search"
      />
    </div>

    <el-table
      v-loading="loading"
      :data="items"
      row-key="id"
      class="table"
      empty-text="还没有模板：上传 Word、PDF 或者新建一个"
      data-testid="templates-table"
      @row-click="(row: TemplateSummary) => emit('open', row.id)"
    >
      <el-table-column label="名称" min-width="200">
        <template #default="{ row }">
          <div class="name" data-testid="template-row-name">{{ row.name }}</div>
          <div v-if="row.description" class="muted ellipsis">{{ row.description }}</div>
        </template>
      </el-table-column>
      <el-table-column label="分类" min-width="130">
        <template #default="{ row }">
          <span v-if="row.category_path">{{ row.category_path }}</span>
          <span v-else class="muted">未分类</span>
        </template>
      </el-table-column>
      <el-table-column label="填写项" width="80" align="right">
        <template #default="{ row }">{{ row.field_count }}</template>
      </el-table-column>
      <el-table-column label="使用次数" width="90" align="right">
        <template #default="{ row }">
          <span data-testid="template-row-used">{{ row.used_count }}</span>
        </template>
      </el-table-column>
      <el-table-column label="原件" min-width="140">
        <template #default="{ row }">
          <el-button v-if="row.file_name" link type="primary" size="small" class="file" @click.stop="download(row)">{{
            row.file_name
          }}</el-button>
          <span v-else class="muted">—</span>
        </template>
      </el-table-column>
      <el-table-column label="状态" width="80">
        <template #default="{ row }">
          <el-tag size="small" :type="row.status === 'active' ? 'success' : 'info'" data-testid="template-row-status">{{
            row.status === 'active' ? '启用' : '停用'
          }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="创建人" width="100">
        <template #default="{ row }">{{ row.created_by_name ?? '—' }}</template>
      </el-table-column>
      <el-table-column label="更新时间" width="150">
        <template #default="{ row }">
          <span class="muted">{{ formatDateTime(row.updated_at) }}</span>
        </template>
      </el-table-column>
    </el-table>

    <el-pagination
      v-if="total > PAGE_SIZE"
      v-model:current-page="page"
      :page-size="PAGE_SIZE"
      :total="total"
      layout="total, prev, pager, next"
      class="pager"
    />
  </div>
</template>

<style scoped>
.filters {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 12px;
}

.filter {
  width: 110px;
}

.search {
  width: 220px;
}

.table :deep(.el-table__row) {
  cursor: pointer;
}

.name {
  font-weight: 500;
}

.ellipsis,
.file {
  display: block;
  max-width: 100%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.muted {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.pager {
  justify-content: flex-end;
  margin-top: 12px;
}
</style>
