<script setup lang="ts">
import { errorMessage, type Schemas } from '@edp/api-client'
import { ElMessage, ElMessageBox } from 'element-plus'
import { onMounted, ref } from 'vue'

import { api, formatDateTime } from '../api'

const props = defineProps<{ tenant: Schemas['TenantOut'] }>()
const emit = defineEmits<{ changed: [] }>()

const status = ref<Schemas['ClosureStatus'] | null>(null)
const deletion = ref<Schemas['TenantDeletionOut'] | null>(null)
const busy = ref(false)

async function load(): Promise<void> {
  const { data, error } = await api.GET('/platform/v1/tenants/{tenant_id}/closure', {
    params: { path: { tenant_id: props.tenant.id } },
  })
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  status.value = data
  if (props.tenant.purged_at) {
    const records = await api.GET('/platform/v1/deletions')
    deletion.value = records.data?.items.find((d) => d.tenant_id === props.tenant.id) ?? null
  }
}

async function requestClosure(): Promise<void> {
  const result = await ElMessageBox.prompt(
    `注销「${props.tenant.name}」：先自动导出一次数据，保留 ${status.value?.retention_days ?? 30} 天后删除全部业务数据。请填写原因。`,
    '代租户申请注销',
    { confirmButtonText: '申请注销', cancelButtonText: '取消', inputPattern: /\S/, inputErrorMessage: '请填写原因' },
  ).catch(() => null)
  if (!result) return
  busy.value = true
  const { data, error } = await api.POST('/platform/v1/tenants/{tenant_id}/closure', {
    params: { path: { tenant_id: props.tenant.id } },
    body: { reason: result.value },
  })
  busy.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  status.value = data
  emit('changed')
}

async function cancelClosure(): Promise<void> {
  busy.value = true
  const { data, error } = await api.DELETE('/platform/v1/tenants/{tenant_id}/closure', {
    params: { path: { tenant_id: props.tenant.id } },
  })
  busy.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  status.value = data
  emit('changed')
}

async function purgeNow(): Promise<void> {
  const result = await ElMessageBox.prompt(
    `立即删除「${props.tenant.name}」的全部业务数据（客户、会话、消息、知识库、员工、文件），不能恢复。请输入企业代码 ${props.tenant.code} 确认。`,
    '立即删除数据',
    {
      type: 'error',
      confirmButtonText: '删除',
      cancelButtonText: '取消',
      inputValidator: (value) => value === props.tenant.code || '企业代码不正确',
    },
  ).catch(() => null)
  if (!result) return
  busy.value = true
  const { data, error } = await api.POST('/platform/v1/tenants/{tenant_id}/purge', {
    params: { path: { tenant_id: props.tenant.id } },
  })
  busy.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  deletion.value = data.deletion
  ElMessage.success('数据已删除，已生成删除记录')
  emit('changed')
}

onMounted(load)
</script>

<template>
  <div v-if="status" v-loading="busy" data-testid="tenant-closure">
    <el-result
      v-if="tenant.purged_at"
      icon="info"
      title="已注销"
      :sub-title="`数据已于 ${formatDateTime(tenant.purged_at)} 删除`"
    >
      <template #extra>
        <el-descriptions v-if="deletion" :column="1" border size="small" class="record">
          <el-descriptions-item label="删除的记录">{{ deletion.counts.rows }} 行</el-descriptions-item>
          <el-descriptions-item label="删除的文件">{{ deletion.counts.objects }} 个</el-descriptions-item>
          <el-descriptions-item label="SHA-256">
            <code data-testid="deletion-digest">{{ deletion.digest }}</code>
          </el-descriptions-item>
        </el-descriptions>
      </template>
    </el-result>
    <template v-else-if="status.closing">
      <el-alert
        type="warning"
        :closable="false"
        show-icon
        :title="`已申请注销（${formatDateTime(status.requested_at ?? '')}），将于 ${formatDateTime(status.scheduled_at ?? '')} 删除全部业务数据`"
      />
      <div class="actions">
        <el-button @click="cancelClosure">撤销注销</el-button>
        <el-button type="danger" data-testid="purge-button" @click="purgeNow">立即删除数据</el-button>
      </div>
    </template>
    <template v-else>
      <p class="sub">
        注销时先自动导出一次数据（租户管理员可以在控制台下载），保留 {{ status.retention_days }}
        天后删除全部业务数据，并生成删除记录。订阅、账单和平台审计记录保留。
      </p>
      <el-button type="danger" plain data-testid="closure-button" @click="requestClosure">
        代租户申请注销
      </el-button>
    </template>
  </div>
</template>

<style scoped>
.actions {
  margin-top: 12px;
  display: flex;
  gap: 8px;
}

.record {
  width: 560px;
}

.sub {
  font-size: 13px;
  color: var(--el-text-color-secondary);
}
</style>
