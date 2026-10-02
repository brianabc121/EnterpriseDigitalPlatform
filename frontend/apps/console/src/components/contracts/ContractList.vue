<script setup lang="ts">
import { errorMessage } from '@edp/api-client'
import { ElMessage } from 'element-plus'
import { onMounted, ref, watch } from 'vue'

import { api } from '../../api'
import {
  amountText,
  EXPIRY_LABEL,
  STATUS_LABEL,
  STATUS_TAG,
  VIEWS,
  type ContractPage,
  type ContractSummary,
  type ContractView,
} from '../../contracts'

/**
 * 合同列表（§34.5）：按状态页签（快到期、已到期是已签署的合同按结束日期算的）、左侧的分类（包含下级分类）
 * 筛选，按名称、编号、客户搜索；最近修改的在前，点一行打开合同。
 */
const props = defineProps<{ categoryId: string | null; refreshKey: number }>()
const emit = defineEmits<{ open: [id: string] }>()

const PAGE_SIZE = 20
const view = ref<ContractView>('all')
const q = ref('')
const page = ref(1)
const result = ref<ContractPage | null>(null)
const loading = ref(false)

async function load(): Promise<void> {
  loading.value = true
  const { data, error } = await api.GET('/api/v1/contracts', {
    params: {
      query: {
        view: view.value,
        category_id: props.categoryId ?? undefined,
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
  result.value = data
}

function search(): void {
  if (page.value === 1) void load()
  else page.value = 1
}

function viewLabel(name: ContractView, label: string): string {
  const count = result.value?.counts[name]
  return count ? `${label} ${count}` : label
}

watch([view, () => props.categoryId], search)
watch(page, load)
watch(() => props.refreshKey, load)
onMounted(load)
</script>

<template>
  <div>
    <div class="filters">
      <el-radio-group v-model="view" size="small" data-testid="contract-views">
        <el-radio-button v-for="[name, label] in VIEWS" :key="name" :value="name" :data-testid="`contract-view-${name}`">
          {{ viewLabel(name, label) }}
        </el-radio-button>
      </el-radio-group>
      <el-input
        v-model="q"
        clearable
        placeholder="名称、编号或客户"
        class="search"
        size="small"
        data-testid="contract-search"
        @keyup.enter="search"
        @clear="search"
      />
    </div>

    <el-table
      v-loading="loading"
      :data="result?.items ?? []"
      row-key="id"
      class="table"
      empty-text="暂无合同"
      data-testid="contracts-table"
      @row-click="(row: ContractSummary) => emit('open', row.id)"
    >
      <el-table-column prop="no" label="编号" width="150" />
      <el-table-column label="名称" min-width="200">
        <template #default="{ row }">
          <div class="title" data-testid="contract-row-title">{{ row.title }}</div>
          <div class="flags">
            <span class="muted category">{{ row.category_path || '未分类' }}</span>
            <el-tag v-if="row.ai_generated" size="small" type="info" effect="plain">AI 起草</el-tag>
            <el-tag
              v-if="row.status === 'draft' && row.missing.length"
              size="small"
              type="warning"
              effect="plain"
              data-testid="contract-row-missing"
              >{{ row.missing.length }} 项待填写</el-tag
            >
          </div>
        </template>
      </el-table-column>
      <el-table-column label="客户" min-width="130">
        <template #default="{ row }">
          <div>{{ row.customer_name ?? '—' }}</div>
          <div v-if="row.order_no" class="muted">订单 {{ row.order_no }}</div>
        </template>
      </el-table-column>
      <el-table-column label="金额" width="110" align="right">
        <template #default="{ row }">{{ amountText(row.amount) }}</template>
      </el-table-column>
      <el-table-column label="状态" width="120">
        <template #default="{ row }: { row: ContractSummary }">
          <el-tag size="small" :type="STATUS_TAG[row.status]" data-testid="contract-row-status">{{
            STATUS_LABEL[row.status]
          }}</el-tag>
          <el-tag
            v-if="row.expiry"
            size="small"
            :type="row.expiry === 'expired' ? 'danger' : 'warning'"
            effect="plain"
            class="expiry"
            data-testid="contract-row-expiry"
            >{{ EXPIRY_LABEL[row.expiry] }}</el-tag
          >
        </template>
      </el-table-column>
      <el-table-column label="负责人" width="90">
        <template #default="{ row }">{{ row.owner_name ?? '—' }}</template>
      </el-table-column>
      <el-table-column label="签订 / 结束" width="110">
        <template #default="{ row }">
          <div>{{ row.sign_date ?? '—' }}</div>
          <div class="muted">{{ row.end_date ? `至 ${row.end_date}` : '' }}</div>
        </template>
      </el-table-column>
    </el-table>

    <el-pagination
      v-if="(result?.total ?? 0) > PAGE_SIZE"
      v-model:current-page="page"
      :page-size="PAGE_SIZE"
      :total="result?.total ?? 0"
      layout="total, prev, pager, next"
      class="pager"
    />
  </div>
</template>

<style scoped>
.filters {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  margin-bottom: 12px;
}

.search {
  width: 220px;
}

.table :deep(.el-table__row) {
  cursor: pointer;
}

.title {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.flags {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 4px;
  margin-top: 2px;
}

.category {
  margin-right: 2px;
}

.expiry {
  margin-left: 4px;
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
