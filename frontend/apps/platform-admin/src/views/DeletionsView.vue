<script setup lang="ts">
import { errorMessage, type Schemas } from '@edp/api-client'
import { ElMessage } from 'element-plus'
import { onMounted, ref } from 'vue'

import { api, formatDateTime } from '../api'

const items = ref<Schemas['TenantDeletionOut'][]>([])
const loading = ref(false)

async function load(): Promise<void> {
  loading.value = true
  const { data, error } = await api.GET('/platform/v1/deletions')
  loading.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  items.value = data.items
}

function copy(record: Schemas['TenantDeletionOut']): void {
  const text = JSON.stringify(record, null, 2)
  void navigator.clipboard?.writeText(text)
  ElMessage.success('已复制删除记录')
}

onMounted(load)
</script>

<template>
  <div>
    <h2>删除记录</h2>
    <p class="sub">
      租户注销后删除数据的记录：各表删除的行数、对象存储删除的文件数与字节数，以及这些明细的 SHA-256
      摘要（可以交给客户核对）。
    </p>
    <el-table v-loading="loading" :data="items" data-testid="deletion-table" empty-text="暂无记录">
      <el-table-column label="租户" min-width="160">
        <template #default="{ row }">{{ row.name }}<span class="sub"> {{ row.code }}</span></template>
      </el-table-column>
      <el-table-column label="申请注销" width="170">
        <template #default="{ row }">{{ row.requested_at ? formatDateTime(row.requested_at) : '—' }}</template>
      </el-table-column>
      <el-table-column label="删除时间" width="170">
        <template #default="{ row }">{{ formatDateTime(row.purged_at) }}</template>
      </el-table-column>
      <el-table-column label="删除内容" width="200">
        <template #default="{ row }">
          <div class="sub">记录 {{ row.counts.rows }} 行，文件 {{ row.counts.objects }} 个</div>
          <div class="sub">IM 群 {{ row.counts.im_groups }} 个</div>
        </template>
      </el-table-column>
      <el-table-column label="SHA-256">
        <template #default="{ row }"><code class="digest">{{ row.digest }}</code></template>
      </el-table-column>
      <el-table-column label="操作" width="80">
        <template #default="{ row }">
          <el-button link type="primary" @click="copy(row)">复制</el-button>
        </template>
      </el-table-column>
    </el-table>
  </div>
</template>

<style scoped>
h2 {
  margin: 0 0 8px;
  font-size: 18px;
}

.digest {
  font-size: 12px;
  word-break: break-all;
}

.sub {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
</style>
