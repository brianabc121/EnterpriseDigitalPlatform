<script setup lang="ts">
import { errorMessage, type Schemas } from '@edp/api-client'
import { ElMessage } from 'element-plus'
import { onMounted, reactive, ref } from 'vue'

import { api, formatDateTime } from '../../api'

const WHAT: Record<string, string> = { sessions: '查看会话列表', messages: '查看会话消息' }

const data = ref<Schemas['SupportGrantList'] | null>(null)
const busy = ref(false)
const form = reactive({ reason: '', hours: 24 })

async function load(): Promise<void> {
  const result = await api.GET('/api/v1/tenant/support-grants')
  if (!result.data) {
    ElMessage.error(errorMessage(result.error))
    return
  }
  data.value = result.data
}

async function grant(): Promise<void> {
  if (!form.reason.trim()) {
    ElMessage.warning('请填写需要平台协助的问题')
    return
  }
  busy.value = true
  const { error } = await api.POST('/api/v1/tenant/support-grants', {
    body: { reason: form.reason.trim(), hours: form.hours },
  })
  busy.value = false
  if (error) {
    ElMessage.error(errorMessage(error))
    return
  }
  form.reason = ''
  ElMessage.success('已授权')
  await load()
}

async function revoke(item: Schemas['SupportGrantOut']): Promise<void> {
  const { error } = await api.DELETE('/api/v1/tenant/support-grants/{grant_id}', {
    params: { path: { grant_id: item.id } },
  })
  if (error) {
    ElMessage.error(errorMessage(error))
    return
  }
  await load()
}

onMounted(load)
</script>

<template>
  <div data-testid="support-tab">
    <p class="sub">
      平台运营人员默认看不到贵企业的业务数据。需要平台协助排查问题时，可以授权一段时间：有效期内平台人员可以只读查看会话和消息，
      每次查看都记录在下方，随时可以撤销。
    </p>
    <el-form inline @submit.prevent="grant">
      <el-form-item label="问题">
        <el-input v-model="form.reason" placeholder="例如：部分消息没有送达" class="reason" data-testid="grant-reason" />
      </el-form-item>
      <el-form-item label="有效期">
        <el-select v-model="form.hours" class="hours">
          <el-option :value="2" label="2 小时" />
          <el-option :value="24" label="1 天" />
          <el-option :value="72" label="3 天" />
          <el-option :value="168" label="7 天" />
        </el-select>
      </el-form-item>
      <el-form-item>
        <el-button type="primary" :loading="busy" data-testid="grant-submit" @click="grant">授权</el-button>
      </el-form-item>
    </el-form>

    <h4>授权记录</h4>
    <el-table :data="data?.items ?? []" size="small" empty-text="没有授权过" data-testid="grant-table">
      <el-table-column prop="reason" label="问题" />
      <el-table-column label="授权时间" width="170">
        <template #default="{ row }">{{ formatDateTime(row.created_at) }}</template>
      </el-table-column>
      <el-table-column label="有效期至" width="170">
        <template #default="{ row }">{{ formatDateTime(row.expires_at) }}</template>
      </el-table-column>
      <el-table-column label="状态" width="90">
        <template #default="{ row }">
          <el-tag disable-transitions :type="row.active ? 'success' : 'info'">
            {{ row.active ? '有效' : row.revoked_at ? '已撤销' : '已过期' }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="操作" width="80">
        <template #default="{ row }">
          <el-button v-if="row.active" link type="danger" @click="revoke(row)">撤销</el-button>
        </template>
      </el-table-column>
    </el-table>

    <h4>平台访问记录</h4>
    <el-table :data="data?.accesses ?? []" size="small" empty-text="平台没有查看过" data-testid="access-table">
      <el-table-column label="时间" width="170">
        <template #default="{ row }">{{ formatDateTime(row.created_at) }}</template>
      </el-table-column>
      <el-table-column label="操作">
        <template #default="{ row }">{{ WHAT[row.what] ?? row.what }}</template>
      </el-table-column>
      <el-table-column prop="resource_id" label="会话" />
    </el-table>
  </div>
</template>

<style scoped>
.reason {
  width: 280px;
}

.hours {
  width: 110px;
}

.sub {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

h4 {
  margin: 20px 0 8px;
}
</style>
