<script setup lang="ts">
import { errorMessage, type Schemas } from '@edp/api-client'
import { ElMessage } from 'element-plus'
import { onMounted, ref } from 'vue'

import { api, formatDateTime } from '../api'

const CHANNEL_TYPES: Record<string, string> = {
  web: '网页',
  wecom_kf: '微信客服',
  wecom_contact: '企业微信客户联系',
  email: '邮件',
}
const CORP_STATUS: Record<string, string> = { active: '已授权', cancelled: '已取消授权' }

const overview = ref<Schemas['ChannelOverview'] | null>(null)
const loading = ref(false)

async function load(): Promise<void> {
  loading.value = true
  const { data, error } = await api.GET('/platform/v1/channels')
  loading.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  overview.value = data
}

onMounted(load)
</script>

<template>
  <div>
    <div class="page-header">
      <h2>渠道授权</h2>
      <el-button @click="load">刷新</el-button>
    </div>
    <el-alert
      v-if="overview && !overview.wecom_configured"
      type="info"
      :closable="false"
      show-icon
      title="平台还没有配置企业微信服务商（EDP_WECOM_SUITE_ID），租户不能接入企业微信"
      class="notice"
    />
    <el-table v-loading="loading" :data="overview?.items ?? []" data-testid="channel-table">
      <el-table-column label="租户" min-width="160">
        <template #default="{ row }">{{ row.name }}<span class="sub"> {{ row.code }}</span></template>
      </el-table-column>
      <el-table-column label="启用的渠道" min-width="200">
        <template #default="{ row }">
          <el-tag disable-transitions v-for="(n, type) in row.channels" :key="type" size="small" class="tag">
            {{ CHANNEL_TYPES[type] ?? type }} {{ n }}
          </el-tag>
          <span v-if="row.disabled_channels" class="sub">停用 {{ row.disabled_channels }} 个</span>
        </template>
      </el-table-column>
      <el-table-column label="企业微信" min-width="260">
        <template #default="{ row }">
          <template v-if="row.wecom">
            <strong>{{ row.wecom.corp_name || row.wecom.corp_id }}</strong>
            <el-tag disable-transitions size="small" :type="row.wecom.status === 'active' ? 'success' : 'info'" class="tag">
              {{ CORP_STATUS[row.wecom.status] ?? row.wecom.status }}
            </el-tag>
            <div class="sub">
              授权于 {{ formatDateTime(row.wecom.authorized_at) }}；客服账号 {{ row.wecom.kf_accounts }} 个，成员
              {{ row.wecom.members }} 人
            </div>
            <div class="sub">
              最近同步：{{ row.wecom.last_sync_at ? formatDateTime(row.wecom.last_sync_at) : '—' }}
            </div>
            <div v-for="e in row.wecom.sync_errors" :key="e" class="error">{{ e }}</div>
          </template>
          <span v-else class="sub">未接入</span>
        </template>
      </el-table-column>
    </el-table>
  </div>
</template>

<style scoped>
.page-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 16px;
}

.page-header h2 {
  margin: 0;
  font-size: 18px;
}

.notice {
  margin-bottom: 12px;
}

.tag {
  margin-right: 6px;
}

.sub {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.error {
  font-size: 12px;
  color: var(--el-color-danger);
}
</style>
